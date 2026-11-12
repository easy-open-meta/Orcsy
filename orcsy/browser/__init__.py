"""浏览器调试引擎包（Chromium / CDP 直连）。"""

from __future__ import annotations

from .cdp import CdpClient
from .discover import find_browser, launch_browser, terminate_browser

__all__ = [
    "CdpClient",
    "find_browser",
    "launch_browser",
    "terminate_browser",
]
