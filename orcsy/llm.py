"""大模型接入与 Agent 工具循环。

两种协议均原生支持工具调用（function calling）：
- openai：OpenAI Chat Completions + tools（覆盖 DeepSeek/GLM/Kimi/通义/Ollama/vLLM 等）
- anthropic：Anthropic Messages API + tools

Agent 循环（run_task_loop）：
  任务提示 → 模型 → （工具调用 → 执行 → 结果回传）* N → 模型调用 finish 工具 → 结构化结论

模型的思考内容（OpenAI 兼容的 reasoning_content / Anthropic 的 thinking 块）在
模型返回时原样写入运行日志（💭 前缀条目），看板里可展开查看；不回传给服务端。

安全：文件工具被 ToolContext 禁锢在项目根内；命令工具有白名单；循环有轮数上限。
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from . import USER_AGENT, masking
from .concurrency import get_budget
from .config import LLMConfig
from .logging_setup import get_logger
from .masking import MaskingSession
from .tools import (
    FinishSignal,
    TaskStopped,
    ToolContext,
    dispatch_tool,
    tool_definitions_anthropic,
    tool_definitions_openai,
)

log = get_logger("llm")

DEFAULT_MAX_TURNS = 40

# 知名模型的（上下文窗口, 最大输出）兜底规格：OpenAI 兼容服务的 /models
# 列表通常不带窗口元数据，看板选模型时按 id 子串命中补全；顺序即优先级
# （先具体后泛化），数值取官方公开值，用户可在设置页手改。
MODEL_SPECS: list[tuple[str, int, int]] = [
    ("deepseek-reasoner", 65536, 65536),
    ("deepseek-chat", 65536, 8192),
    ("deepseek", 65536, 8192),
    ("kimi-k2", 131072, 8192),
    ("moonshot-v1-128k", 131072, 8192),
    ("moonshot-v1-32k", 32768, 8192),
    ("moonshot-v1-8k", 8192, 4096),
    ("moonshot", 131072, 8192),
    ("glm-4.6", 200000, 128000),
    ("glm-4.5", 131072, 96768),
    ("glm-4", 131072, 4096),
    ("glm", 131072, 8192),
    ("qwen3", 131072, 16384),
    ("qwen", 131072, 8192),
    ("gpt-4.1", 1048576, 32768),
    ("gpt-4o", 128000, 16384),
    ("gpt-4", 8192, 4096),
    ("gpt", 128000, 16384),
    ("gemini", 1048576, 65536),
    ("claude", 200000, 64000),
]


def model_spec(model_id: str) -> tuple[int, int] | None:
    """按 id 子串查内置规格表；未命中返回 None（不瞎猜）。"""
    mid = (model_id or "").lower()
    for key, ctx, out in MODEL_SPECS:
        if key in mid:
            return ctx, out
    return None


def _auth_headers(provider: str, api_key: str) -> dict[str, str]:
    """构造认证头；api_key 为空（Ollama/vLLM 等本地服务）时不带认证头，
    避免空 Bearer 值触发 httpx Illegal header value。"""
    if provider == "anthropic":
        headers = {"anthropic-version": "2023-06-01"}
        if api_key:
            headers["x-api-key"] = api_key
        return headers
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _anthropic_base(base_url: str) -> str:
    """归一化 anthropic 兼容地址：自动补 /v1。

    官方（https://api.anthropic.com）与多数兼容服务（如 MiMo 的
    https://token-plan-cn.xiaomimimo.com/anthropic）公布的根地址都不含 /v1，
    而对话端点在其下 /v1/messages——用户照抄官方地址即可，这里统一补齐；
    已以 /v1 结尾的地址不重复补（兼容既有配置与自带 /v1 的代理服务）。
    """
    base = (base_url or "").rstrip("/")
    return base if base.endswith("/v1") else base + "/v1"


def test_connection(provider: str, base_url: str, api_key: str, model: str,
                    timeout_seconds: int = 300) -> dict:
    """最小对话请求验证大模型接入连通性（看板「测试连接」按钮）。

    只发一条极短 user 消息、max_tokens=64、不带工具与思考参数，把失败面与
    费用压到最小：HTTP 200 即证明 网络可达 + 认证有效 + 模型名存在，回复正文
    仅作展示——推理模型（DeepSeek-R1 等）思考可能耗尽小额 max_tokens 导致
    正文为空，同样算连通。返回 {reply, elapsed_seconds}；失败抛 LLMError。
    """
    provider = provider.strip().lower()
    model = model.strip()
    if provider not in ("openai", "anthropic"):
        raise LLMError(f"provider 不合法：{provider}（openai / anthropic）")
    if not model:
        raise LLMError("model 不能为空")
    client = LLMClient(LLMConfig(
        provider=provider, api_key=api_key, base_url=base_url, model=model,
        # -1（不超时）对一次性探活没有意义：按 30s 兜底
        timeout_seconds=(30 if int(timeout_seconds) <= 0
                         else max(5, min(int(timeout_seconds), 120))),
    ))
    if provider == "anthropic":
        url = _anthropic_base(base_url) + "/messages"
        headers = _auth_headers("anthropic", api_key)
    else:
        url = base_url.rstrip("/") + "/chat/completions"
        headers = _auth_headers("openai", api_key)
    payload = {
        "model": model, "max_tokens": 64,
        "messages": [{"role": "user", "content": "连接测试，请回复：OK"}],
    }
    started = time.monotonic()
    data = client._post(url, payload, headers)
    elapsed = round(time.monotonic() - started, 1)
    if provider == "anthropic":
        reply = "".join(b.get("text", "") for b in data.get("content") or []
                        if isinstance(b, dict) and b.get("type") == "text").strip()
    else:
        try:
            reply = str(data["choices"][0]["message"].get("content") or "").strip()
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(
                f"响应结构异常：{json.dumps(data, ensure_ascii=False)[:300]}") from e
    return {"reply": reply[:200], "elapsed_seconds": elapsed}


def list_models(provider: str, base_url: str, api_key: str,
                timeout_seconds: int = 30) -> list[dict]:
    """拉取大模型服务的模型列表，尽量带上每个模型的上下文/输出上限。

    openai 兼容与 anthropic 都是 GET {base_url}/models；anthropic 响应自带
    context_window / max_output_tokens，openai 兼容服务通常没有（置 None，
    由看板用 model_spec 补全）。
    """
    if provider == "anthropic":
        url = _anthropic_base(base_url) + "/models"
    else:
        url = base_url.rstrip("/") + "/models"
    headers = {"User-Agent": USER_AGENT, **_auth_headers(provider, api_key)}
    # -1（不超时）对拉取列表没有意义：按 30s 兜底
    list_timeout = (30 if int(timeout_seconds) <= 0
                    else min(max(timeout_seconds, 5), 30))
    try:
        resp = httpx.get(url, headers=headers, timeout=list_timeout)
    except httpx.HTTPError as e:
        raise LLMError(f"获取模型列表失败：{e}") from e
    if resp.status_code == 404:
        # 404 ≠ 密钥/网络问题，是该 base_url 下没有 /models 端点——按协议给
        # 可行动的提示（如 MiMo 等 anthropic 兼容面只有 /messages，无列表端点）
        if provider == "anthropic":
            raise LLMError(
                "获取模型列表失败：HTTP 404 —— 该服务的 anthropic 兼容面通常不提供"
                "模型列表端点（仅 /messages 对话）。可直接手填模型名，"
                "或改用其 openai 兼容地址拉取列表。")
        raise LLMError(
            "获取模型列表失败：HTTP 404 —— 该地址下没有 /models 端点，"
            "请检查 base_url（OpenAI 兼容地址通常以 /v1 结尾）。")
    if resp.status_code != 200:
        raise LLMError(f"获取模型列表失败：HTTP {resp.status_code} {resp.text[:200]}")
    try:
        data = resp.json().get("data") or []
    except ValueError as e:
        raise LLMError("获取模型列表失败：响应不是 JSON") from e
    models = []
    for m in data:
        mid = str(m.get("id") or "").strip()
        if not mid:
            continue
        models.append({
            "id": mid,
            "context_window": m.get("context_window"),
            "max_output_tokens": m.get("max_output_tokens"),
        })
    models.sort(key=lambda x: x["id"])
    return models


# 早期工具结果被裁剪后的占位文本
_TRIM_PLACEHOLDER = "（早期工具结果已省略）"
# 滚动压缩参数：最近 N 条消息保持原文；其前的长工具结果占位化
_KEEP_RECENT_MSGS = 30
_TOOL_KEEP_CHARS = 2500
# 历史估算 token 软预算：超过即压缩旧工具结果（与模型窗口无关的常驻成本控制）
_SOFT_BUDGET_TOKENS = 64000


def _est_tokens_msg(m: dict) -> int:
    """粗估一条消息的 token 数（len//2，中文偏保守；用于压缩触发判断）。"""
    try:
        return max(1, len(json.dumps(m, ensure_ascii=False)) // 2)
    except (TypeError, ValueError):
        return 1


# 思考内容写入运行日志的单轮上限：思考动辄数千字，不截断会把日志尾
# （看板只读文件尾部）刷满，反而看不见执行主线
_THINKING_LOG_CHARS = 6000


def _clip_thinking(text: str) -> str:
    if len(text) <= _THINKING_LOG_CHARS:
        return text
    return text[:_THINKING_LOG_CHARS] + f"\n…（思考内容超过 {_THINKING_LOG_CHARS} 字，已截断）"


class LLMError(RuntimeError):
    pass


# ---- 瞬时故障有界重试：单次超时/网关抖动不应判死整个任务 ----
# 实测（2026-11-03）：后端任务单请求 timed out 一次即整任务 blocked，
# 只能等下一调度周期重跑。对可重试错误自动退避重试，最终失败才抛 LLMError。
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
# 真·配额限流（其它 5xx 是网关抖动，不该收缩并发预算）
_RATE_LIMIT_STATUS = frozenset({429, 529})
_LLM_MAX_ATTEMPTS = 4            # 首次 + 3 次重试
_LLM_RETRY_BACKOFF = (2, 4, 8)   # 每次重试前的退避秒数
# 厂商建议等待时长的响应头：Retry-After（秒或 HTTP 日期）优先；部分 OpenAI
# 兼容网关与 Anthropic 用 *-reset-* 给「距配额重置还有多久」（形如 6m0s）
_RETRY_HINT_HEADERS = (
    "retry-after",
    "x-ratelimit-reset-requests",
    "anthropic-ratelimit-requests-reset",
    "x-ratelimit-reset-tokens",
)
# 厂商建议值上限：超过就不等了（4 次重试的窗口内等一小时等于挂死）
_RETRY_HINT_CAP_SECONDS = 120.0
_DURATION_RE = re.compile(r"([\d.]+)\s*(ms|s|m|h)", re.IGNORECASE)


def _parse_wait_value(raw: str) -> float | None:
    """解析一个等待时长值：纯秒数 / HTTP 日期 / 1s·100ms·6m0s 时长串。"""
    text = raw.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:  # Retry-After 也可以是一个 HTTP 日期
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        dt = None
    if dt is not None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    total = 0.0
    matched = False
    for num, unit in _DURATION_RE.findall(text):
        total += float(num) * {"ms": 0.001, "s": 1.0,
                               "m": 60.0, "h": 3600.0}[unit.lower()]
        matched = True
    return total if matched else None


def _parse_server_delay(resp) -> float | None:
    """从响应头取厂商建议的等待秒数；拿不到返回 None。

    不是所有厂商都给：OpenAI 兼容网关常见 ``x-ratelimit-reset-requests``，
    Anthropic 用 ``anthropic-ratelimit-requests-reset``（+``retry-after``），
    Gemini / DeepSeek 多数只回 429 不带窗口——那种情况交给 AIMD 自己收敛。
    """
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    for name in _RETRY_HINT_HEADERS:
        raw = headers.get(name)
        if not raw:
            continue
        value = _parse_wait_value(str(raw))
        if value is not None:
            return min(_RETRY_HINT_CAP_SECONDS, max(0.5, value))
    return None
# 连续 N 轮未调用任何工具（只输出纯文本）视为卡住，升级强制收尾；
# 否则「提醒」会无限循环（轮数不限的任务永不结束）
_NO_TOOL_FORCE_TURNS = 3
# 完全相同参数的同一工具调用第 N 次起在结果里附警告（模型打转止损，
# 实测会 100+ 轮重读同一批文件）；计数仅存内存，任务结束即弃
_REPEAT_CALL_WARN = 3
# 轮询类调试工具豁免打转警告（等待条件/拉取新日志/查看快照天然会以相同参数调用）
_POLLING_EXEMPT_TOOLS = frozenset({
    "browser_snapshot", "browser_wait", "browser_console",
    "browser_network", "dev_server_logs",
})


class AgentResponse:
    """Agent 对单个任务的结构化结论（来自 finish 工具）。"""

    def __init__(self, action: str, summary: str, files_changed: list[str],
                 commit_message: str, notes: str,
                 issues: list[dict] | None = None) -> None:
        self.action = action              # completed / blocked / cannot_do
        self.summary = summary
        self.files_changed = files_changed
        self.commit_message = commit_message
        self.notes = notes
        # 结构化问题清单（可选，审查场景使用；[{file, line?, severity, message}]）
        self.issues = issues or []

    @classmethod
    def fallback(cls, reason: str) -> "AgentResponse":
        return cls(action="blocked", summary="", files_changed=[],
                   commit_message="", notes=f"模型未按协议给出结论：{reason}")


def parse_finish_args(args_json: str | dict) -> dict:
    """解析 finish 工具的参数；不是 JSON 对象时返回空 dict。"""
    try:
        payload = json.loads(args_json) if isinstance(args_json, str) else (args_json or {})
    except json.JSONDecodeError:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _parse_issues(raw: object) -> list[dict]:
    """宽松解析 finish.issues（审查场景的结构化问题清单）。

    只收形状完整的条目（file/severity/message 必填），line 可缺失；其余字段
    忽略。非法条目静默跳过——解析失败不应影响结论本身。
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        file = str(item.get("file") or "").strip()
        severity = str(item.get("severity") or "").strip().lower()
        message = str(item.get("message") or "").strip()
        if not file or not message or severity not in ("blocker", "major", "minor"):
            continue
        entry: dict = {"file": file, "severity": severity, "message": message}
        line = item.get("line")
        if isinstance(line, int) and not isinstance(line, bool) and line > 0:
            entry["line"] = line
        out.append(entry)
    return out


def finish_response(payload: dict) -> AgentResponse:
    """把 finish 参数转成 AgentResponse，协议不完整时返回 blocked 兜底。"""
    action = str(payload.get("action", "")).strip().lower()
    summary = str(payload.get("summary", "")).strip()
    if action not in ("completed", "blocked", "cannot_do", "split", "wait_retest") or not summary:
        return AgentResponse.fallback(f"finish 参数不完整：action={action!r}")
    files = payload.get("files_changed") or []
    return AgentResponse(
        action=action,
        summary=summary,
        files_changed=[str(f) for f in files] if isinstance(files, list) else [],
        commit_message=str(payload.get("commit_message", "")).strip(),
        notes=str(payload.get("notes", "")).strip(),
        issues=_parse_issues(payload.get("issues")),
    )


class LLMClient:
    """同一任务的对话客户端：维护完整消息历史（含工具调用轮次）。"""

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self.messages: list[dict] = []
        # 每轮响应的 usage 累计（两种协议字段不同，这里统一收敛）
        self.usage = {"turns": 0, "prompt_tokens": 0, "completion_tokens": 0}
        # 启用的工具名集合（run_task_loop 从 ToolContext 取）；None = 全量下发
        self._enabled_tools: frozenset[str] | None = None
        # 强制停止信号（run_task_loop 从 ToolContext 取）：HTTP 请求在子线程
        # 执行、主线程 0.2s 轮询它——不超时（timeout_seconds=-1）时挂起的
        # 请求也能被看板「强制停止」秒级打断
        self._stop_event: object | None = None
        # 瞬时故障重试参数（测试可覆盖以退避归零加速）
        self.retry_attempts = _LLM_MAX_ATTEMPTS
        self.retry_backoff: tuple[float, ...] = _LLM_RETRY_BACKOFF
        # 数据脱敏会话（任务级）：出站 payload 掩除敏感信息，模型写回占位符
        # 时还原原文；映射仅存内存，任务循环结束即清除（masking.configure
        # 关闭总开关时 mask_payload 内部零开销透传）
        self._masking = MaskingSession()

    def _acc_usage(self, data: dict, provider: str) -> None:
        u = data.get("usage") or {}
        if provider == "anthropic":
            pt = int(u.get("input_tokens") or 0)
            ct = int(u.get("output_tokens") or 0)
        else:
            pt = int(u.get("prompt_tokens") or 0)
            ct = int(u.get("completion_tokens") or 0)
        self.usage["turns"] += 1
        self.usage["prompt_tokens"] += pt
        self.usage["completion_tokens"] += ct

    def reset(self, system_prompt: str) -> None:
        self.messages = [{"role": "system", "content": system_prompt}]

    # ---- 消息体协议适配 ----

    @staticmethod
    def _user(content: str) -> dict:
        return {"role": "user", "content": content}

    @staticmethod
    def _assistant_tool_calls(call_id: str, name: str, args_json: str) -> dict:
        """把一次工具调用表示成 assistant 消息（两种协议统一成 OpenAI 形态存储）。"""
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": name, "arguments": args_json},
            }],
        }

    @staticmethod
    def _tool_result(call_id: str, content: str) -> dict:
        return {"role": "tool", "tool_call_id": call_id, "content": content}

    # ---- Agent 循环 ----

    def run_task_loop(self, task_prompt: str, ctx: ToolContext,
                      max_turns: int = DEFAULT_MAX_TURNS,
                      on_progress=None) -> AgentResponse:
        """执行一个任务的完整工具循环，返回结构化结论。

        on_progress(line) 在每次工具调用后触发（finish 除外），供执行器把
        进度落盘到任务文件——进程被强杀时进度不丢。
        """
        try:
            return self._run_task_loop(task_prompt, ctx, max_turns, on_progress)
        finally:
            # 数据脱敏映射处理完即清除：任务结束（含异常/停止/熔断）不留任何
            # 原文↔占位符对应关系在内存
            self._masking.clear()

    def _run_task_loop(self, task_prompt: str, ctx: ToolContext,
                       max_turns: int = DEFAULT_MAX_TURNS,
                       on_progress=None) -> AgentResponse:
        self.messages.append(self._user(task_prompt))
        # 工具开关：schema 按启用集过滤下发（dispatch_tool 里还有兜底拦截）
        self._enabled_tools = ctx.enabled_tools
        # 停止信号传到 HTTP 层：请求挂起期间「强制停止」也能生效
        self._stop_event = ctx.stop_event
        use_tools = True

        turn = 0
        # max_turns <= 0（如 -1）= 不设轮数上限：只有 finish / 停止信号 /
        # 具体防死循环护栏（同文件重读、连续只读、连续无工具）能结束循环。
        # 绝不额外加「整体轮数硬上限」——用户显式写 -1 就是要不限轮数，
        # 长任务的 QA 全流程 E2E 合理需要数百轮，硬墙会误杀正常推进。
        unlimited = max_turns is None or max_turns <= 0
        no_tool_turns = 0        # 连续未调用工具的轮数（连续过多升级强制收尾）
        repeat_calls: Counter[tuple[str, str]] = Counter()  # (工具, 参数) → 次数
        file_read_counts: Counter[str] = Counter()         # path -> 累计读取次数（防死循环）
        consecutive_read_turns = 0                         # 连续只读未动笔轮次
        forced_reason = ""       # 非空 = 因卡住/轮数耗尽强制收尾（而非 finish）

        while unlimited or turn < max_turns:
            turn += 1
            # 强制停止（看板置位）：在模型请求之前先看一眼，避免多跑一轮
            if ctx.stop_event is not None and ctx.stop_event.is_set():
                raise TaskStopped()
            if self.cfg.provider == "anthropic":
                assistant_msg, tool_calls, reasoning = self._step_anthropic(use_tools)
            else:
                assistant_msg, tool_calls, reasoning = self._step_openai(use_tools)

            self.messages.append(assistant_msg)
            # 模型返回了思考内容（DeepSeek-R1/GLM 等的 reasoning_content，
            # Anthropic 的 thinking 块）时写入运行日志，看板可展开查看；
            # 思考内容不回传给服务（多数协议禁止），只本地留档
            if reasoning:
                log.info("💭 模型思考（第 %d 轮）：\n%s", turn, _clip_thinking(reasoning))

            if not tool_calls:
                # 模型直接输出文字而非调用工具：提醒其走 finish 协议；
                # 连续多轮提醒仍不调工具视为卡住，升级强制收尾（防无限循环）
                no_tool_turns += 1
                said = (assistant_msg.get("content") or "").strip()[:200]
                log.debug("第 %d 轮模型未调用工具，输出：%s", turn, said)
                if no_tool_turns >= _NO_TOOL_FORCE_TURNS:
                    forced_reason = (f"连续 {no_tool_turns} 轮未调用任何工具"
                                     "（疑似卡住）")
                    break
                self.messages.append(self._user(
                    "请调用 finish 工具给出任务结论（action/summary 等字段）。"
                    "不要用纯文本回复。"
                ))
                continue

            no_tool_turns = 0

            for call in tool_calls:
                # 同一轮多个工具调用之间也检查：停止请求一个工具周期内即生效
                if ctx.stop_event is not None and ctx.stop_event.is_set():
                    raise TaskStopped()
                call_id, name, args_json = call
                log.info("工具调用 [%d] %s %s", turn, name,
                         (args_json or "")[:120].replace("\n", " "))
                if name == "finish":
                    # finish 不还原：结论（summary/notes）会写进任务文件落盘，
                    # 占位符保持脱敏形态正是需求效果（密钥不落盘）
                    raise FinishSignal(self._parse_finish_args(args_json))
                # 写回还原（返回侧闭环）：模型把占位符当真实值引用时（复制密钥、
                # 整文件回写等），dispatch 前还原原文——还原在参数日志之后，
                # 日志里只有占位符形态不泄露
                args_json = self._masking.restore(args_json)
                if on_progress is not None:
                    try:
                        on_progress(f"第{turn}轮 · {name} · "
                                    f"{(args_json or '')[:60].strip()}".replace("\n", " "))
                    except Exception:  # noqa: BLE001 进度回调失败不影响执行
                        pass

                # 先统一算只读/写标记与连读计数（护栏 1、2 共用）。
                # 关键：护栏 1 的拦截分支不能 continue 跳过计数——否则「永远
                # 重读同一文件」的模型连读轮数永远停在拦截前的值，护栏 2 的
                # 熔断阈值再也达不到，`-1` 不限轮数时就会无限空转。
                produces_code = bool(getattr(ctx, "produces_code", True))
                is_read_op = name in ("read_file", "list_dir", "find_files", "grep", "read_skill")
                is_write_op = name in ("write_file", "edit_file", "multi_edit", "delete_file", "move_file", "run_command")
                if is_write_op:
                    consecutive_read_turns = 0
                elif is_read_op:
                    consecutive_read_turns += 1

                # 防死循环 1：同一文件重复读取硬拦截（超过 5 次直接拒绝，迫使模型基于已知上下文开工）
                intercepted: str | None = None
                if name == "read_file":
                    p_target = ""
                    try:
                        p_args = json.loads(args_json) if isinstance(args_json, str) else (args_json or {})
                        p_target = str(p_args.get("path") or "").strip()
                    except Exception:
                        pass
                    if p_target:
                        file_read_counts[p_target] += 1
                        if file_read_counts[p_target] > 5:
                            intercepted = (
                                f"错误：文件 {p_target} 已在当前任务中累计读取过 {file_read_counts[p_target] - 1} 次！"
                                "系统已拦截本次重复读取。你已经完全掌握了该文件内容，严禁继续只读打转。"
                                "请立即调用 edit_file / write_file 开始编写代码，或调用 finish 汇报结果。"
                            )
                            log.warning("拦截重复读取文件 [%s]（第 %d 次）", p_target, file_read_counts[p_target])

                if intercepted is not None:
                    # 被拦截的重读同样计作一次只读轮（已在上面计入），这里只回
                    # 错误结果给模型；tool_call/tool_result 配对保持完整
                    log.debug("工具结果 [%d] %s（拦截）：%s", turn, name, intercepted[:120])
                    self.messages.append(self._tool_result(call_id, intercepted))
                else:
                    result = dispatch_tool(ctx, name, args_json)

                    if produces_code and consecutive_read_turns == 10:
                        result += (
                            f"\n\n🚨 警告：你已连续 {consecutive_read_turns} 轮只读文件而未动笔写代码！"
                            "已收集的信息足够推进。请立即停止调研，使用 edit_file 或 write_file 编写实现！"
                        )

                    # 完全相同参数的重复调用检测：给打转的模型一个止损信号
                    repeat_calls[(name, args_json or "")] += 1
                    repeats = repeat_calls[(name, args_json or "")]
                    if name not in _POLLING_EXEMPT_TOOLS and repeats >= _REPEAT_CALL_WARN:
                        if repeats == _REPEAT_CALL_WARN:
                            log.warning("工具调用 [%d] %s 已 %d 次以完全相同的参数重复执行", turn, name, repeats)
                        result += (
                            f"\n\n⚠️ 提醒：这是第 {repeats} 次以完全相同的参数调用 {name}，"
                            "重复执行不会产生新信息。请基于已有结果推进（写入/修改/下一步操作）；"
                            "若已无法推进，请调用 finish（action=blocked）说明原因。"
                        )
                    log.debug("工具结果 [%d] %s：%s", turn, name, result[:200].replace("\n", " "))
                    self.messages.append(self._tool_result(call_id, result))

                # 防死循环 2：代码生产类角色连续只读未动笔（含被拦截的重读）
                # 累计达上限 → 强制熔断收尾。用 ctx.produces_code 判定，单兵
                # （current_role 为空串）也覆盖，PM/QA（False）不误伤。
                if produces_code and consecutive_read_turns >= 20:
                    forced_reason = (
                        f"检测到只读死循环：已连续 {consecutive_read_turns} 轮仅读取文件"
                        "而未做任何改动，强制熔断收尾")
                    break

            if forced_reason:
                break

            self._trim_context()

        # 强制收尾：轮数耗尽（仅有限轮数模式），或命中具体防死循环护栏
        forced_reason = forced_reason or f"已达到最大工作轮数 {max_turns}"
        log.warning("%s，要求强制收尾", forced_reason)
        if ctx.stop_event is not None and ctx.stop_event.is_set():
            raise TaskStopped()
        if on_progress is not None:
            try:
                on_progress(f"{forced_reason}，要求强制收尾")
            except Exception:  # noqa: BLE001
                pass
        self.messages.append(self._user(
            f"{forced_reason}。请立即调用 finish 工具：能完成的部分说明完成度，"
            "未完成的部分用 action=blocked 并在 notes 里说明剩余工作。"
        ))
        try:
            if self.cfg.provider == "anthropic":
                assistant_msg, tool_calls, reasoning = self._step_anthropic(True)
            else:
                assistant_msg, tool_calls, reasoning = self._step_openai(True)
            if reasoning:
                log.info("💭 模型思考（收尾轮）：\n%s", _clip_thinking(reasoning))
            self.messages.append(assistant_msg)
            for call_id, name, args_json in tool_calls:
                if name == "finish":
                    return self._finish_response(self._parse_finish_args(args_json))
        except LLMError:
            pass
        return AgentResponse.fallback(f"{forced_reason}，模型未给出结论")

    @staticmethod
    def _parse_finish_args(args_json: str | dict) -> dict:
        return parse_finish_args(args_json)

    def _trim_context(self) -> None:
        """控制对话历史的常驻成本，避免 token 浪费（每轮工具结果落库后执行）。

        两级策略：
        1. 滚动压缩：历史估算超过软预算（默认 24K tokens，与模型窗口无关——
           历史每轮都要重发，小就是省）时，把「最近 N 条消息之外」的长工具
           结果占位化。这些信息模型已经消费过，保留原文纯属每轮重复计费。
        2. 紧急裁剪：估算逼近模型窗口 90% 时（长任务/小窗口），对剩余旧工具
           结果进一步占位化，防止 400 撞墙。
        system 与最近消息保持原文；tool_call/tool_result 配对不被破坏。
        """
        limit = int(getattr(self.cfg, "max_context_tokens", 0) or 0)
        if len(self.messages) <= _KEEP_RECENT_MSGS + 2:
            return
        total = sum(_est_tokens_msg(m) for m in self.messages)
        soft = min(_SOFT_BUDGET_TOKENS, int(limit * 0.9)) if limit > 0 else _SOFT_BUDGET_TOKENS
        if total > soft:
            target = int(soft * 0.7)
            for i in range(1, len(self.messages) - _KEEP_RECENT_MSGS):
                if total <= target:
                    break
                m = self.messages[i]
                c = m.get("content")
                if m.get("role") == "tool" and isinstance(c, str) \
                        and len(c) > _TOOL_KEEP_CHARS and not c.endswith("已省略）"):
                    total -= _est_tokens_msg(m) - 12
                    m["content"] = _TRIM_PLACEHOLDER
        if limit > 0 and total > int(limit * 0.9):
            for i in range(1, len(self.messages) - 6):
                m = self.messages[i]
                c = m.get("content")
                if m.get("role") == "tool" and isinstance(c, str) \
                        and len(c) > 100 and not c.endswith("已省略）"):
                    total -= _est_tokens_msg(m) - 12
                    m["content"] = _TRIM_PLACEHOLDER
            log.info("上下文逼近模型窗口（limit=%d tokens），已强制裁剪早期工具结果", limit)

    @staticmethod
    def _finish_response(payload: dict) -> AgentResponse:
        return finish_response(payload)

    # ---- OpenAI 兼容协议 ----

    def _payload_openai(self, use_tools: bool) -> dict:
        payload: dict = {
            "model": self.cfg.model,
            "messages": self.messages,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        if self.cfg.enable_thinking:
            # 开关打开时才带上思考参数：Qwen/通义系认 enable_thinking，
            # GLM 系认 thinking 对象；严格校验未知字段的服务（如 OpenAI 官方）
            # 对不支持思考的模型请勿开此开关
            payload["enable_thinking"] = True
            payload["thinking"] = {"type": "enabled"}
        if use_tools:
            payload["tools"] = tool_definitions_openai(self._enabled_tools)
        return payload

    def _step_openai(self, use_tools: bool) -> tuple[dict, list[tuple[str, str, str]], str]:
        data = self._post(
            self.cfg.base_url.rstrip("/") + "/chat/completions",
            self._payload_openai(use_tools),
            _auth_headers("openai", self.cfg.api_key),
        )
        self._acc_usage(data, "openai")
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"OpenAI 响应结构异常：{json.dumps(data, ensure_ascii=False)[:500]}") from e
        content = msg.get("content") or ""
        # 推理模型的思考内容：DeepSeek/GLM 等走 reasoning_content，个别服务叫 reasoning
        reasoning = str(msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            tool_calls.append((tc.get("id") or f"call_{len(tool_calls)}",
                               fn.get("name", ""), fn.get("arguments") or "{}"))
        if not content and not tool_calls and not reasoning:
            raise LLMError("大模型返回了空内容")
        assistant_dict: dict = {"role": "assistant", "content": content or None}
        if tool_calls:
            assistant_dict["tool_calls"] = [
                {
                    "id": cid,
                    "type": "function",
                    "function": {"name": name, "arguments": args},
                }
                for cid, name, args in tool_calls
            ]
        return assistant_dict, tool_calls, reasoning

    # ---- Anthropic 协议 ----

    def _payload_anthropic(self, use_tools: bool) -> dict:
        system_parts = [m["content"] for m in self.messages if m["role"] == "system"]
        rest = [m for m in self.messages if m["role"] != "system"]
        # OpenAI 形态的历史转 Anthropic 形态
        converted: list[dict] = []
        for m in rest:
            if m["role"] == "user":
                converted.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                blocks = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m.get("tool_calls") or []:
                    fn = tc["function"]
                    try:
                        args = json.loads(fn["arguments"])
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    blocks.append({"type": "tool_use", "id": tc["id"],
                                   "name": fn["name"], "input": args})
                if blocks:
                    converted.append({"role": "assistant", "content": blocks})
            elif m["role"] == "tool":
                converted.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": m["tool_call_id"],
                                 "content": m["content"]}],
                })
        payload: dict = {
            "model": self.cfg.model,
            "system": "\n\n".join(system_parts),
            "messages": converted,
            "max_tokens": self.cfg.max_tokens,
            "temperature": self.cfg.temperature,
        }
        if self.cfg.enable_thinking:
            # extended thinking 必须显式开启，模型才会返回 thinking 块；
            # 官方约束：budget_tokens ≥ 1024 且 < max_tokens，temperature 固定 1
            # （直接省略 temperature，API 默认即为 1）
            if self.cfg.max_tokens < 2048:
                raise LLMError("开启模型思考需要 max_tokens ≥ 2048（思考预留至少 1024）")
            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": max(1024, self.cfg.max_tokens // 2),
            }
            del payload["temperature"]
        if use_tools:
            payload["tools"] = tool_definitions_anthropic(self._enabled_tools)
        return payload

    def _step_anthropic(self, use_tools: bool) -> tuple[dict, list[tuple[str, str, str]], str]:
        data = self._post(
            _anthropic_base(self.cfg.base_url) + "/messages",
            self._payload_anthropic(use_tools),
            _auth_headers("anthropic", self.cfg.api_key),
        )
        self._acc_usage(data, "anthropic")
        try:
            blocks = data["content"]
        except (KeyError, TypeError) as e:
            raise LLMError(f"Anthropic 响应结构异常：{json.dumps(data, ensure_ascii=False)[:500]}") from e
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        # 扩展思考开启时（模型支持的前提下），thinking 块里是模型的思考过程
        reasoning = "".join(b.get("thinking", "") for b in blocks
                            if b.get("type") == "thinking").strip()
        tool_calls = [
            (b.get("id") or f"call_{i}", b.get("name", ""),
             json.dumps(b.get("input") or {}, ensure_ascii=False))
            for i, b in enumerate(blocks) if b.get("type") == "tool_use"
        ]
        if not text and not tool_calls and not reasoning:
            raise LLMError("大模型返回了空内容")
        assistant_dict: dict = {"role": "assistant", "content": text or None}
        if tool_calls:
            assistant_dict["tool_calls"] = [
                {
                    "id": cid,
                    "type": "function",
                    "function": {"name": name, "arguments": args},
                }
                for cid, name, args in tool_calls
            ]
        return assistant_dict, tool_calls, reasoning

    # ---- HTTP ----

    def _request_timeout(self):
        """httpx 超时值：timeout_seconds <= 0（约定 -1）= 不超时，无限等待响应。"""
        return None if self.cfg.timeout_seconds <= 0 else self.cfg.timeout_seconds

    def _timeout_error(self, e: Exception, attempts: int = 1) -> LLMError:
        # 可行动指引：思考型模型 + 长上下文时单轮 300s 常不够，用户需要
        # 知道去哪儿调、任务怎么恢复，而不是只看到一个裸的 timed out
        retried = f"（含自动重试共 {attempts} 次尝试）" if attempts > 1 else ""
        return LLMError(
            f"请求大模型超时：{self.cfg.timeout_seconds} 秒内未返回。{retried}"
            "思考型模型/长上下文单轮生成常需更久，可在看板设置页「大模型接入」"
            "把「超时秒数」调大（如 600~900；-1 = 不超时，挂起时靠「强制停止」"
            "打断）后重试；本任务会被标为 blocked，重置为待办即可按原优先级重跑"
            "（已完成的进度不受影响）。")

    def _post(self, url: str, payload: dict, headers: dict) -> dict:
        # 出站脱敏（数据脱敏唯一网络出口）：只动 payload 副本不碰 self.messages
        # ——本地历史保留原文（护栏计数、上下文裁剪依赖原文），重发历史幂等；
        # 关闭总开关时零开销透传。日志只记命中次数，绝不记脱敏内容
        if masking.enabled():
            payload, hits = self._masking.mask_payload(payload)
            if hits:
                log.debug("数据脱敏：本次请求掩除 %d 处敏感信息", hits)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers = {"User-Agent": USER_AGENT, **headers,
                       "Content-Type": "application/json"}
        timeout = self._request_timeout()
        log.debug("LLM 请求 %s model=%s msgs=%d timeout=%s",
                  url, self.cfg.model, len(self.messages), timeout)
        stop = self._stop_event
        max_attempts = self.retry_attempts
        backoff = self.retry_backoff
        # 厂商配额池（进程级共享）：许可只在「HTTP 请求在途」期间持有。主 Agent
        # 派子代理时手上没有许可（工具执行阶段已 release），因此父/子共用一个
        # 池不会死锁；多个项目并行时也共享同一预算，不会各限各的
        budget = get_budget(self.cfg.base_url, self.cfg.api_key, self.cfg.model)
        for attempt in range(1, max_attempts + 1):
            is_last = attempt == max_attempts
            server_delay: float | None = None
            # 池满/静默期在这里排队（可被强制停止秒级打断），而不是直接失败
            if not budget.acquire(stop):
                raise TaskStopped()
            try:
                try:
                    if stop is not None:
                        resp = self._post_stop_aware(url, body, req_headers,
                                                     timeout, stop)
                    else:
                        resp = httpx.post(url, content=body, headers=req_headers,
                                          timeout=timeout)
                except httpx.TimeoutException as e:
                    if is_last:
                        raise self._timeout_error(e, attempt) from e
                    reason = f"请求超时（timeout={timeout}）"
                except httpx.TransportError as e:
                    if is_last:
                        raise LLMError(
                            f"请求大模型失败：{e}（已自动重试 {attempt - 1} 次）") from e
                    reason = f"网络传输错误：{e}"
                except httpx.HTTPError as e:
                    # 非传输类 HTTP 层异常（重定向过多等）：重试无意义，保持旧行为
                    raise LLMError(f"请求大模型失败：{e}") from e
                else:
                    if resp.status_code == 200:
                        budget.on_success()
                        try:
                            return resp.json()
                        except ValueError as e:
                            raise LLMError(
                                f"大模型返回非 JSON：{resp.text[:500]}") from e
                    # 厂商建议等待时长（Retry-After / *-reset-*），没有则 None
                    server_delay = _parse_server_delay(resp)
                    if resp.status_code in _RETRYABLE_STATUS:
                        if resp.status_code in _RATE_LIMIT_STATUS:
                            # 真限流：收缩并发预算 + 全池静默，避免几个子代理
                            # 同时醒来再撞一次
                            budget.on_rate_limited(server_delay)
                        if is_last:
                            suffix = (f"（已自动重试 {attempt - 1} 次）"
                                      if attempt > 1 else "")
                            raise LLMError(
                                f"大模型返回 HTTP {resp.status_code}："
                                f"{resp.text[:500]}{suffix}")
                        err_msg = resp.text[:200].strip().replace("\n", " ")
                        reason = (f"HTTP {resp.status_code}: {err_msg}"
                                  if err_msg else f"HTTP {resp.status_code}")
                    else:
                        suffix = (f"（已自动重试 {attempt - 1} 次）"
                                  if attempt > 1 else "")
                        raise LLMError(f"大模型返回 HTTP {resp.status_code}："
                                       f"{resp.text[:500]}{suffix}")
            finally:
                # 先释放再退避：撞墙的请求不该占着名额干等
                budget.release()
            delay = backoff[min(attempt - 1, len(backoff) - 1)]
            if server_delay is not None:
                # 厂商给了窗口就按它来（至少不早于本地退避），避免白撞
                delay = max(delay, server_delay)
            log.warning("LLM 请求第 %d/%d 次失败（%s），%.0f 秒后重试（并发上限 %d）",
                        attempt, max_attempts, reason, delay, budget.limit)
            self._sleep_stop_aware(delay, stop)
        raise LLMError("unreachable")  # pragma: no cover — 循环必经 return/raise

    @staticmethod
    def _sleep_stop_aware(seconds: float, stop) -> None:
        """重试退避等待：每 0.2s 看一眼停止信号，「强制停止」秒级生效。"""
        if stop is None:
            time.sleep(seconds)
            return
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if stop.is_set():
                raise TaskStopped()
            time.sleep(0.2)

    def _post_stop_aware(self, url: str, body: bytes, headers: dict,
                         timeout, stop) -> "httpx.Response":
        """子线程发请求，主线程 0.2s 轮询强制停止信号。

        不超时（-1）时请求可能无限挂起，停止信号必须能打断等待；有限超时
        也顺带受益——「强制停止」秒级生效，而不是最长等满 timeout_seconds。
        打断后子线程（daemon）自生自灭：聊天完成是只读请求，服务端即使
        生成完也无人消费，零副作用。
        """
        out: dict = {}

        def worker() -> None:
            try:
                out["resp"] = httpx.post(url, content=body, headers=headers,
                                         timeout=timeout)
            except BaseException as e:  # noqa: BLE001 — 带回主线程按类型处理
                out["err"] = e

        t = threading.Thread(target=worker, daemon=True, name="llm-request")
        t.start()
        while True:
            if stop.is_set():
                raise TaskStopped()
            t.join(0.2)
            if not t.is_alive():
                break
        if "err" in out:
            # 原样抛回主线程：超时/传输错误的「重试 + 文案」统一收敛在 _post
            raise out["err"]
        return out["resp"]
