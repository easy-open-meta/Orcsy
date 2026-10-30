# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec：orcsy 双产物
#   dist/orcsy-web.exe   console 版（web/run/init/validate）
#   dist/orcsy-tray.exe  windowed 托盘版（无黑窗口，开机自启用）
# 版本资源来自 build/exe-version.txt（由 build/build.py 从 __version__ 生成）

import os
from pathlib import Path

_common_datas = [('orcsy/templates', 'orcsy/templates')]
# dashboard.html 已编译进 orcsy/dashboard_html.py，无需数据文件

_common_excludes = ['tkinter', 'matplotlib', 'numpy']


def build(name, script, console):
    a = Analysis(
        [script],
        pathex=[str(Path('.').resolve())],
        binaries=[],
        datas=list(_common_datas),
        hiddenimports=['orcsy.web', 'orcsy.tray', 'orcsy.autostart'],
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
        version='build/exe-version.txt',
        console=console,
        upx=False,
    )


build('orcsy-web', 'entry_web.py', console=True)
build('orcsy-tray', 'entry_tray.py', console=False)
