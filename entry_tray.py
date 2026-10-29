"""托盘版入口（windowed 打包，无控制台窗口）。

与 console 版同一套 CLI；--tray 参数启动系统托盘模式。
冻结环境下把模板与看板页面定位到 _MEIPASS。
"""

import sys
from pathlib import Path

import task_arranger.templates as _tpl

_bundled = Path(getattr(sys, "_MEIPASS", ".")) / "task_arranger"
if _bundled.is_dir():
    _tpl.TEMPLATES_DIR = _bundled

from task_arranger.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
