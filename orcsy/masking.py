"""数据脱敏：密码、密钥等敏感信息发送模型厂商前在本地掩除（详见 docs/design-data-masking.md）。

旁路安全设计（同 notify/用量台账）：处理只在内存中进行——
- 不写临时文件、不持久化映射、日志只记命中次数绝不记内容；
- 占位符格式 `[REDACTED:类别:8位哈希]`：同值同占位符（跨轮一致，模型可引用）、
  不匹配任何脱敏正则（重复脱敏是恒等变换，历史重发无嵌套叠加）；
- 任务级 MaskingSession 映射仅存于实例属性，run_task_loop 结束即 clear()，
  LLMClient 实例随任务结束被 GC——任何时刻磁盘上没有映射或原文副本。

开关在 config.yaml 的 masking 节（enabled/enhanced_rules/custom_patterns），
由 web 层随配置加载/保存注入 configure()，看板改完立即生效。
"""

from __future__ import annotations

import copy
import hashlib
import re

from .config import MaskingConfig
from .logging_setup import get_logger

log = get_logger("masking")

# 占位符形态：[REDACTED:类别:8位哈希]。哈希保证同值同占位符、异值可区分。
# 幂等性（占位符不被任何规则二次匹配）不靠「字样恰好不匹配」的巧合，而是
# 由 mask_text 内部的占位符守卫兜底（见 _PLACEHOLDER_GUARD）：增强规则的
# 键值对形态（如 token=<32位哈希>）曾会命中占位符里的哈希段造成嵌套
_PLACEHOLDER = "[REDACTED:{category}:{tag}]"
# 占位符守卫：匹配任何「本模块产出的占位符」整段，sub 时原样放回——
# 使占位符对后续规则透明，重复脱敏恒等（无嵌套叠加）
_PLACEHOLDER_GUARD = re.compile(r"\[REDACTED:[A-Za-z0-9_-]+:[0-9a-f]{8}\]")


# ---- 内置规则（模块加载时一次性预编译，任务循环高频路径不重复编译） ----

# 高置信规则：只匹配格式自证敏感的令牌，误伤率趋近零（决策一：默认开）
_HIGH_CONFIDENCE: list[tuple[re.Pattern, str]] = [
    # (编译正则, 类别名)。顺序即替换顺序，先长后短避免部分前缀抢先命中
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "api_key"),          # OpenAI/DeepSeek/GLM 系
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"), "api_key"),     # GitHub PAT（ghp_/gho_/ghu_/ghs_/ghr_）
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "api_key"),   # GitHub fine-grained PAT
    (re.compile(r"glpat-[A-Za-z0-9_-]{15,}"), "api_key"),       # GitLab PAT
    (re.compile(r"AKIA[0-9A-Z]{16}"), "api_key"),               # AWS AccessKeyId
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "api_key"),          # Google API key
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "api_key"),   # Slack token
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "jwt"),  # JWT 三段式
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                re.DOTALL), "private_key"),                     # PEM 私钥块（单块整体）
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{16,}"), "bearer"),  # Bearer 认证头
    # URL 查询串令牌：参数名自证敏感，值 ≥8 字符才掩（防误伤 "token=" 空占位）
    (re.compile(r"(?i)(access_token|api_key|apikey|secret|client_secret|auth_token|token)="
                r"([A-Za-z0-9_.~/+=-]{8,})"), "url_token"),
    # 连接串密码段：scheme://user:<password>@host —— 密码段不含 /@ 排除边界溢出
    (re.compile(r"(?i)(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|http|https|ftp)://"
                r"[^:/@\s]+:([^@\s/]{4,})@"), "db_password"),
]

# 键值对增强规则：key 保留 value 掩除（决策一：默认关，误伤文档示例/测试夹具）
_ENHANCED: list[tuple[re.Pattern, str]] = [
    # 常见敏感键名 = 值（env/ini/yaml 平铺、shell 变量；值 ≥4 字符）
    (re.compile(r"(?i)\b(api[_-]?key|apikey|secret|token|password|passwd|pwd|"
                r"access[_-]?key|private[_-]?key|auth[_-]?token|client[_-]?secret|"
                r"credentials?)\b(\s*[=:]\s*)"
                r"(\"[^\"]{4,}\"|'[^']{4,}'|[^\s&\"']{4,})"), "secret_value"),
    # JSON 形态 "password": "xxx"（上面规则已覆盖，此条兜底冒号紧邻形态）
    (re.compile(r"(?i)\"(api[_-]?key|secret|token|password|access[_-]?token|"
                r"client[_-]?secret)\"(\s*:\s*)\"([^\"]{4,})\""), "secret_value"),
]

# 增强规则的掩除分组（上面规则里 value 所在捕获组序号）：key/分隔符保留
_ENHANCED_VALUE_GROUP = 3


# ---- 模块级开关（masking 节），由 web 层注入 configure() ----

_FLAGS: dict[str, bool] = {"enabled": True, "enhanced_rules": False}
_CUSTOM: tuple[tuple[re.Pattern, str], ...] = ()


def configure(cfg) -> None:
    """注入 masking 配置（AppConfig.masking），看板保存后随 _apply_config 即时生效。"""
    global _CUSTOM
    _FLAGS["enabled"] = bool(getattr(cfg, "enabled", True))
    _FLAGS["enhanced_rules"] = bool(getattr(cfg, "enhanced_rules", False))
    patterns = []
    for raw in getattr(cfg, "custom_patterns", None) or []:
        try:
            patterns.append((re.compile(raw), "custom"))
        except re.error as e:  # 理论上 config 加载已拦截；防御式跳过不炸运行时
            log.warning("自定义脱敏正则无法编译已跳过：%s（%s）", raw[:50], e)
    _CUSTOM = tuple(patterns)


