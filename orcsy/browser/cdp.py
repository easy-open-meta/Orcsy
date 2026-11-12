"""纯 Python 实现的轻量同步 CDP（Chrome DevTools Protocol）客户端。

基于 websockets 同步协议与线程模型，实现命令配对、事件监听、
顶层导航白名单拦截与高层自动化操作封装。
"""

from __future__ import annotations

import base64
import json
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from websockets.sync.client import ClientConnection, connect

from ..hosts import check_host_allowed, extract_host_and_port
from ..logging_setup import get_logger
from .page_script import BUILD_SNAPSHOT_SCRIPT, FOCUS_AND_CLEAR_SCRIPT, GET_ELEMENT_BOUNDS_SCRIPT

log = get_logger("browser.cdp")


class CdpClient:
    """与单页面 Target 交互的同步 CDP 客户端。"""

    def __init__(self, ws_url: str, *, port: int, allowed_hosts: list[str],
                 stop_event: object | None = None) -> None:
        self.ws_url = ws_url
        self.port = port
        self.allowed_hosts = list(allowed_hosts)
        self.stop_event = stop_event

        self._ws: ClientConnection | None = None
        self._id_counter = 0
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._closed = False

        # 事件缓冲区
        self.console_events: list[dict[str, Any]] = []
        self.network_events: list[dict[str, Any]] = []
        self.page_loaded_event = threading.Event()
        self.recv_thread: threading.Thread | None = None

    def connect(self) -> None:
        """建立 WebSocket 连接并初始化后台接收线程与域设置。"""
        log.info("连接 CDP WebSocket: %s", self.ws_url)
        self._ws = connect(self.ws_url, open_timeout=10.0)
        self.recv_thread = threading.Thread(target=self._recv_loop, name="cdp-recv", daemon=True)
        self.recv_thread.start()

        # 启用所需 CDP 域
        self.send("Page.enable")
        self.send("Runtime.enable")
        self.send("Log.enable")
        self.send("Network.enable")
        # 启用网络拦截（前置安全护栏）
        try:
            self.send("Fetch.enable", {"patterns": [{"requestStage": "Request"}]})
        except Exception as e:
            log.warning("启用 Fetch 域网络拦截失败：%s", e)

    def _recv_loop(self) -> None:
        """后台消息接收分发循环。"""
        while not self._closed and self._ws:
            try:
                raw = self._ws.recv()
                if not raw:
                    break
                msg = json.loads(raw)
            except Exception:
                break

            # 1. 命令响应匹配
            msg_id = msg.get("id")
            if msg_id is not None:
                with self._lock:
                    pending = self._pending.get(msg_id)
                if pending:
                    event, container = pending
                    container.update(msg)
                    event.set()
                continue

            # 2. 事件处理
            method = msg.get("method", "")
            params = msg.get("params", {})

            if method == "Page.loadEventFired":
                self.page_loaded_event.set()

            elif method == "Fetch.requestPaused":
                # 前置网络安全审查
                self._handle_fetch_request_paused(params)

            elif method in ("Runtime.consoleAPICalled", "Log.entryAdded", "Runtime.exceptionThrown"):
                self._handle_console_event(method, params)

            elif method.startswith("Network."):
                self._handle_network_event(method, params)

    def _handle_fetch_request_paused(self, params: dict[str, Any]) -> None:
        """审查网络请求；对顶层主 Frame 导航强制核验白名单，子资源直接放行。"""
        request_id = params.get("requestId")
        if not request_id:
            return
        req = params.get("request", {})
        url = req.get("url", "")
        resource_type = params.get("resourceType", "")

        # 仅针对主页面文档导航（Document）进行白名单严格审查
        if resource_type == "Document":
            parsed = urlparse(url)
            # 允许 about:blank
            if url == "about:blank" or (parsed.scheme == "about" and parsed.path == "blank"):
                self._async_send("Fetch.continueRequest", {"requestId": request_id})
                return

            if parsed.scheme not in ("http", "https"):
                log.warning("拦截非法协议导航: %s", url)
                self._async_send("Fetch.failRequest", {"requestId": request_id, "errorReason": "AccessDenied"})
                return

            host, _ = extract_host_and_port(url)
            ok, reason = check_host_allowed(host, self.allowed_hosts)
            if not ok:
                log.warning("拦截非白名单导航: %s (原因: %s)", url, reason)
                self._async_send("Fetch.failRequest", {"requestId": request_id, "errorReason": "AccessDenied"})
                return

        # 页面子资源（CSS/JS/图片/Fetch/XHR）直接放行，确保页面依赖正常加载
        self._async_send("Fetch.continueRequest", {"requestId": request_id})

    def _handle_console_event(self, method: str, params: dict[str, Any]) -> None:
        """收集控制台与日志信息。"""
        entry: dict[str, Any] = {"time": time.time()}
        if method == "Runtime.consoleAPICalled":
            entry["level"] = params.get("type", "log")
            args = params.get("args", [])
            text_parts = []
            for a in args:
                if "value" in a:
                    text_parts.append(str(a["value"]))
                elif "description" in a:
                    text_parts.append(str(a["description"]))
                else:
                    text_parts.append(str(a))
            entry["text"] = " ".join(text_parts)
        elif method == "Log.entryAdded":
            le = params.get("entry", {})
            entry["level"] = le.get("level", "info")
            entry["text"] = le.get("text", "")
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails", {})
            exc = details.get("exception", {})
            entry["level"] = "error"
            entry["text"] = details.get("text", "") or exc.get("description", "Uncaught Exception")

        with self._lock:
            self.console_events.append(entry)
            if len(self.console_events) > 200:
                self.console_events.pop(0)

    def _handle_network_event(self, method: str, params: dict[str, Any]) -> None:
        """收集网络请求生命周期事件。"""
        req_id = params.get("requestId", "")
        with self._lock:
            if method == "Network.requestWillBeSent":
                req = params.get("request", {})
                self.network_events.append({
                    "id": req_id,
                    "url": req.get("url", ""),
                    "method": req.get("method", "GET"),
                    "status": 0,
                    "failed": False,
                    "time": time.time(),
                })
            elif method == "Network.responseReceived":
                resp = params.get("response", {})
                for item in reversed(self.network_events):
                    if item["id"] == req_id:
                        item["status"] = resp.get("status", 0)
                        item["type"] = resp.get("mimeType", "")
                        break
            elif method == "Network.loadingFailed":
                for item in reversed(self.network_events):
                    if item["id"] == req_id:
                        item["failed"] = True
                        item["error"] = params.get("errorText", "")
                        break
            if len(self.network_events) > 200:
                self.network_events.pop(0)

    def _async_send(self, method: str, params: dict[str, Any] | None = None) -> None:
        """非阻塞发送协议命令（用于事件应答）。"""
        if self._closed or not self._ws:
            return
        with self._lock:
            self._id_counter += 1
            call_id = self._id_counter
        payload = json.dumps({"id": call_id, "method": method, "params": params or {}})
        try:
            self._ws.send(payload)
        except Exception:
            pass

    def send(self, method: str, params: dict[str, Any] | None = None,
             timeout: float = 30.0) -> dict[str, Any]:
        """向浏览器同步发送一条 CDP 请求并等待返回。"""
        if self._closed or not self._ws:
            raise RuntimeError("CDP 客户端连接已关闭")

        with self._lock:
            self._id_counter += 1
            call_id = self._id_counter
            event = threading.Event()
            resp_box: dict[str, Any] = {}
            self._pending[call_id] = (event, resp_box)

        payload = json.dumps({"id": call_id, "method": method, "params": params or {}})
        self._ws.send(payload)

        # 带切片等待，支持响应强停信号
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.stop_event is not None and getattr(self.stop_event, "is_set", lambda: False)():
                with self._lock:
                    self._pending.pop(call_id, None)
                from ..tools import TaskStopped
                raise TaskStopped()
            if event.wait(timeout=0.2):
                break
        else:
            with self._lock:
                self._pending.pop(call_id, None)
            raise TimeoutError(f"CDP 请求超时（>{timeout}s，方法：{method}）")

        with self._lock:
            self._pending.pop(call_id, None)

        if "error" in resp_box:
            err = resp_box["error"]
            raise RuntimeError(f"CDP 命令失败 ({method}): {err.get('message')} (code={err.get('code')})")

        return resp_box.get("result", {})

    # ---- 业务操作封装 ----

    def navigate(self, url: str, wait_until: str = "load", timeout: int = 30) -> str:
        """导航至指定 URL。"""
        clean_url = str(url or "").strip()
        parsed = urlparse(clean_url)
        if parsed.scheme not in ("http", "https", "about"):
            raise ValueError(f"仅支持 http/https 协议（当前：{parsed.scheme or '空'}）")

        if clean_url != "about:blank":
            host, _ = extract_host_and_port(clean_url)
            ok, reason = check_host_allowed(host, self.allowed_hosts)
            if not ok:
                raise ValueError(f"白名单安全拦截：{reason}")

        self.page_loaded_event.clear()
        res = self.send("Page.navigate", {"url": clean_url}, timeout=timeout)
        if "errorText" in res:
            raise RuntimeError(f"导航失败：{res['errorText']}")

        # 等待加载
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.stop_event is not None and getattr(self.stop_event, "is_set", lambda: False)():
                from ..tools import TaskStopped
                raise TaskStopped()
            if self.page_loaded_event.wait(timeout=0.2):
                break
            # 同时也检查 readyState
            try:
                doc_state = self.evaluate("document.readyState")
                if doc_state in ("interactive", "complete"):
                    break
            except Exception:
                pass

        time.sleep(0.3)  # 给予前端框架水合与渲染留出缓冲
        doc_title = self.evaluate("document.title") or ""
        return f"已成功导航至 {clean_url}\n页面标题：{doc_title or '（无标题）'}"

    def evaluate(self, expression: str, timeout: float = 10.0) -> Any:
        """在页面当前执行上下文求值。"""
        res = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
            timeout=timeout,
        )
        exception_details = res.get("exceptionDetails")
        if exception_details:
            exc = exception_details.get("exception", {})
            raise RuntimeError(f"页面 JavaScript 执行错误：{exc.get('description') or exception_details.get('text')}")
        val = res.get("result", {})
        return val.get("value")

    def snapshot(self, root_ref: str | None = None, max_chars: int = 6000) -> str:
        """生成并返回结构化文本快照树。"""
        opts = json.dumps({"rootRef": root_ref or "", "maxChars": max_chars})
        expr = f"({BUILD_SNAPSHOT_SCRIPT})({opts})"
        data = self.evaluate(expr)
        if isinstance(data, dict):
            return data.get("text", "")
        return str(data or "")

    def click(self, ref: str | None = None, selector: str | None = None,
              double: bool = False) -> str:
        """定位元素并派发真实鼠标点击。"""
        target = ref or selector
        if not target:
            return "错误：必须指定 ref 或 selector"

        expr = f"({GET_ELEMENT_BOUNDS_SCRIPT})({json.dumps(target)})"
        bounds = self.evaluate(expr)
        if not bounds:
            return f"错误：未在页面上找到元素 '{target}'（请先调用 browser_snapshot 查看最新元素树）"

        x, y = bounds["x"], bounds["y"]
        click_count = 2 if double else 1

        self.send("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y,
            "button": "left", "clickCount": click_count
        })
        self.send("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y,
            "button": "left", "clickCount": click_count
        })

        time.sleep(0.3)
        return f"已成功点击元素 '{target}' (位于坐标 [{int(x)}, {int(y)}])"

    def type_text(self, text: str, ref: str | None = None,
                  clear: bool = True, submit: bool = False) -> str:
        """在目标元素中输入文本。"""
        if ref:
            expr = f"({FOCUS_AND_CLEAR_SCRIPT})({json.dumps(ref)}, {json.dumps(clear)})"
            focused = self.evaluate(expr)
            if not focused:
                return f"错误：未找到目标输入元素 '{ref}'（请先调用 browser_snapshot 查看最新元素树）"
        elif clear:
            # 无 ref 时对当前激活元素尝试清空
            self.evaluate("if (document.activeElement && 'value' in document.activeElement) document.activeElement.value = '';")

        # 逐字符或整体插入文本
        self.send("Input.insertText", {"text": text})

        if submit:
            # 回车键
            self.send("Input.dispatchKeyEvent", {
                "type": "rawKeyDown", "windowsVirtualKeyCode": 13,
                "unmodifiedText": "\r", "text": "\r", "key": "Enter", "code": "Enter"
            })
            self.send("Input.dispatchKeyEvent", {
                "type": "keyUp", "windowsVirtualKeyCode": 13,
                "key": "Enter", "code": "Enter"
            })

        time.sleep(0.2)
        target_desc = f"元素 '{ref}'" if ref else "当前聚焦元素"
        return f"已在 {target_desc} 中输入文本（{len(text)} 字符）{'并回车提交' if submit else ''}"

    def wait_condition(self, text: str | None = None, selector: str | None = None,
                       url_contains: str | None = None, timeout_seconds: int = 15) -> str:
        """条件轮询等待。"""
        if not text and not selector and not url_contains:
            return "错误：必须指定 text、selector 或 url_contains 至少一个等待条件"

        deadline = time.monotonic() + max(1, timeout_seconds)
        while time.monotonic() < deadline:
            if self.stop_event is not None and getattr(self.stop_event, "is_set", lambda: False)():
                from ..tools import TaskStopped
                raise TaskStopped()

            # 检查条件
            if url_contains:
                curr_url = self.evaluate("window.location.href") or ""
                if url_contains in curr_url:
                    return f"等待达成：URL 已包含 '{url_contains}'"

            if selector:
                exists = self.evaluate(f"!!document.querySelector({json.dumps(selector)})")
                if exists:
                    return f"等待达成：已匹配选择器 '{selector}'"

            if text:
                has_text = self.evaluate(f"document.body.innerText.includes({json.dumps(text)})")
                if has_text:
                    return f"等待达成：页面已包含文本 '{text}'"

            time.sleep(0.3)

        # 超时返回当前页面概要，便于大模型分析调整
        curr_url = self.evaluate("window.location.href") or ""
        curr_title = self.evaluate("document.title") or ""
        return (
            f"错误：等待条件超时（>{timeout_seconds}s）。当前页面：\n"
            f"- URL: {curr_url}\n"
            f"- Title: {curr_title}\n"
            "建议调用 browser_snapshot 查看当前实际内容或检查服务控制台日志。"
        )

    def screenshot(self, ref: str | None = None, full_page: bool = False,
                   output_path: Path | None = None) -> str:
        """截取页面并保存为 PNG 图片，返回路径与尺寸。"""
        params: dict[str, Any] = {"format": "png", "captureBeyondViewport": full_page}
        if ref:
            expr = f"({GET_ELEMENT_BOUNDS_SCRIPT})({json.dumps(ref)})"
            bounds = self.evaluate(expr)
            if not bounds:
                return f"错误：未找到元素 '{ref}' 进行截图"
            params["clip"] = {
                "x": bounds["left"],
                "y": bounds["top"],
                "width": max(1, bounds["width"]),
                "height": max(1, bounds["height"]),
                "scale": 1,
            }

        res = self.send("Page.captureScreenshot", params, timeout=15.0)
        data_b64 = res.get("data", "")
        img_bytes = base64.b64decode(data_b64)

        if output_path is None:
            tmp = tempfile.NamedTemporaryFile("wb", suffix=".png", delete=False)
            output_path = Path(tmp.name)
            tmp.write(img_bytes)
            tmp.close()
        else:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(img_bytes)

        # 尝试使用 PIL 提取尺寸
        dims = ""
        try:
            import io

            from PIL import Image
            with Image.open(io.BytesIO(img_bytes)) as im:
                dims = f"（{im.width}x{im.height} 像素，{len(img_bytes) // 1024} KB）"
        except Exception:
            dims = f"（{len(img_bytes) // 1024} KB）"

        return (
            f"页面截图已成功保存至：{output_path} {dims}\n"
            "说明：模型自身无法直接解析图像内容，请调用 browser_snapshot 获取结构化文本分析页面。"
        )

    def console_logs(self, level: str = "all", clear: bool = False) -> str:
        """获取控制台日志。"""
        level = (level or "all").lower()
        with self._lock:
            if level == "all":
                logs = list(self.console_events)
            elif level == "error":
                logs = [e for e in self.console_events if e.get("level") in ("error", "exception")]
            elif level == "warn":
                logs = [e for e in self.console_events if e.get("level") in ("warn", "warning", "error")]
            else:
                logs = [e for e in self.console_events if e.get("level") == level]
            if clear:
                self.console_events.clear()

        if not logs:
            return "（暂无控制台日志）"

        lines = [f"[{e.get('level', 'log').upper()}] {e.get('text', '')}" for e in logs[-50:]]
        return "\n".join(lines)

    def network_requests(self, failed_only: bool = False,
                         url_contains: str | None = None,
                         reqid: str | None = None) -> str:
        """获取网络请求记录或查看指定响应体。"""
        if reqid:
            try:
                res = self.send("Network.getResponseBody", {"requestId": reqid}, timeout=5.0)
                body = res.get("body", "")
                if res.get("base64Encoded"):
                    try:
                        body = base64.b64decode(body).decode("utf-8", errors="replace")
                    except Exception:
                        body = "（二进制内容）"
                if len(body) > 4000:
                    body = body[:4000] + "\n…（响应体已截断至前 4000 字符）"
                return f"[请求 ID: {reqid} 响应正文]\n{body}"
            except Exception as e:
                return f"获取请求 '{reqid}' 响应体失败：{e}"

        with self._lock:
            items = list(self.network_events)

        if failed_only:
            items = [i for i in items if i.get("failed") or i.get("status", 0) >= 400]
        if url_contains:
            items = [i for i in items if url_contains in i.get("url", "")]

        if not items:
            return "（暂无网络请求记录）"

        lines = []
        for i in items[-30:]:
            status_desc = "FAIL" if i.get("failed") else (str(i.get("status")) if i.get("status") else "PENDING")
            lines.append(f"[{i['id']}] {i.get('method', 'GET')} {status_desc} {i.get('url', '')[:100]}")
        return "\n".join(lines)

    def close(self) -> None:
        """关闭连接。"""
        self._closed = True
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
