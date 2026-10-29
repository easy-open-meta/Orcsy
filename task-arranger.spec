# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec：task-arranger 双产物
#   dist/task-arranger-web.exe   console 版（web/run/init/validate）
#   dist/task-arranger-tray.exe  windowed 托盘版（无黑窗口，开机自启用）

import os
from pathlib import Path

_common_datas = [('task_arranger/templates', 'task_arranger/templates')]
# dashboard.html 已编译进 task_arranger/dashboard_html.py，无需数据文件

_common_excludes = ['tkinter', 'matplotlib', 'numpy']


def build(name, script, console):
    a = Analysis(
        [script],
        pathex=[str(Path('.').resolve())],
        binaries=[],
        datas=list(_common_datas),
        hiddenimports=['task_arranger.web', 'task_arranger.tray', 'task_arranger.autostart'],
        hookspath=[],
        runtime_hooks=[],
        excludes=_common_excludes,
        noarchive=False,
    )
    pyz = PYZ(a.pure, a.zipped_data)
    return EXE(
        pyz, a.scripts, a.binaries, a.zipfiles, a.datas,
        name=name,
        icon='build/icon.ico',
        console=console,
        upx=False,
    )


build('task-arranger-web', 'entry_web.py', console=True)
build('task-arranger-tray', 'entry_tray.py', console=False)
