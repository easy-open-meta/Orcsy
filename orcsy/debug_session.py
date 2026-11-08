"""任务级调试会话容器：统一管理开发服务、HTTP客户端与浏览器实例。

保证任务结束时无论是正常完成、审查不通过、LLM报错还是用户强制停止，
调用 close() 均能安全释放全部句柄、临时目录与子进程。
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from .browser.cdp import CdpClient
from .browser.discover import find_browser, launch_browser, terminate_browser
from .config import LocalDebugConfig
from .devserver import DevServerManager
from .logging_setup import get_logger

log = get_logger("debug_session")


class DebugSession:
    """一个任务执行期间的本地调试会话。"""

    def __init__(self, root: Path, *,
                 allowed_commands: list[str] | None = None,
                 local_debug: LocalDebugConfig | None = None,
                 effective_hosts: list[str] | None = None,
                 extra_env: dict[str, str] | None = None,
                 stop_event: object | None = None,
                 artifacts_dir: Path | None = None) -> None:
        self.root = root.resolve()
        self.allowed_commands = allowed_commands
        self.cfg = local_debug or LocalDebugConfig()
        self.effective_hosts = list(effective_hosts or self.cfg.allowed_hosts or ["localhost", "127.0.0.1", "::1"])
        self.extra_env = extra_env
        self.stop_event = stop_event
        self.artifacts_dir = artifacts_dir

        # 后台服务管理器
        self.services = DevServerManager(
            root=self.root,
            allowed_commands=self.allowed_commands,
            max_services=self.cfg.max_services,
            extra_env=self.extra_env,
        )

        # 会话级 HTTP 客户端（携带 cookie jar，禁用自动跟随以保证逐跳白名单核验）
        self._http_client: httpx.Client | None = None

        # 浏览器实例与 CDP 客户端
        self._browser_proc: subprocess.Popen | None = None
        self._browser_port: int | None = None
        self._browser_ws_url: str | None = None
        self._browser_profile_dir: Path | None = None
        self._cdp_client: CdpClient | None = None

        self._closed = False
        self._lock = threading.Lock()

    @property
    def http_client(self) -> httpx.Client:
        """获取或惰性创建会话级 HTTP 客户端。"""
        with self._lock:
            if self._closed:
                raise RuntimeError("调试会话已关闭")
            if self._http_client is None:
                self._http_client = httpx.Client(
                    follow_redirects=False,
                    timeout=httpx.Timeout(self.cfg.navigation_timeout_seconds),
                    headers={"User-Agent": "Mozilla/5.0 (compatible; OrcsyDebugAgent)"},
                )
            return self._http_client

    def get_or_create_browser(self, browser_engine: str | None = None) -> CdpClient:
        """获取或惰性拉起 Chromium 浏览器并连接至首个 Page Target。"""
        with self._lock:
            if self._closed:
                raise RuntimeError("调试会话已关闭")
            if self._cdp_client is not None:
                return self._cdp_client

            engine = browser_engine or self.cfg.browser
            exe_path, detected_engine = find_browser(engine, self.cfg.browser_executable)
            proc, port, ws_debugger_url, profile_dir = launch_browser(
                exe_path,
                headless=self.cfg.headless,
            )

            self._browser_proc = proc
            self._browser_port = port
            self._browser_ws_url = ws_debugger_url
            self._browser_profile_dir = profile_dir

            # 查询 Page 类型的 Target
            page_ws_url = None
            try:
                resp = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=3.0)
                targets = resp.json()
                for t in targets:
                    if t.get("type") == "page" and "webSocketDebuggerUrl" in t:
                        page_ws_url = t["webSocketDebuggerUrl"]
                        break
            except Exception:
                pass

            if not page_ws_url:
                page_ws_url = ws_debugger_url

            cdp = CdpClient(
                page_ws_url,
                port=port,
                allowed_hosts=self.effective_hosts,
                stop_event=self.stop_event,
            )
            cdp.connect()
            self._cdp_client = cdp
            return self._cdp_client

    # ---- 浏览器操作代理 ----

    def browser_navigate(self, args: dict) -> str:
        url = str(args.get("url", "")).strip()
        wait_until = str(args.get("wait_until", "load"))
        engine = args.get("browser")
        cdp = self.get_or_create_browser(engine)
        return cdp.navigate(url, wait_until=wait_until, timeout=self.cfg.navigation_timeout_seconds)

    def browser_snapshot(self, args: dict) -> str:
        cdp = self.get_or_create_browser()
        root_ref = args.get("ref")
        max_chars = int(args.get("max_chars", 6000) or 6000)
        return cdp.snapshot(root_ref=root_ref, max_chars=max_chars)

    def browser_click(self, args: dict) -> str:
        cdp = self.get_or_create_browser()
        ref = args.get("ref")
        selector = args.get("selector")
        double = bool(args.get("double", False))
        return cdp.click(ref=ref, selector=selector, double=double)

    def browser_type(self, args: dict) -> str:
        cdp = self.get_or_create_browser()
        text = str(args.get("text", ""))
        ref = args.get("ref")
        clear = bool(args.get("clear", True))
        submit = bool(args.get("submit", False))
        return cdp.type_text(text, ref=ref, clear=clear, submit=submit)

    def browser_wait(self, args: dict) -> str:
        cdp = self.get_or_create_browser()
        text = args.get("text")
        selector = args.get("selector")
        url_contains = args.get("url_contains")
        timeout = int(args.get("timeout_seconds", 15) or 15)
        return cdp.wait_condition(text=text, selector=selector, url_contains=url_contains, timeout_seconds=timeout)

    def browser_screenshot(self, args: dict) -> str:
        cdp = self.get_or_create_browser()
        ref = args.get("ref")
        full_page = bool(args.get("full_page", False))

        output_path = None
        if self.artifacts_dir:
            file_name = f"screenshot-{int(time.time() * 1000)}.png"
            output_path = self.artifacts_dir / file_name

        return cdp.screenshot(ref=ref, full_page=full_page, output_path=output_path)

    def browser_console(self, args: dict) -> str:
        cdp = self.get_or_create_browser()
        level = str(args.get("level", "all"))
        clear = bool(args.get("clear", False))
        return cdp.console_logs(level=level, clear=clear)

    def browser_network(self, args: dict) -> str:
        cdp = self.get_or_create_browser()
        failed_only = bool(args.get("failed_only", False))
        url_contains = args.get("url_contains")
        reqid = args.get("reqid")
        return cdp.network_requests(failed_only=failed_only, url_contains=url_contains, reqid=reqid)

    def browser_eval(self, args: dict) -> str:
        cdp = self.get_or_create_browser()
        expr = str(args.get("expression", "")).strip()
        if not expr:
            return "错误：expression 不能为空"
        val = cdp.evaluate(expr)
        try:
            return json.dumps(val, ensure_ascii=False, indent=2)
        except Exception:
            return str(val)

    def browser_close(self, args: dict | None = None) -> str:
        with self._lock:
            if self._cdp_client is not None:
                try:
                    self._cdp_client.send("Browser.close", timeout=2.0)
                except Exception:
                    pass
                try:
                    self._cdp_client.close()
                except Exception:
                    pass
                self._cdp_client = None

            if self._browser_proc is not None:
                terminate_browser(
                    self._browser_proc,
                    ws_url=self._browser_ws_url,
                    profile_dir=self._browser_profile_dir,
                )
                self._browser_proc = None
                self._browser_profile_dir = None
        return "浏览器实例已关闭并清理临时配置"

    def close(self) -> None:
        """幂等关闭整个调试会话，释放全部后台服务、浏览器和网络连接。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True

        log.info("正在清理调试会话资源 (root=%s)...", self.root)

        # 1. 关闭浏览器
        if self._cdp_client is not None:
            try:
                self._cdp_client.send("Browser.close", timeout=2.0)
            except Exception:
                pass
            try:
                self._cdp_client.close()
            except Exception:
                pass
            self._cdp_client = None

        if self._browser_proc is not None:
            try:
                terminate_browser(
                    self._browser_proc,
                    ws_url=self._browser_ws_url,
                    profile_dir=self._browser_profile_dir,
                )
            except Exception as e:
                log.warning("关闭调试浏览器异常：%s", e)
            self._browser_proc = None
            self._browser_profile_dir = None

        # 2. 终止全部后台服务
        try:
            self.services.close()
        except Exception as e:
            log.warning("清理后台服务异常：%s", e)

        # 3. 关闭 HTTP 客户端
        if self._http_client is not None:
            try:
                self._http_client.close()
            except Exception as e:
                log.warning("关闭 HTTP 客户端异常：%s", e)
            self._http_client = None

        log.info("调试会话清理完毕")