def enabled() -> bool:
    """总开关：false 时 _post 走零开销透传路径。"""
    return _FLAGS["enabled"]


def _active_rules() -> list[tuple[re.Pattern, str, int]]:
    """当前生效的规则集：(编译正则, 类别名, 掩除分组)。掩除分组 0 = 整段匹配。"""
    if not _FLAGS["enabled"]:
        return []
    rules: list[tuple[re.Pattern, str, int]] = [
        (p, c, 0) for p, c in _HIGH_CONFIDENCE]
    if _FLAGS["enhanced_rules"]:
        rules.extend((p, c, _ENHANCED_VALUE_GROUP) for p, c in _ENHANCED)
    rules.extend((p, c, 0) for p, c in _CUSTOM)
    return rules


class MaskingSession:
    """任务级脱敏会话：出站掩除 + 写回还原的双向映射（纯内存，用完即清）。"""

    def __init__(self) -> None:
        # _rev: 原文 → 占位符（脱敏查/建）；_fwd: 原文 sha256 全哈希 → 原文
        # （还原查表走全哈希精确匹配，占位符 8 位前缀仅展示/查找起点）
        self._rev: dict[str, str] = {}
        self._fwd: dict[str, str] = {}
        self._fwd_tag: dict[str, str] = {}  # 8位前缀 → 全哈希（restore 入口）

    # ---- 出站：原文 → 占位符 ----

    def mask_text(self, text: str) -> tuple[str, int]:
        """对一段文本应用当前生效规则，返回 (脱敏后文本, 命中次数)。

        幂等保障：每条规则替换前先把已有占位符换成守卫段（\x00+序号），
        该条替换完立即还原——占位符对规则透明，重复脱敏是恒等变换（无嵌套）。
        """
        rules = _active_rules()
        if not rules or not text:
            return text, 0
        hits = 0
        # 守卫槽位：占位符 → \x00N。 \x00 不会出现在正常文本与任何规则的
        # 字符类里；N 保证每个占位符有独立槽位，替换后按槽位精确还原
        slots: dict[str, str] = {}

        def _stash(m: re.Match) -> str:
            ph = m.group(0)
            slots.setdefault(ph, f"\x00{len(slots)}\x00")
            return slots[ph]

        for pattern, category, group in rules:
            guarded = _PLACEHOLDER_GUARD.sub(_stash, text)

            def _sub(m: re.Match) -> str:
                nonlocal hits
                value = m.group(group) if group else m.group(0)
                if not value:
                    return m.group(0)
                ph = self._rev.get(value)
                if ph is None:
                    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
                    ph = _PLACEHOLDER.format(category=category, tag=digest[:8])
                    self._rev[value] = ph
                    self._fwd[digest] = value
                    self._fwd_tag[digest[:8]] = digest
                hits += 1
                # key/分隔符保留（增强规则 group>0），value 换占位符
                if group:
                    return m.group(0).replace(value, ph, 1)
                return ph

            try:
                text = pattern.sub(_sub, guarded)
            except Exception:  # noqa: BLE001 — 单条规则异常不炸请求
                log.warning("脱敏规则执行异常已跳过", exc_info=True)
            finally:
                # 本条规则处理完立即放回占位符（下一条规则重新 stash）
                for ph, slot in slots.items():
                    if slot in text:
                        text = text.replace(slot, ph)
        return text, hits

    def mask_payload(self, payload: dict) -> tuple[dict, int]:
        """深拷贝 payload 并对消息文本字段脱敏，返回 (脱敏副本, 命中数)。

        只动副本不碰 self.messages——本地历史保留原文（护栏计数、上下文裁剪
        依赖原文）。覆盖两种协议的敏感文本面：
        - OpenAI：messages[].content、tool_calls[].function.arguments
        - Anthropic：system、messages[].content（字符串或块列表 text/tool_result）
        """
        data = copy.deepcopy(payload)
        hits = 0
        messages = data.get("messages")
        if isinstance(messages, list):
            for m in messages:
                if not isinstance(m, dict):
                    continue
                c = m.get("content")
                if isinstance(c, str):
                    c2, n = self.mask_text(c)
                    if n:
                        m["content"] = c2
                        hits += n
                elif isinstance(c, list):
                    for b in c:
                        if not isinstance(b, dict):
                            continue
                        for key in ("text", "content"):
                            v = b.get(key)
                            if isinstance(v, str):
                                v2, n = self.mask_text(v)
                                if n:
                                    b[key] = v2
                                    hits += n
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") if isinstance(tc, dict) else None
                    args = fn.get("arguments") if fn else None
                    if isinstance(args, str):
                        a2, n = self.mask_text(args)
                        if n:
                            fn["arguments"] = a2
                            hits += n
        system = data.get("system")
        if isinstance(system, str):
            s2, n = self.mask_text(system)
            if n:
                data["system"] = s2
                hits += n
        return data, hits

    # ---- 返回侧：占位符 → 原文（写回还原） ----

    def restore(self, text: str) -> str:
        """把文本中「本会话建立的占位符」还原为原文；未知占位符形态原样透传。

        模型把占位符写回工具参数（write_file 内容、run_command 命令串等）
        时在 dispatch 前调用——复制密钥类任务正常完成，整文件回写不丢数据。
        """
        if not text or not self._rev:
            return text
        out = text
        # 按占位符字面量还原（占位符含原文完整哈希锚点，只认映射内条目；
        # 模型幻觉的占位符不在映射里，自然透传）
        for value, ph in self._rev.items():
            if ph in out:
                out = out.replace(ph, value)
        return out

    def clear(self) -> None:
        """清除全部映射（任务结束 finally 调用，处理完即清除）。"""
        self._rev.clear()
        self._fwd.clear()
        self._fwd_tag.clear()
