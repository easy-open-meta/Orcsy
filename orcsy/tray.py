"""系统托盘图标：与控制台并存（由 cmd_web 放在后台线程启动）。

菜单项：显示主界面 / 立即扫描 / 开机自启动（勾选）/ 退出。
托盘图标状态：扫描执行中显示黄色点，空闲为默认蓝。
退出统一路径：菜单「退出」调 server.shutdown() 唤醒主线程的
serve_forever，由 cmd_web 的 _cleanup 统一收尾；Ctrl+C 退出时
cmd_web 反向调 stop_tray() 摘掉图标。
"""

from __future__ import annotations

import threading
import webbrowser
from pathlib import Path

from .logging_setup import get_logger

log = get_logger("tray")

# 托盘图标（生成自 build/icon 同款配色，内嵌绘制避免 ICO 文件依赖）
from PIL import Image, ImageDraw

# 运行中的 pystray.Icon 引用（pystray 延迟导入，用 object 规避类型依赖）
_ICON_HOLDER: dict[str, object] = {}

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


def stop_tray() -> None:
    """从任意线程摘掉托盘图标（Ctrl+C 退出路径调用）。未启动时静默跳过。"""
    icon = _ICON_HOLDER.get("icon")
    if icon is None:
        return
    try:
        icon.stop()
    except Exception:  # noqa: BLE001 图标摘不掉不影响退出收尾
        pass


def run_tray(state, scheduler, url: str, server=None) -> None:
    """阻塞运行托盘图标循环（由 cmd_web 放在后台线程调用）。

    server 用于「退出」菜单：shutdown() 让主线程 serve_forever 返回，
    收尾统一走 cmd_web 的 _cleanup，托盘不重复清理。
    """
    import pystray

    from .autostart import is_enabled, toggle

    def open_dashboard(icon, item):
        webbrowser.open(url)

    def trigger_scan(icon, item):
        threading.Thread(target=scheduler.run_cycle, daemon=True).start()
        icon.notify("已触发一轮扫描", "Orcsy")

    def toggle_autostart(icon, item):
        on = toggle()
        icon.notify("开机自启动已" + ("开启" if on else "关闭"), "Orcsy")

    def quit_app(icon, item):
        log.info("托盘退出")
        icon.stop()
        if server is not None:
            # 不能在图标线程直接等 shutdown()：主线程收尾时还要回到
            # 这里处理消息循环，丢到独立线程唤醒即可
            threading.Thread(target=server.shutdown, daemon=True).start()

    menu = pystray.Menu(
        pystray.MenuItem("显示主界面", open_dashboard, default=True),
        pystray.MenuItem("立即扫描", trigger_scan),
        pystray.MenuItem("开机自启动", toggle_autostart, checked=lambda item: is_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )

    icon = pystray.Icon("orcsy", _make_icon(False), "Orcsy", menu)
    _ICON_HOLDER["icon"] = icon

    def _updater():
        """随调度状态换图标（执行中黄点）。"""
        import time

        last = None
        while True:
            try:
                running = scheduler.is_running
                if running != last:
                    last = running
                    icon.icon = _make_icon(running)
                    icon.title = "Orcsy（扫描执行中…）" if running else "Orcsy"
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2)

    threading.Thread(target=_updater, daemon=True).start()

    icon.run()
    _ICON_HOLDER.pop("icon", None)
