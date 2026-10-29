"""把 Vite 构建产物（单文件 index.html）转成 task_arranger/dashboard_html.py 常量。

沿用"页面编译进 exe"机制，规避 PyInstaller _MEI 数据文件读取坑。
用法：uv run python tools/gen_page.py
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "dashboard" / "dist" / "index.html"
DST = ROOT / "task_arranger" / "dashboard_html.py"

html = SRC.read_text(encoding="utf-8")
DST.write_text(
    '"""看板页面（由 dashboard/ 前端工程构建产物生成，编译进 exe，避免 _MEI 数据文件读取问题）。"""\n\n'
    "PAGE = " + repr(html) + "\n",
    encoding="utf-8",
)
print(f"dashboard_html.py 已生成：{len(html)} 字符 ← {SRC}")
