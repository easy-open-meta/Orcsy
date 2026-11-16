"""统一出口白名单（V1-R6）：把「应用自身发起的 HTTP」收敛到一个阀门并留审计。

**范围（重要）**：Agent 的 `run_command` 可以启动任意子进程（`npm install`、
`pip`、`git fetch`），进程级网络隔离不可能由应用层保证。本模块只承诺：

1. 应用自身用 httpx / urllib 发起的请求全部经本模块判定：
   模型调用（`llm.py`）、Issue 平台（`issues.py`）、镜像测速（`mirrors.py`）、
   网页抓取（`tools_net.py`）、更新检查与更新包下载（`updater.py`）；
2. 白名单 = 配置里显式出现的模型 / Issue / 镜像 / 更新源主机 +
   `egress.extra_hosts`；模型、Issue、镜像等改配置后自动进入白名单，
   不需要用户重复抄一遍主机名；
3. 非白名单拒绝并写审计（`logs/audit.jsonl`，action=`egress.deny`）；
4. 子进程出网不拦截——`env` 注入的代理/镜像与 run 事件里的命令名仍可审计。

**回环地址**（`127.0.0.1` / `localhost` / `::1`）不是「出网」：本机 dev_server
就绪探针、浏览器 CDP、测试桩都走这里，永远放行。

`mode: off` 仅用于排障（全放行、不写拒绝审计）；默认 `allowlist`。
"""

from __future__ import annotations

import ipaddress
import re
import threading
import time
from urllib.parse import urlparse

from .logging_setup import get_logger

log = get_logger("egress")

MODE_ALLOWLIST = "allowlist"
MODE_OFF = "off"
MODES: tuple[str, ...] = (MODE_ALLOWLIST, MODE_OFF)
DEFAULT_MODE = MODE_ALLOWLIST

_LOCAL_HOSTS = frozenset({"localhost"})
_MAX_LAST_DENIED = 10
_HOST_RE = re.compile(r"^(\*\.)?[a-z0-9_:.\-\[\]]+$", re.IGNORECASE)


class EgressDenied(Exception):
    """出网请求被白名单拒绝（消息面向用户，可直接展示）。"""

    def __init__(self, message: str, *, host: str = "", purpose: str = "") -> None:
        super().__init__(message)
        self.host = host
        self.purpose = purpose


