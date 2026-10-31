"""开机自启动：写当前用户的注册表 Run 键（HKCU\\...\\Run）。

托盘 exe 以 --tray 参数自启（无窗口静默后台）；取消自启即删键。
"""

from __future__ import annotations

import sys
from pathlib import Path

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "Orcsy"


def _tray_exe() -> str | None:
    """冻结目录下优先找专用托盘 exe。"""
    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable).parent / "orcsy-tray.exe"
        if candidate.exists():
            return str(candidate)
    return None


def _current_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    # 源码运行：退化到 python -m
    import orcsy.__main__ as m

    return f'"{sys.executable}" "{Path(m.__file__)}" --tray'


def is_enabled() -> bool:
    """查询是否已开启自启（非 Windows 一律 False，注册表仅 win32 存在）。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.QueryValueEx(k, _VALUE_NAME)
            return True
    except OSError:
        return False


def enable() -> bool:
    """开启自启，返回是否成功（非 Windows 返回 False）。"""
    if sys.platform != "win32":
        return False
    import winreg

    exe = _tray_exe()
    command = f'"{exe}" --tray' if exe else _current_command()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
        winreg.SetValueEx(k, _VALUE_NAME, 0, winreg.REG_SZ, command)
    return True


def disable() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, _VALUE_NAME)
        return True
    except FileNotFoundError:
        return False


def toggle() -> bool:
    """切换自启状态，返回切换后的状态。"""
    if is_enabled():
        disable()
        return False
    enable()
    return True
