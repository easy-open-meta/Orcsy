"""PyInstaller 入口：冻结环境下把模板目录定位到 _MEIPASS，再调用真实 CLI。"""

import sys
from pathlib import Path

import orcsy.templates as _tpl

_bundled = Path(getattr(sys, "_MEIPASS", ".")) / "orcsy" / "templates"
if _bundled.is_dir():
    _tpl.TEMPLATES_DIR = _bundled

from orcsy.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
