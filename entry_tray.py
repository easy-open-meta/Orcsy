"""托盘版入口（windowed 打包，无控制台窗口）。

与 console 版同一套 CLI；托盘图标由 cmd_web 默认启动（后台线程，
与控制台版行为一致），--tray 参数仅为自启动注册表兼容保留。
冻结环境下把模板目录定位到 _MEIPASS。
"""

import sys
from pathlib import Path

import orcsy.templates as _tpl

_bundled = Path(getattr(sys, "_MEIPASS", ".")) / "orcsy" / "templates"
if _bundled.is_dir():
    _tpl.TEMPLATES_DIR = _bundled

from orcsy.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
