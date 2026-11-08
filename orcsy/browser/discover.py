"""Chromium 系列浏览器（Chrome / Edge）探测、拉起与临时 Profile 管理。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..tools import (_NO_WINDOW, _kill_process_tree, _windows_startupinfo,
                     register_child_proc, unregister_child_proc)

log = get_logger("browser.discover")

# 常见 Windows 安装路径
_WIN_CHROME_PATHS = (
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
)

_WIN_EDGE_PATHS = (
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
)


def _check_win_reg(app_name: str) -> str | None:
    """在 Windows 注册表 App Paths 中查找可执行文件。"""
    if os.name != "nt":
        return None
    try:
        import winreg
        subkey = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{app_name}"
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, subkey) as k:
                    val, _ = winreg.QueryValueEx(k, "")
                    if val and os.path.isfile(val):
                        return val
            except OSError:
                pass
    except Exception:
        pass
    return None


def _find_chrome() -> str | None:
    """寻找 Chrome 可执行文件。"""
    if os.name == "nt":
        reg = _check_win_reg("chrome.exe")
        if reg:
            return reg
        for raw in _WIN_CHROME_PATHS:
            p = os.path.expandvars(raw)
            if os.path.isfile(p):
                return p
    elif sys_platform := os.uname().sysname if hasattr(os, "uname") else "":
        if sys_platform == "Darwin":
            mac_p = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            if os.path.isfile(mac_p):
                return mac_p
        else:
            for bin_name in ("google-chrome", "chromium", "chromium-browser", "google-chrome-stable"):
                found = shutil.which(bin_name)
                if found:
                    return found
    return shutil.which("chrome") or shutil.which("google-chrome")


def _find_edge() -> str | None:
    """寻找 Microsoft Edge 可执行文件。"""
    if os.name == "nt":
        reg = _check_win_reg("msedge.exe")
        if reg:
            return reg
        for raw in _WIN_EDGE_PATHS:
            p = os.path.expandvars(raw)
            if os.path.isfile(p):
                return p
    elif sys_platform := os.uname().sysname if hasattr(os, "uname") else "":
        if sys_platform == "Darwin":
            mac_p = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
            if os.path.isfile(mac_p):
                return mac_p
        else:
            for bin_name in ("microsoft-edge", "microsoft-edge-stable", "msedge"):
                found = shutil.which(bin_name)
                if found:
                    return found
    return shutil.which("msedge") or shutil.which("microsoft-edge")


def find_browser(browser_type: str = "auto", custom_path: str = "") -> tuple[str, str]:
    """定位可用浏览器路径与引擎类型。

    返回 (executable_path, "chrome" | "edge")。
    若找不到则抛出包含可行动指引的 FileNotFoundError。
    """
    if custom_path:
        p = Path(custom_path).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"配置的自定义浏览器路径不存在：{custom_path}")
        engine = "edge" if "edge" in p.name.lower() else "chrome"
        return str(p), engine

    b_type = (browser_type or "auto").strip().lower()
    if b_type == "chrome":
        found = _find_chrome()
        if found:
            return found, "chrome"
        raise FileNotFoundError(
            "未检测到 Google Chrome 浏览器。请安装 Chrome 或在 config.yaml 的 local_debug 节点中配置 browser_executable 路径；"
            "若当前系统仅有 Edge，可将 browser 设置为 auto 或 edge。"
        )

    if b_type == "edge":
        found = _find_edge()
        if found:
            return found, "edge"
        raise FileNotFoundError(
            "未检测到 Microsoft Edge 浏览器。请安装 Edge 或在 config.yaml 的 local_debug 节点中配置 browser_executable 路径。"
        )

    # auto：优先 Chrome，若无则回退 Edge
    found_chrome = _find_chrome()
    if found_chrome:
        return found_chrome, "chrome"
    found_edge = _find_edge()
    if found_edge:
        return found_edge, "edge"

    raise FileNotFoundError(
        "未在系统中检测到任何 Chromium 系列浏览器（Chrome / Edge）。"
        "如需使用浏览器调试能力，请安装 Chrome 或 Edge，或在 local_debug 配置中显式指定路径；"
        "若当前任务无需浏览器操作，请在项目或全局配置的 tools 节关闭 browser。"
    )


def launch_browser(executable_path: str, *, headless: bool = True,
                   user_data_dir: Path | None = None) -> tuple[subprocess.Popen, int, str, Path]:
    """启动一个独立的 Chromium 子进程并等待 DevTools 端口就绪。

    返回 (proc, port, ws_debugger_url, profile_dir)。
    """
    profile_dir = user_data_dir or Path(tempfile.mkdtemp(prefix="orcsy-browser-profile-")).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        executable_path,
        "--remote-debugging-port=0",
        f"--user-data-dir={profile_dir}",
        "--enable-automation",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-translate",
        "--disable-default-apps",
        "--disable-features=Translate,OptimizationHints,MediaRouter",
        "--mute-audio",
    ]

    if headless:
        cmd.append("--headless=new")

    # 容器环境安全兼容
    if os.path.exists("/.dockerenv"):
        cmd.extend(["--no-sandbox", "--disable-dev-shm-usage"])

    cmd.append("about:blank")

    log.info("拉起调试浏览器 (headless=%s): %s", headless, executable_path)

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        startupinfo=_windows_startupinfo(),
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | _NO_WINDOW,
    )
    register_child_proc(proc)

    # 等待 DevToolsActivePort 文件生成
    port_file = profile_dir / "DevToolsActivePort"
    port: int | None = None
    browser_path = ""
    deadline = time.monotonic() + 15.0

    while time.monotonic() < deadline:
        if port_file.is_file():
            try:
                lines = port_file.read_text(encoding="utf-8").splitlines()
                if lines and lines[0].strip().isdigit():
                    port = int(lines[0].strip())
                    browser_path = lines[1].strip() if len(lines) > 1 else ""
                    break
            except Exception:
                pass

        # 仅当启动入口进程发生非 0 错误（如配置错误、崩溃）时提前报错；
        # 在 Windows 上 Chromium 入口 stub 启动后台主进程后返回 0 退出属正常机制
        if proc.poll() is not None and proc.returncode != 0:
            stderr_out = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            unregister_child_proc(proc)
            shutil.rmtree(profile_dir, ignore_errors=True)
            raise RuntimeError(f"浏览器进程启动后异常退出（退出码 {proc.returncode}）：{stderr_out[:500]}")

        time.sleep(0.1)

    if port is None:
        unregister_child_proc(proc)
        _kill_process_tree(proc)
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise TimeoutError("浏览器启动超时：未能在 15 秒内生成 DevTools 调试端口")

    ws_debugger_url = f"ws://127.0.0.1:{port}{browser_path}"
    log.info("浏览器调试端口就绪 (port=%d, ws=%s)", port, ws_debugger_url)
    return proc, port, ws_debugger_url, profile_dir


def terminate_browser(proc: subprocess.Popen, *, ws_url: str | None = None,
                      profile_dir: Path | None = None) -> None:
    """终止浏览器实例并清理临时 profile 目录。"""
    log.info("正在关闭调试浏览器 (pid=%s)...", proc.pid)

    # 1. 尝试通过 WebSocket 发送优雅退出命令
    if ws_url and proc.poll() is None:
        try:
            from websockets.sync.client import connect
            import json
            with connect(ws_url, open_timeout=1.5, close_timeout=1.5) as ws:
                ws.send(json.dumps({"id": 999999, "method": "Browser.close"}))
                try:
                    ws.recv()
                except Exception:
                    pass
        except Exception:
            pass

    # 2. 进程退出等待与进程树终结
    try:
        proc.wait(timeout=2.0)
    except Exception:
        _kill_process_tree(proc)

    unregister_child_proc(proc)

    # 3. 临时 Profile 目录清理（带重试等待 Windows 句柄平滑释放，处理只读属性）
    if profile_dir and profile_dir.is_dir():
        import stat

        def _on_error(fn, path, excinfo):
            try:
                os.chmod(path, stat.S_IWRITE)
                fn(path)
            except Exception:
                pass

        for _ in range(12):
            try:
                if sys.version_info >= (3, 12):
                    shutil.rmtree(profile_dir, onexc=lambda fn, p, exc: _on_error(fn, p, exc))
                else:
                    shutil.rmtree(profile_dir, onerror=_on_error)
                break
            except Exception:
                time.sleep(0.25)
        else:
            log.warning("清理临时浏览器目录失败（可能稍后自动清理）：%s", profile_dir)
