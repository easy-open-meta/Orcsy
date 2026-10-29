"""内置模板（AGENT.md / 示例任务）的定位。

源码运行时在包目录内；PyInstaller 冻结后由 entry_web 把路径指到 _MEIPASS。
"""

from __future__ import annotations

from pathlib import Path

TEMPLATES_DIR = Path(__file__).parent / "templates"
