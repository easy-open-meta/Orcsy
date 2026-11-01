"""一键构建：版本号单源化 → PyInstaller 双产物 → Inno Setup 安装包 + zip 更新包。

版本号唯一来源：orcsy/__init__.py 的 __version__。
版本规则：功能大变更（新部署方式/新核心功能）→ 次版本 +1（如 0.12.2 → 0.13.0）；
功能补丁（行为修正/UI 调整/文案提示）→ 修订号 +1（如 0.12.2 → 0.12.3）。
本脚本由它生成两份构建期文件（勿手改）：
  - build/exe-version.txt : PyInstaller exe 的 Windows 版本资源
  - build/version.ini     : installer.iss 的 MyAppVersion（ReadIni 读取）
之后依次执行 PyInstaller、ISCC 与 zip 打包，保证 exe 自报版本 = 安装包版本。

产物：
  - build/Setup/orcsy-setup-{ver}.exe   首次安装用 Inno 安装包（允许 UAC）
  - build/Setup/orcsy-update-{ver}.zip  自更新用 zip 更新包（手动导入/在线更新共用，
    零 UAC 换名安装；发布 Release 时须将此 zip 一并作为附件上传）

用法：uv run python build/build.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
COMPANY = "Orcsy"
PRODUCT_EN = "Orcsy"


def read_version() -> tuple[int, int, int]:
    text = (ROOT / "orcsy" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', text)
    if not m:
        sys.exit("无法从 orcsy/__init__.py 解析 __version__")
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
        StringStruct('FileDescription', 'Orcsy — Agent 任务编排系统'),
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


def build_update_zip(ver: tuple[int, int, int]) -> Path:
    """打包 zip 更新包（自更新载体）：两个 exe + README，扁平结构。

    成员名与 orcsy/updater.py 的白名单一致：orcsy-web.exe / orcsy-tray.exe /
    README*；文件名 orcsy-update-{ver}.zip 供版本识别（同/异版本均允许安装）。
    """
    dist = ROOT / "dist"
    out_dir = BUILD / "Setup"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"orcsy-update-{ver[0]}.{ver[1]}.{ver[2]}.zip"
    members = [dist / "orcsy-web.exe", dist / "orcsy-tray.exe",
               ROOT / "README.md", ROOT / "README.zh-CN.md"]
    missing = [m.name for m in members if not m.is_file()]
    if missing:
        sys.exit(f"zip 更新包缺少源文件：{', '.join(missing)}")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for m in members:
            zf.write(m, arcname=m.name)
    print(f"zip 更新包：{out}")
    return out


def main() -> int:
    ver = read_version()
    print(f"版本号来源 orcsy/__init__.py：{'.'.join(map(str, ver))}")
    BUILD.mkdir(exist_ok=True)
    write_exe_version_file(ver)
    write_version_ini(ver)
    build_frontend()

    run([sys.executable, "-m", "PyInstaller", "orcsy.spec", "--noconfirm"],
        cwd=ROOT)

    iscc = Path(sys.executable).parents[1] / "Programs" / "Inno Setup 6" / "ISCC.exe"
    if not iscc.exists():
        iscc = Path.home() / "AppData" / "Local" / "Programs" / "Inno Setup 6" / "ISCC.exe"
    if not iscc.exists():
        sys.exit("未找到 ISCC.exe（Inno Setup 6），请先安装")
    run([iscc, "installer.iss"], cwd=BUILD)

    zip_pkg = build_update_zip(ver)
    setup = BUILD / "Setup" / f"orcsy-setup-{ver[0]}.{ver[1]}.{ver[2]}.exe"
    print(f"\n完成：\n  首次安装包 {setup}\n  自更新包   {zip_pkg}"
          f"\n发布 Release 时两者都作为附件上传（在线更新只认 orcsy-update-*.zip）。")
    print("发布命令（exe + zip 一并上传）：uv run python build/release.py")
    print("校验：dist exe --version 与安装包文件版本应同为 "
          f"{ver[0]}.{ver[1]}.{ver[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