def normalize_host(value: str) -> str:
    """主机名归一：小写、去空白、去末尾点。URL 也接受（取 hostname）。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        try:
            return (urlparse(raw).hostname or "").lower().rstrip(".")
        except ValueError:
            return ""
    # 裸 host[:port]：去掉端口（IPv6 的 [] 原样保留在 host 里由调用方处理）
    if raw.startswith("["):
        end = raw.find("]")
        return raw[:end + 1].lower() if end > 0 else raw.lower()
    if raw.count(":") == 1:
        host, _, port = raw.partition(":")
        if port.isdigit():
            raw = host
    return raw.lower().rstrip(".")


def _effective_port(url: str) -> int | None:
    try:
        u = urlparse(url)
    except ValueError:
        return None
    try:
        if u.port:
            return u.port
    except ValueError:
        return None
    if u.scheme == "https":
        return 443
    if u.scheme == "http":
        return 80
    return None


def _is_loopback(host: str) -> bool:
    if not host:
        return False
    if host in _LOCAL_HOSTS:
        return True
    candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _parse_rule(entry: str) -> tuple[str, int | None, bool]:
    """解析白名单条目 → (host, port|None, wildcard)。

    支持：`api.example.com`、`api.example.com:8443`、`*.example.com`
    （通配同时匹配主域与子域）、`[::1]:8080`。非法条目抛 ValueError。
    """
    raw = str(entry or "").strip().lower()
    if not raw or any(ch in raw for ch in ("/", "\\", "@", "?", "#", " ")):
        raise ValueError(f"非法的出网白名单条目：{entry!r}（只填主机名，可带端口）")
    wildcard = raw.startswith("*.")
    hostpart = raw[2:] if wildcard else raw
    port: int | None = None
    if hostpart.startswith("["):
        end = hostpart.find("]")
        if end < 0:
            raise ValueError(f"IPv6 主机缺少右括号：{entry!r}")
        host = hostpart[:end + 1]
        rest = hostpart[end + 1:]
        if rest:
            if not rest.startswith(":") or not rest[1:].isdigit():
                raise ValueError(f"非法的出网白名单条目：{entry!r}")
            port = int(rest[1:])
    elif hostpart.count(":") == 1:
        host, _, port_s = hostpart.partition(":")
        if not port_s.isdigit():
            raise ValueError(f"非法的出网白名单条目：{entry!r}")
        port = int(port_s)
    else:
        host = hostpart
    if not host or not _HOST_RE.fullmatch(("*." if wildcard else "") + host):
        raise ValueError(f"非法的出网白名单条目：{entry!r}")
    if port is not None and not (1 <= port <= 65535):
        raise ValueError(f"出网白名单端口越界：{entry!r}")
    return host, port, wildcard


def validate_extra_hosts(value: object, *, context: str = "egress.extra_hosts") -> list[str]:
    """校验配置里的额外白名单：字符串（逗号分隔）或列表，返回去重后的条目。

    只接受主机名/通配主机名，可带端口；带 scheme/路径的 URL 直接拒绝——
    用户想粘 URL 时给出明确提示（粘 URL 会让端口匹配语义含糊）。
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        items = [x.strip() for x in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = [str(x).strip() for x in value]
    else:
        raise ValueError(f"{context} 必须是列表或逗号分隔字符串")
    out: list[str] = []
    seen: set[str] = set()
    for entry in items:
        if not entry:
            continue
        if "://" in entry:
            raise ValueError(
                f"{context} 只填主机名，不要带 http(s)://：{entry}（如 api.example.com）")
        try:
            _parse_rule(entry)
        except ValueError as e:
            raise ValueError(f"{context}：{e}") from e
        key = entry.lower()
        if key not in seen:
            seen.add(key)
            out.append(entry)
    return out


class EgressPolicy:
    """一份不可变的白名单策略 + 可追加的运行时条目（更新包 CDN 等）。"""
    def __init__(self, *, mode: str = DEFAULT_MODE,
                 extra_hosts: list[str] | tuple[str, ...] = (),
                 derived_hosts: list[str] | tuple[str, ...] = (),
                 audit: bool = True) -> None:
        self.mode = mode if mode in MODES else DEFAULT_MODE
        self.audit = bool(audit)
        self.extra_hosts = [str(h).strip() for h in extra_hosts if str(h).strip()]
        self.derived_hosts = sorted({normalize_host(h) for h in derived_hosts
                                     if normalize_host(h)})
        self._rules: list[tuple[str, int | None, bool, str]] = []
        for source, entries in (("derived", self.derived_hosts),
                                ("extra", self.extra_hosts)):
            for entry in entries:
                try:
                    host, port, wildcard = _parse_rule(entry)
                except ValueError as e:
                    log.warning("忽略无效的出网白名单条目（%s）：%s", source, e)
                    continue
                self._rules.append((host, port, wildcard, source))
        self._lock = threading.Lock()
        self._allowed = 0
        self._denied = 0
        self._last_denied: list[dict] = []
        self._runtime: list[dict] = []

    # ---- 判定 ----

    def evaluate(self, url: str, *, purpose: str = "") -> dict:
        """返回 {allowed, host, rule, reason, purpose}，不抛异常、不写审计。"""
        host = normalize_host(url)
        if self.mode == MODE_OFF:
            return {"allowed": True, "host": host, "rule": "off",
                    "reason": "egress.mode=off（排障模式，全部放行）",
                    "purpose": purpose}
        if _is_loopback(host):
            return {"allowed": True, "host": host, "rule": "local",
                    "reason": "本机回环地址不是出网", "purpose": purpose}
        port = _effective_port(url)
        rule = self._match(host, port) if host else ""
        if rule:
            return {"allowed": True, "host": host, "rule": rule,
                    "reason": "命中白名单", "purpose": purpose}
        return {"allowed": False, "host": host, "rule": "",
                "reason": "不在出网白名单", "purpose": purpose}

    def _match(self, host: str, port: int | None) -> str:
        for rule_host, rule_port, wildcard, source in self._rules:
            if rule_port is not None and port != rule_port:
                continue
            if host == rule_host:
                return source
            if wildcard and host.endswith("." + rule_host):
                return source
        return ""

    def check(self, url: str, *, purpose: str = "") -> dict:
        """放行返回 decision；拒绝写审计后抛 `EgressDenied`。"""
        decision = self.evaluate(url, purpose=purpose)
        if decision["allowed"]:
            with self._lock:
                self._allowed += 1
            return decision
        with self._lock:
            self._denied += 1
            self._last_denied.append({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "host": decision["host"],
                "purpose": purpose,
            })
            if len(self._last_denied) > _MAX_LAST_DENIED:
                del self._last_denied[:-_MAX_LAST_DENIED]
        self._audit_deny(url, decision, purpose)
        raise EgressDenied(self._deny_message(decision, purpose),
                           host=decision["host"], purpose=purpose)

    def _audit_deny(self, url: str, decision: dict, purpose: str) -> None:
        if not self.audit:
            return
        try:
            from .audit import record as audit_record
            audit_record(
                "egress.deny", actor="system:egress", result="denied",
                target=decision["host"],
                detail={"purpose": purpose, "url": _safe_url(url),
                        "mode": self.mode,
                        "hint": "config.yaml egress.extra_hosts / 设置页出网白名单"})
        except Exception:  # noqa: BLE001 — 审计是旁路，绝不改变判定结果
            log.debug("出网拒绝审计写入失败（忽略）", exc_info=True)

    def _deny_message(self, decision: dict, purpose: str) -> str:
        host = decision["host"] or "(无法解析主机)"
        return (
            f"出网白名单拦截：{host} 不在允许列表"
            f"{f'（用途：{purpose}）' if purpose else ''}。\n"
            "应用自身发起的 HTTP 只允许：配置中的模型 / Issue / 镜像 / 更新源主机，"
            "以及 egress.extra_hosts 显式追加的主机。\n"
            f"如需放行，请在「设置 → 出网白名单」或 config.yaml 的 "
            f"egress.extra_hosts 添加 {host}（可带端口，如 {host}:8443；"
            "子域用 *.example.com）；排障可临时把 egress.mode 设为 off。")

    # ---- 运行时条目（更新包 CDN 等，来自已放行的发布 API 响应）----

    def allow_runtime_host(self, value: str, *, purpose: str = "") -> str:
        raw = str(value or "").strip().lower()
        host = normalize_host(value)
        if not host or _is_loopback(host):
            return host
        if "://" in raw:  # 传 URL 时只取 host[:port]
            try:
                u = urlparse(raw)
                raw = host + (f":{u.port}" if u.port else "")
            except ValueError:
                raw = host
        try:
            rule_host, port, wildcard = _parse_rule(raw)
        except ValueError:
            return ""
        with self._lock:
            known = any(rh == rule_host and rp == port for rh, rp, _w, _s in self._rules)
            added = not known
            if added:
                display = rule_host + (f":{port}" if port else "")
                self._rules.append((rule_host, port, wildcard, "runtime"))
                self._runtime.append({"host": display, "purpose": purpose,
                                      "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
        if not added:
            return host
        try:
            from .audit import record as audit_record
            audit_record("egress.runtime_allow", actor="system:egress",
                         target=host, detail={"purpose": purpose})
        except Exception:  # noqa: BLE001
            pass
        return host

    def snapshot(self) -> dict:
        with self._lock:
            denied = self._denied
            allowed = self._allowed
            last = [dict(x) for x in self._last_denied]
            runtime = [dict(x) for x in self._runtime]
        return {
            "mode": self.mode,
            "audit": self.audit,
            "extra_hosts": list(self.extra_hosts),
            "derived_hosts": list(self.derived_hosts),
            "runtime_hosts": runtime,
            "allowed": allowed,
            "denied": denied,
            "last_denied": last,
        }


def _safe_url(url: str) -> str:
    """审计里只留 scheme://host/path：query 可能带 access_token，不能落盘。"""
    try:
        u = urlparse(str(url))
    except ValueError:
        return ""
    if not u.hostname:
        return ""
    path = u.path if len(u.path) <= 120 else u.path[:120] + "…"
    return f"{u.scheme}://{u.hostname}{path}"


# ---- 模块级单例：应用启动时按配置注入，各出网点只调用 check() ----

_POLICY: EgressPolicy | None = None
_POLICY_LOCK = threading.Lock()
_RUNTIME: list[dict] = []   # 跨 init 保留：更新包 CDN 在本次进程内始终可信


def _collect_urls(config: object) -> list[str]:
    """从配置推导白名单 URL（models / issues / mirrors / update）。"""
    urls: list[str] = []

    def add(value: object) -> None:
        text = str(value or "").strip()
        if text.startswith(("http://", "https://")):
            urls.append(text)

    def add_llm(llm: object) -> None:
        if llm is None:
            return
        if isinstance(llm, dict):
            add(llm.get("base_url"))
        else:
            add(getattr(llm, "base_url", ""))

    add_llm(getattr(config, "llm", None))
    review = getattr(config, "review", None)
    add_llm(getattr(review, "llm", None))
    for profile in (getattr(config, "agent_profiles", None) or {}).values():
        add_llm(getattr(profile, "llm", None))
    for project in getattr(config, "projects", None) or []:
        add_llm(getattr(project, "llm", None))
        team = getattr(project, "team", None)
        for member in (getattr(team, "members", None) or []):
            add_llm(getattr(member, "llm", None))
        repo = getattr(project, "repo", None) or {}
        if isinstance(repo, dict):
            add(repo.get("url"))
            add(repo.get("api_base"))
    add(getattr(getattr(config, "update", None), "repo", ""))
    try:
        from .mirrors import BUILTIN_MIRRORS
        for mirrors in BUILTIN_MIRRORS.values():
            for mirror in mirrors:
                add(mirror.url)
                add(mirror.test_url)
    except Exception:  # noqa: BLE001 — 配置推导失败不应阻止启动
        log.debug("镜像源白名单推导失败（忽略）", exc_info=True)
    mirrors_cfg = getattr(config, "mirrors", None)
    for urls_list in (getattr(mirrors_cfg, "custom", None) or {}).values():
        for value in urls_list or []:
            add(value)
    for value in (getattr(mirrors_cfg, "disabled", None) or []):
        add(value)
    return urls


def init_egress(config: object = None) -> EgressPolicy:
    """按配置重建白名单策略；应用启动与设置保存后调用（同配置重建结果一致）。"""
    global _POLICY
    egress_cfg = getattr(config, "egress", None)
    mode = str(getattr(egress_cfg, "mode", DEFAULT_MODE) or DEFAULT_MODE)
    extra = list(getattr(egress_cfg, "extra_hosts", None) or [])
    audit = bool(getattr(egress_cfg, "audit", True))
    policy = EgressPolicy(mode=mode, extra_hosts=extra,
                          derived_hosts=_collect_urls(config), audit=audit)
    with _POLICY_LOCK:
        if _RUNTIME:
            policy._runtime.extend(_RUNTIME)
            for item in _RUNTIME:
                try:
                    host, port, wildcard = _parse_rule(item["host"])
                except ValueError:
                    continue
                if not policy._match(host, None):
                    policy._rules.append((host, port, wildcard, "runtime"))
        _POLICY = policy
    return policy


def get_egress() -> EgressPolicy:
    """取当前策略；未初始化时返回 off 策略（库调用/测试的兼容默认）。

    应用进程（`cmd_run` / `cmd_web`）都会在 Scheduler/AppState 构造时初始化，
    因此正常运行不会落到这个兜底。
    """
    global _POLICY
    with _POLICY_LOCK:
        if _POLICY is None:
            _POLICY = EgressPolicy(mode=MODE_OFF, audit=False)
        return _POLICY


def check(url: str, *, purpose: str = "") -> dict:
    """各出网点统一入口：放行返回判定，拒绝写审计并抛 EgressDenied。"""
    return get_egress().check(url, purpose=purpose)


def allow_runtime_host(value: str, *, purpose: str = "") -> str:
    """把已放行来源（更新发布 API 响应）给出的下载主机加入本次进程白名单。"""
    global _RUNTIME
    policy = get_egress()
    host = policy.allow_runtime_host(value, purpose=purpose)
    if host:
        with _POLICY_LOCK:
            if not any(item.get("host") == host for item in _RUNTIME):
                _RUNTIME.append({"host": host, "purpose": purpose,
                                 "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
    return host


def snapshot() -> dict:
    """看板/接口用的只读快照（不含任何密钥）。"""
    return get_egress().snapshot()


def reset_for_tests() -> None:
    """测试专用：清空单例与运行时条目。"""
    global _POLICY, _RUNTIME
    with _POLICY_LOCK:
        _POLICY = None
        _RUNTIME = []


__all__ = [
    "DEFAULT_MODE",
    "MODES",
    "EgressDenied",
    "EgressPolicy",
    "allow_runtime_host",
    "check",
    "get_egress",
    "init_egress",
    "normalize_host",
    "reset_for_tests",
    "snapshot",
]
