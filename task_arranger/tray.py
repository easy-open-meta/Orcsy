"""系统托盘模式：看板服务在后台运行，托盘图标提供快捷操作。

菜单项：打开看板 / 立即扫描 / 开机自启动（勾选）/ 退出。
托盘图标状态：扫描执行中显示黄色点，空闲为默认蓝。
"""

from __future__ import annotations

import threading
import webbrowser
from pathlib import Path

from .logging_setup import get_logger

log = get_logger("tray")

# 托盘图标（生成自 build/icon 同款配色，内嵌 base64 避免 ICO 文件依赖）
from PIL import Image, ImageDraw

_ICON_CACHE: dict[str, "Image.Image"] = {}


def _make_icon(running: bool = False) -> "Image.Image":
    key = "running" if running else "idle"
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    S = 64
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, S - 2, S - 2), radius=14, fill=(15, 20, 32, 255))
    # 三列看板
    for x, color in ((12, (91, 140, 255, 255)), (26, (91, 140, 255, 180)), (40, (91, 140, 255, 120))):
        d.rounded_rectangle((x, 14, x + 9, 50), radius=3, fill=color)
    if running:
        d.ellipse((44, 6, 58, 20), fill=(232, 161, 60, 255))  # 黄点=执行中
    _ICON_CACHE[key] = img
    return img


def run_tray(state, scheduler, url: str) -> None:
    """阻塞运行托盘图标循环（在主线程调用）。state/scheduler 供菜单操作。"""
    import pystray

    from .autostart import is_enabled, toggle

    def open_dashboard(icon, item):
        webbrowser.open(url)

    def trigger_scan(icon, item):
        threading.Thread(target=scheduler.run_cycle, daemon=True).start()
        icon.notify("已触发一轮扫描", "任务编排系统")

    def toggle_autostart(icon, item):
        on = toggle()
        icon.notify("开机自启动已" + ("开启" if on else "关闭"), "任务编排系统")

    def quit_app(icon, item):
        log.info("托盘退出")
        icon.stop()
        scheduler.stop()
        # icon.run() 解除阻塞后，cmd_web 的 _cleanup 收尾（停 HTTP、收子进程）

    menu = pystray.Menu(
        pystray.MenuItem("打开看板", open_dashboard, default=True),
        pystray.MenuItem("立即扫描", trigger_scan),
        pystray.MenuItem("开机自启动", toggle_autostart, checked=lambda item: is_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )

    icon = pystray.Icon("task-arranger", _make_icon(False), "任务编排系统", menu)

    def _updater():
        """随调度状态换图标（执行中黄点）。"""
        import time

        last = None
        while icon.visible or True:
            try:
                running = scheduler.is_running
                if running != last:
                    last = running
                    icon.icon = _make_icon(running)
                    icon.title = "任务编排系统（扫描执行中…）" if running else "任务编排系统"
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2)

    threading.Thread(target=_updater, daemon=True).start()

    def _open_on_start():
        import time

        time.sleep(1.0)
        webbrowser.open(url)

    threading.Thread(target=_open_on_start, daemon=True).start()
    icon.run()
