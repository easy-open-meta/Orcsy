"""一键构建：版本号单源化 → PyInstaller 双产物 → Inno Setup 安装包。

版本号唯一来源：task_arranger/__init__.py 的 __version__。
本脚本由它生成两份构建期文件（勿手改）：
  - build/exe-version.txt : PyInstaller exe 的 Windows 版本资源
  - build/version.ini     : installer.iss 的 MyAppVersion（ReadIni 读取）
之后依次执行 PyInstaller 与 ISCC，保证 exe 自报版本 = 安装包版本。

用法：uv run python build/build.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
COMPANY = "TaskArranger"
PRODUCT_EN = "Task Arranger"


def read_version() -> tuple[int, int, int]:
    text = (ROOT / "task_arranger" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', text)
    if not m:
        sys.exit("无法从 task_arranger/__init__.py 解析 __version__")
    return tuple(int(g) for g in m.groups())  # type: ignore[return-value]


def write_exe_version_file(ver: tuple[int, int, int]) -> None:
    """PyInstaller 的 Windows 版本资源（资源管理器属性里看到的 FileVersion）。"""
    v = ver + (0,)
    ver_str = ".".join(str(x) for x in v)
    tpl = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={v}, prodvers={v},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable('080404b0', [
        StringStruct('CompanyName', '{COMPANY}'),
        StringStruct('FileDescription', '任务编排系统'),
        StringStruct('FileVersion', '{ver_str}'),
        StringStruct('ProductName', '{PRODUCT_EN}'),
        StringStruct('ProductVersion', '{ver[0]}.{ver[1]}.{ver[2]}'),
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])]),
  ]
)
"""
    (BUILD / "exe-version.txt").write_text(tpl, encoding="utf-8")


def write_version_ini(ver: tuple[int, int, int]) -> None:
    (BUILD / "version.ini").write_text(
        f"[Version]\nAppVersion={ver[0]}.{ver[1]}.{ver[2]}\n", encoding="ascii")


def run(cmd: list[str], cwd: Path) -> None:
    print(f"$ {' '.join(str(c) for c in cmd)}  (cwd={cwd})")
    subprocess.run([str(c) for c in cmd], cwd=cwd, check=True, shell=False)


def _npm(cwd: Path, *args: str) -> None:
    """Windows 上 npm 是 npm.cmd，CreateProcess 只认全路径，先用 which 解析。"""
    if sys.platform == "win32":
        npm = shutil.which("npm.cmd") or shutil.which("npm")
    else:
        npm = shutil.which("npm")
    if npm is None:
        sys.exit("未找到 npm，请先安装 Node.js（系统 PATH 需包含 nodejs 目录）")
    run([npm, *args], cwd=cwd)


def build_frontend() -> None:
    """前端工程构建：npm install（缺依赖时）→ vite build → 生成 dashboard_html.py。"""
    dash = ROOT / "dashboard"
    if not (dash / "package.json").exists():
        return
    if not (dash / "node_modules").exists():
        _npm(dash, "install", "--no-fund", "--no-audit")
    _npm(dash, "run", "build")
    run([sys.executable, "tools/gen_page.py"], cwd=ROOT)


def main() -> int:
    ver = read_version()
    print(f"版本号来源 task_arranger/__init__.py：{'.'.join(map(str, ver))}")
    BUILD.mkdir(exist_ok=True)
    write_exe_version_file(ver)
    write_version_ini(ver)
    build_frontend()

    run([sys.executable, "-m", "PyInstaller", "task-arranger.spec", "--noconfirm"],
        cwd=ROOT)

    iscc = Path(sys.executable).parents[1] / "Programs" / "Inno Setup 6" / "ISCC.exe"
    if not iscc.exists():
        iscc = Path.home() / "AppData" / "Local" / "Programs" / "Inno Setup 6" / "ISCC.exe"
    if not iscc.exists():
        sys.exit("未找到 ISCC.exe（Inno Setup 6），请先安装")
    run([iscc, "installer.iss"], cwd=BUILD)

    setup = BUILD / "Setup" / f"task-arranger-setup-{ver[0]}.{ver[1]}.{ver[2]}.exe"
    print(f"\n完成：{setup}")
    print("校验：dist exe --version 与安装包文件版本应同为 "
          f"{ver[0]}.{ver[1]}.{ver[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
