"""本地调试工具集：驱动本地浏览器、管理开发服务与调试本地 API。

包含三大组共 14 个工具：
- dev_server 组（3 个）：dev_server_start, dev_server_logs, dev_server_stop
- http_request（1 个）：本地与内网 API 测试
- browser 组（10 个）：browser_navigate, browser_snapshot, browser_click,
  browser_type, browser_wait, browser_screenshot, browser_console,
  browser_network, browser_eval, browser_close
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from .hosts import assert_host_allowed, extract_host_and_port
from .logging_setup import get_logger

if TYPE_CHECKING:
    from .tools import ToolContext

log = get_logger("tools_debug")

MAX_HTTP_RESPONSE_CHARS = 8000
MAX_REDIRECT_HOPS = 5
REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))


def _ensure_session(ctx: ToolContext) -> object:
    session = getattr(ctx, "session", None)
    if session is None:
        raise RuntimeError("当前任务未初始化调试会话（未开启本地调试或测试环境未注入 session）")
    return session


# ---- 1. dev_server 系列工具 ----

def tool_dev_server_start(ctx: ToolContext, args: dict) -> str:
    session = _ensure_session(ctx)
    name = str(args.get("name", "")).strip()
    command = args.get("command", [])
    cwd_rel = str(args.get("cwd", ".")).strip()
    ready_url = str(args.get("ready_url", "")).strip()
    ready_timeout = int(args.get("ready_timeout_seconds", 60) or 60)
    custom_env = args.get("env")
    if custom_env is not None and not isinstance(custom_env, dict):
        return "错误：env 必须是键值对字典"

    try:
        res = session.services.start(
            name,
            command,
            cwd_rel=cwd_rel,
            ready_url=ready_url,
            ready_timeout_seconds=ready_timeout,
            custom_env=custom_env,
            stop_event=ctx.stop_event,
        )
        return (
            f"服务 '{res['name']}' 已成功在后台启动：\n"
            f"- 进程 PID：{res['pid']}\n"
            f"- 分配端口：{res['port']}\n"
            f"- 基础地址：{res['base_url']}\n"
            f"- 服务状态：已就绪（可调用 dev_server_logs 查看实时日志或用 http_request 调试）"
        )
    except Exception as e:
        return f"错误：启动服务失败：{e}"


def tool_dev_server_logs(ctx: ToolContext, args: dict) -> str:
    session = _ensure_session(ctx)
    name = str(args.get("name", "")).strip()
    if not name:
        return "错误：name 不能为空"
    tail = int(args.get("tail", 80) or 80)
    since_last = bool(args.get("since_last", False))
    return session.services.logs(name, tail=tail, since_last=since_last)


def tool_dev_server_stop(ctx: ToolContext, args: dict) -> str:
    session = _ensure_session(ctx)
    name = args.get("name")
    if name is not None:
        name = str(name).strip() or None
    return session.services.stop(name)


# ---- 2. http_request 本地 API 调试工具 ----

def tool_http_request(ctx: ToolContext, args: dict) -> str:
    session = _ensure_session(ctx)
    raw_url = str(args.get("url", "")).strip()
    if not raw_url:
        return "错误：url 不能为空"

    method = str(args.get("method", "GET")).strip().upper()
    allowed_methods = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
    if method not in allowed_methods:
        return f"错误：不支持的 HTTP 方法 '{method}'（允许：{', '.join(sorted(allowed_methods))}）"

    parsed = urlparse(raw_url)
    if parsed.scheme not in ("http", "https"):
        return f"错误：仅支持 http 或 https 协议（当前：{parsed.scheme or '空'}）"

    headers = args.get("headers")
    if headers is not None and not isinstance(headers, dict):
        return "错误：headers 必须是键值对字典"
    clean_headers = {str(k): str(v) for k, v in (headers or {}).items()}

    json_data = args.get("json")
    body_data = args.get("body")
    if json_data is not None and body_data is not None:
        return "错误：json 与 body 不能同时提供"

    timeout = min(int(args.get("timeout_seconds", 20) or 20), 120)
    follow_redirects = bool(args.get("follow_redirects", True))

    import httpx

    client: httpx.Client = session.http_client
    effective_hosts = session.effective_hosts

    # 循环重定向防护：每一跳必须重新过 assert_host_allowed
    current_url = raw_url
    for hop in range(MAX_REDIRECT_HOPS):
        host, _ = extract_host_and_port(current_url)
        try:
            assert_host_allowed(host, effective_hosts)
        except ValueError as e:
            return f"错误：白名单安全拦截：{e}"

        t0 = time.monotonic()
        try:
            # 仅第一跳带 body，重定向跳转通常为 GET（303 等标准语义）
            req_kwargs: dict[str, Any] = {
                "headers": clean_headers,
                "timeout": timeout,
            }
            if hop == 0:
                if json_data is not None:
                    req_kwargs["json"] = json_data
                elif body_data is not None:
                    req_kwargs["content"] = str(body_data).encode("utf-8")

            resp = client.request(method if hop == 0 else "GET", current_url, **req_kwargs)
            duration_ms = int((time.monotonic() - t0) * 1000)
        except httpx.TimeoutException:
            return f"错误：请求超时（>{timeout}s，目标：{current_url}）"
        except httpx.RequestError as e:
            return f"错误：请求失败：{e}"

        # 检查是否需要重定向
        if follow_redirects and resp.status_code in REDIRECT_STATUSES:
            loc = (resp.headers.get("location") or "").strip()
            if not loc:
                break
            current_url = urljoin(current_url, loc)
            next_scheme = urlparse(current_url).scheme
            if next_scheme not in ("http", "https"):
                return f"错误：重定向到不支持的协议 '{next_scheme}'"
            continue
        break
    else:
        return f"错误：重定向次数超过上限（>{MAX_REDIRECT_HOPS} 跳）"

    # 格式化响应输出
    status_line = f"[HTTP {resp.status_code} {resp.reason_phrase} · {duration_ms}ms]"

    # 提取关键响应头
    shown_headers = []
    for h in ("content-type", "location", "set-cookie"):
        val = resp.headers.get(h)
        if val:
            if h == "set-cookie":
                # 只暴露 cookie 名，防止大模型将敏感 session token 直接回显
                cookie_names = [c.split("=")[0].strip() for c in val.split(";") if "=" in c]
                shown_headers.append(f"Set-Cookie: {', '.join(cookie_names[:5])}")
            else:
                shown_headers.append(f"{h.title()}: {val[:120]}")

    for k, v in resp.headers.items():
        if k.lower().startswith("x-") and len(shown_headers) < 6:
            shown_headers.append(f"{k}: {v[:120]}")

    head_section = "\n".join(shown_headers)

    # 响应 Body
    content_type = (resp.headers.get("content-type") or "").lower()
    body_text = ""
    is_binary = not any(
        t in content_type for t in ("text/", "application/json", "application/xml", "application/javascript")
    )

    if is_binary and resp.content:
        body_text = f"（二进制响应，共 {len(resp.content)} 字节）"
    else:
        try:
            body_text = resp.text
            # 若是 JSON 则美化排版
            if "application/json" in content_type or (body_text.strip().startswith(("{", "["))):
                try:
                    parsed_json = resp.json()
                    body_text = json.dumps(parsed_json, ensure_ascii=False, indent=2)
                except Exception:
                    pass
        except Exception as e:
            body_text = f"（正文解码失败：{e}）"

    if len(body_text) > MAX_HTTP_RESPONSE_CHARS:
        orig_len = len(body_text)
        body_text = (
            body_text[:MAX_HTTP_RESPONSE_CHARS]
            + f"\n\n…（正文过长已截断，前 {MAX_HTTP_RESPONSE_CHARS} / 共 {orig_len} 字符）"
        )

    out = f"{status_line}\n"
    if head_section:
        out += f"{head_section}\n\n"
    else:
        out += "\n"
    out += body_text if body_text else "（空正文）"
    return out


# ---- 3. browser 系列工具 ----

def tool_browser_navigate(ctx: ToolContext, args: dict) -> str:
    session = _ensure_session(ctx)
    try:
        return session.browser_navigate(args)
    except Exception as e:
        return f"错误：浏览器导航失败：{e}"


def tool_browser_snapshot(ctx: ToolContext, args: dict) -> str:
    session = _ensure_session(ctx)
    try:
        return session.browser_snapshot(args)
    except Exception as e:
        return f"错误：获取页面快照失败：{e}"


def tool_browser_click(ctx: ToolContext, args: dict) -> str:
    session = _ensure_session(ctx)
    try:
        return session.browser_click(args)
    except Exception as e:
        return f"错误：点击操作失败：{e}"


def tool_browser_type(ctx: ToolContext, args: dict) -> str:
    session = _ensure_session(ctx)
    try:
        return session.browser_type(args)
    except Exception as e:
        return f"错误：输入操作失败：{e}"


def tool_browser_wait(ctx: ToolContext, args: dict) -> str:
    session = _ensure_session(ctx)
    try:
        return session.browser_wait(args)
    except Exception as e:
        return f"错误：等待操作失败：{e}"


def tool_browser_screenshot(ctx: ToolContext, args: dict) -> str:
    session = _ensure_session(ctx)
    try:
        return session.browser_screenshot(args)
    except Exception as e:
        return f"错误：截图失败：{e}"


def tool_browser_console(ctx: ToolContext, args: dict) -> str:
    session = _ensure_session(ctx)
    try:
        return session.browser_console(args)
    except Exception as e:
        return f"错误：获取控制台日志失败：{e}"


def tool_browser_network(ctx: ToolContext, args: dict) -> str:
    session = _ensure_session(ctx)
    try:
        return session.browser_network(args)
    except Exception as e:
        return f"错误：获取网络请求记录失败：{e}"


def tool_browser_eval(ctx: ToolContext, args: dict) -> str:
    session = _ensure_session(ctx)
    try:
        return session.browser_eval(args)
    except Exception as e:
        return f"错误：JavaScript 执行失败：{e}"


def tool_browser_close(ctx: ToolContext, args: dict) -> str:
    session = _ensure_session(ctx)
    try:
        return session.browser_close(args)
    except Exception as e:
        return f"错误：关闭浏览器失败：{e}"
        return session.browser_close(args)
    return "浏览器已关闭"


# ---- 4. 工具定义注册表 ----

DEBUG_TOOLS: dict[str, tuple[str, dict, object]] = {
    "dev_server_start": (
        "在后台启动一个本地开发/测试服务（如 npm run dev、python app.py），支持自动分配端口。"
        "命令或参数中可使用 {PORT} 占位符，系统会自动替换为分配的端口并在环境变量中注入 PORT。"
        "可配置 ready_url（如 http://127.0.0.1:{PORT}/health）等待服务就绪。",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "服务标识名，如 web、api"},
                "command": {"type": "array", "items": {"type": "string"},
                            "description": "启动命令参数列表，支持 {PORT} 占位符，如 [\"npm\", \"run\", \"dev\", \"--\", \"--port\", \"{PORT}\"]"},
                "cwd": {"type": "string", "description": "工作目录（相对项目根，默认 .）"},
                "ready_url": {"type": "string", "description": "可选健康探测 URL，支持 {PORT}，如 http://127.0.0.1:{PORT}/"},
                "ready_timeout_seconds": {"type": "integer", "description": "就绪超时（秒，默认 60）"},
                "env": {"type": "object", "description": "可选额外注入的环境变量键值对"},
            },
            "required": ["name", "command"],
        },
        tool_dev_server_start,
    ),
    "dev_server_logs": (
        "读取指定后台开发服务的最新标准输出与错误日志，用于排查服务运行状态或错误堆栈。",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "服务名称"},
                "tail": {"type": "integer", "description": "返回最新日志行数，默认 80"},
                "since_last": {"type": "boolean", "description": "是否仅返回自上次读取以来的新增日志，默认 false"},
            },
            "required": ["name"],
        },
        tool_dev_server_logs,
    ),
    "dev_server_stop": (
        "停止后台运行的开发服务。建议在测试任务交付前停止启动的服务；若未停止，任务结束时系统也会自动清理。",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "要停止的服务名称，留空则停止所有后台服务"},
            },
            "required": [],
        },
        tool_dev_server_stop,
    ),
    "http_request": (
        "对本地或内网开发服务发起 HTTP 请求，调试 REST/GraphQL 接口或测试服务状态。"
        "受主机白名单限制（默认仅允许 localhost / 127.0.0.1 等调试主机），支持方法、请求头、JSON/文本 Body 及 Cookie 会话保持。",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "请求目标 URL，如 http://127.0.0.1:8080/api/v1/users"},
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                           "description": "HTTP 请求方法，默认 GET"},
                "headers": {"type": "object", "description": "请求头键值对字典"},
                "json": {"type": "object", "description": "JSON 请求体（会自动设置 Content-Type 为 application/json）"},
                "body": {"type": "string", "description": "原始文本请求体"},
                "timeout_seconds": {"type": "integer", "description": "请求超时秒数，默认 20"},
                "follow_redirects": {"type": "boolean", "description": "是否自动跟随重定向（每一跳均过白名单检查），默认 true"},
            },
            "required": ["url"],
        },
        tool_http_request,
    ),
    "browser_navigate": (
        "驱动本地浏览器导航至指定 URL（仅放行白名单内的本地/内网调试服务），"
        "若浏览器尚未启动则自动拉起独立临时环境浏览器。",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要导航的目标页面 URL，如 http://127.0.0.1:3000/login"},
                "wait_until": {"type": "string", "enum": ["load", "networkidle"], "description": "等待页面加载完成的条件，默认 load"},
                "browser": {"type": "string", "enum": ["chrome", "edge"], "description": "可选指定本次调用的浏览器引擎（默认跟随全局设置）"},
            },
            "required": ["url"],
        },
        tool_browser_navigate,
    ),
    "browser_snapshot": (
        "获取当前页面的结构化文本快照树，列出视口内的可见元素及其引用标识（如 [e12] button \"登录\"）。"
        "后续点击、输入等操作均直接使用元素 ref 标识。建议在任何页面交互前后调用它查看页面最新状态。",
        {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "可选指定某个容器元素的 ref，仅查看该元素及其子树"},
                "max_chars": {"type": "integer", "description": "文本上限字符数，默认 6000"},
            },
            "required": [],
        },
        tool_browser_snapshot,
    ),
    "browser_click": (
        "点击页面上的指定元素。优先使用 browser_snapshot 中返回的元素 ref（如 \"e12\"）；也可传 CSS 选择器。",
        {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "元素 ref 标识（如 \"e12\"）"},
                "selector": {"type": "string", "description": "CSS 选择器（ref 与 selector 二选一）"},
                "double": {"type": "boolean", "description": "是否为双击，默认 false"},
            },
            "required": [],
        },
        tool_browser_click,
    ),
    "browser_type": (
        "在页面上的输入框或可编辑元素中输入文本。支持清空已有内容，或在输入后自动触发回车提交。",
        {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "目标元素 ref 标识（如 \"e13\"）"},
                "text": {"type": "string", "description": "要输入的文本内容"},
                "clear": {"type": "boolean", "description": "输入前是否先清空输入框，默认 true"},
                "submit": {"type": "boolean", "description": "输入后是否按下 Enter 键，默认 false"},
            },
            "required": ["text"],
        },
        tool_browser_type,
    ),
    "browser_wait": (
        "等待页面发生预期变化，如指定文本出现、选择器元素就绪或 URL 发生跳转。",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "等待出现的页面文本内容"},
                "selector": {"type": "string", "description": "等待出现的 CSS 选择器"},
                "url_contains": {"type": "string", "description": "等待 URL 包含特定片段"},
                "timeout_seconds": {"type": "integer", "description": "最大等待超时秒数，默认 15"},
            },
            "required": [],
        },
        tool_browser_wait,
    ),
    "browser_screenshot": (
        "截取当前页面并保存为 PNG 图片至工件目录（仅供人工查验留证，模型自身无法直接解析图片）。"
        "返回图片在磁盘上的保存路径与宽高尺寸。",
        {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "可选仅截取指定元素区域"},
                "full_page": {"type": "boolean", "description": "是否截取整个滚动长页面，默认 false"},
            },
            "required": [],
        },
        tool_browser_screenshot,
    ),
    "browser_console": (
        "获取浏览器在页面执行过程中捕获的 Console 日志、前端警告与未捕获的 JavaScript 异常。",
        {
            "type": "object",
            "properties": {
                "level": {"type": "string", "enum": ["all", "error", "warn", "info"], "description": "过滤日志级别，默认 all"},
                "clear": {"type": "boolean", "description": "读取后是否清空已捕获日志缓冲，默认 false"},
            },
            "required": [],
        },
        tool_browser_console,
    ),
    "browser_network": (
        "获取浏览器捕获的 HTTP 网络请求历史列表，用于排查前端接口调用失败（4xx/5xx）或资源加载问题。"
        "支持传 reqid 查看特定请求的响应体前 4KB 内容。",
        {
            "type": "object",
            "properties": {
                "failed_only": {"type": "boolean", "description": "是否只列出失败（4xx、5xx 或网络中断）的请求，默认 false"},
                "url_contains": {"type": "string", "description": "按 URL 片段过滤请求"},
                "reqid": {"type": "string", "description": "查看指定网络请求的详细响应正文"},
            },
            "required": [],
        },
        tool_browser_network,
    ),
    "browser_eval": (
        "在当前页面的全局执行上下文中运行 JavaScript 表达式，并返回执行结果的序列化字符串（如读取前端全局变量、localStorage 等）。",
        {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "要执行的 JavaScript 表达式，如 \"localStorage.getItem('token')\""},
            },
            "required": ["expression"],
        },
        tool_browser_eval,
    ),
    "browser_close": (
        "显式关闭当前浏览器实例并销毁临时会话。任务结束时系统会自动关闭，无需必须调用。",
        {
            "type": "object",
            "properties": {},
            "required": [],
        },
        tool_browser_close,
    ),
}
