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

import argparse
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
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
    # sha256 校验文件（sha256sum 标准格式「<hex>  <文件名>」）：在线更新下载后
    # 校验哈希才允许进安装环节（安全审计 2026-11-04 M2）。发布 Release 时
    # zip 与 zip.sha256 必须成对上传，缺一不可
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    sha = out.with_name(out.name + ".sha256")
    sha.write_text(f"{digest}  {out.name}\n", encoding="ascii")
    print(f"zip 更新包：{out}")
    print(f"sha256 校验：{sha}")
    return out


def build_linux_tarball(ver: tuple[int, int, int], arch: str) -> Path:
    """构建独立 Linux 绿色发布包（linux-amd64 / linux-arm64）。

    包含：
    - orcsy: 可执行 CLI 启动器（0o755）
    - install.sh: 一键安装服务（systemd）或当前用户级工具（0o755）
    - uninstall.sh: 一键卸载器（0o755）
    - orcsy.service: systemd 服务单元
    - orcsy/ 完整 Python 包（含已编译内联的前端页面）
    - pyproject.toml、README、配置模板与架构清单
    产出：
    - build/Setup/orcsy-{ver}-linux-{arch}.tar.gz
    - build/Setup/orcsy-{ver}-linux-{arch}.tar.gz.sha256
    """
    v_str = f"{ver[0]}.{ver[1]}.{ver[2]}"
    out_dir = BUILD / "Setup"
    out_dir.mkdir(parents=True, exist_ok=True)
    tar_name = f"orcsy-{v_str}-linux-{arch}.tar.gz"
    out_tar = out_dir / tar_name
    folder_prefix = f"orcsy-{v_str}-linux-{arch}"

    launcher_sh = f"""#!/usr/bin/env bash
# Orcsy (linux-{arch}) CLI 启动器
set -euo pipefail
DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
export PYTHONPATH="$DIR:${{PYTHONPATH:-}}"

if [ -f "$DIR/.venv/bin/python" ]; then
    exec "$DIR/.venv/bin/python" -m orcsy "$@"
fi

if command -v uv >/dev/null 2>&1; then
    exec uv --directory "$DIR" run orcsy "$@"
fi

if command -v python3 >/dev/null 2>&1; then
    exec python3 -m orcsy "$@"
fi

echo "错误: 未找到可用的 python3 或 uv 运行环境，请先安装 Python 3.11+ 或执行 ./install.sh" >&2
exit 1
"""

    install_sh = f"""#!/usr/bin/env bash
# Orcsy 原生 Linux 一键安装器 (系统级 systemd 服务 或 当前用户级 CLI)
set -euo pipefail
DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"

IS_USER=0
for arg in "$@"; do
    if [ "$arg" = "--user" ]; then IS_USER=1; fi
done

if [ "$IS_USER" -eq 1 ]; then
    TARGET_DIR="$HOME/.local/share/orcsy"
    BIN_DIR="$HOME/.local/bin"
    mkdir -p "$TARGET_DIR" "$BIN_DIR"
    cp -r "$DIR/orcsy" "$TARGET_DIR/"
    cp "$DIR/pyproject.toml" "$TARGET_DIR/"
    [ -f "$DIR/config.example.yaml" ] && cp "$DIR/config.example.yaml" "$TARGET_DIR/"
    cp "$DIR/orcsy" "$BIN_DIR/orcsy"
    chmod +x "$BIN_DIR/orcsy"
    echo "==> 已安装到用户目录: $BIN_DIR/orcsy"
    echo "    请确保 $BIN_DIR 已在您的 PATH 中。"
    exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "系统级安装请使用 root 运行: sudo bash $0"
    echo "或者以普通用户免 root 安装: bash $0 --user"
    exit 1
fi

APP_DIR=/opt/orcsy
DATA_DIR=/var/lib/orcsy
PROJECTS_DIR=/srv/orcsy-projects
SERVICE_USER=orcsy
SERVICE_FILE=/etc/systemd/system/orcsy.service

echo "==> 正在安装 Orcsy 到 $APP_DIR..."
mkdir -p "$APP_DIR" "$DATA_DIR" "$PROJECTS_DIR"
cp -r "$DIR/orcsy" "$APP_DIR/"
cp "$DIR/pyproject.toml" "$APP_DIR/"
cp "$DIR/orcsy" "$APP_DIR/orcsy-bin"
chmod +x "$APP_DIR/orcsy-bin"
ln -sf "$APP_DIR/orcsy-bin" /usr/local/bin/orcsy

id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd -r -M -d "$DATA_DIR" -s /usr/sbin/nologin "$SERVICE_USER" || true
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR" "$DATA_DIR" "$PROJECTS_DIR" 2>/dev/null || true

CONFIG="$DATA_DIR/config.yaml"
if [ ! -f "$CONFIG" ] && [ -f "$DIR/config.example.yaml" ]; then
    AUTH_PW=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-20 2>/dev/null || echo "orcsy123456")
    sed "s|auth_password: \"\"|auth_password: \"$AUTH_PW\"|" "$DIR/config.example.yaml" > "$CONFIG" 2>/dev/null || cp "$DIR/config.example.yaml" "$CONFIG"
    chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG" 2>/dev/null || true
    chmod 600 "$CONFIG" 2>/dev/null || true
    echo "    已生成配置文件: $CONFIG"
    echo "    初始看板登录密码: $AUTH_PW"
fi

if [ -f "$DIR/orcsy.service" ] && command -v systemctl >/dev/null 2>&1; then
    install -m 644 "$DIR/orcsy.service" "$SERVICE_FILE"
    sed -i "s|/opt/orcsy/.venv/bin/orcsy|/usr/local/bin/orcsy|g" "$SERVICE_FILE" 2>/dev/null || true
    systemctl daemon-reload || true
    systemctl enable --now orcsy.service || true
    echo "==> systemd 服务已安装并尝试启动 (orcsy.service)"
fi

echo "==> Orcsy 安装完成！直接运行命令: orcsy web"
"""

    uninstall_sh = """#!/usr/bin/env bash
# Orcsy Linux 一键卸载器
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    rm -rf "$HOME/.local/share/orcsy" "$HOME/.local/bin/orcsy"
    echo "==> 用户级 Orcsy 已移除"
    exit 0
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl stop orcsy.service 2>/dev/null || true
    systemctl disable orcsy.service 2>/dev/null || true
    rm -f /etc/systemd/system/orcsy.service
    systemctl daemon-reload || true
fi
rm -f /usr/local/bin/orcsy
rm -rf /opt/orcsy
echo "==> Orcsy 程序文件与服务已移除（数据目录 /var/lib/orcsy 已保留）"
"""

    pkg_info = json.dumps({
        "name": "orcsy",
        "version": v_str,
        "os": "linux",
        "arch": arch,
        "entry_point": "orcsy",
    }, indent=2)

    service_file = ROOT / "deploy" / "linux" / "orcsy.service"
    service_content = service_file.read_text(encoding="utf-8") if service_file.is_file() else ""

    with tarfile.open(out_tar, "w:gz") as tar:
        def _add_virtual(name: str, content: str | bytes, mode: int = 0o644):
            raw = content.encode("utf-8") if isinstance(content, str) else content
            ti = tarfile.TarInfo(f"{folder_prefix}/{name}")
            ti.size = len(raw)
            ti.mode = mode
            ti.mtime = 1730000000
            tar.addfile(ti, io.BytesIO(raw))

        def _add_real(src: Path, arc_name: str, mode: int = 0o644):
            data = src.read_bytes()
            ti = tarfile.TarInfo(f"{folder_prefix}/{arc_name}")
            ti.size = len(data)
            ti.mode = mode
            ti.mtime = int(src.stat().st_mtime)
            tar.addfile(ti, io.BytesIO(data))

        # 脚本类成员赋 0o755 可执行权限
        _add_virtual("orcsy", launcher_sh, mode=0o755)
        _add_virtual("install.sh", install_sh, mode=0o755)
        _add_virtual("uninstall.sh", uninstall_sh, mode=0o755)
        if service_content:
            _add_virtual("orcsy.service", service_content, mode=0o644)
        _add_virtual("package-info.json", pkg_info, mode=0o644)

        for doc_file in ("pyproject.toml", "config.example.yaml", "README.md", "README.zh-CN.md"):
            p = ROOT / doc_file
            if p.is_file():
                _add_real(p, doc_file, mode=0o644)

        # 打包 orcsy/ 源码与模板资源（含已内联前端的 dashboard_html.py）
        for item in (ROOT / "orcsy").rglob("*"):
            if item.is_file():
                if "__pycache__" in item.parts or item.suffix in (".pyc", ".pyo"):
                    continue
                rel = item.relative_to(ROOT).as_posix()
                _add_real(item, rel, mode=0o644)

    digest = hashlib.sha256(out_tar.read_bytes()).hexdigest()
    sha = out_tar.with_name(out_tar.name + ".sha256")
    sha.write_text(f"{digest}  {out_tar.name}\n", encoding="ascii")
    print(f"Linux ({arch}) 发布包：{out_tar}")
    print(f"sha256 校验：{sha}")
    return out_tar


def main() -> int:
    parser = argparse.ArgumentParser(description="Orcsy 多平台一键构建与打包")
    parser.add_argument("--windows-only", action="store_true", help="仅构建 Windows 安装包与更新包")
    parser.add_argument("--linux-only", action="store_true", help="仅构建 Linux 发布包 (amd64 / arm64)")
    args = parser.parse_args()

    ver = read_version()
    v_str = f"{ver[0]}.{ver[1]}.{ver[2]}"
    print(f"版本号来源 orcsy/__init__.py：{v_str}")
    BUILD.mkdir(exist_ok=True)
    write_exe_version_file(ver)
    write_version_ini(ver)
    build_frontend()

    artifacts_done: list[str] = []

    # Windows 产物构建
    if not args.linux_only and sys.platform == "win32":
        run([sys.executable, "-m", "PyInstaller", "orcsy.spec", "--noconfirm"], cwd=ROOT)
        iscc = Path(sys.executable).parents[1] / "Programs" / "Inno Setup 6" / "ISCC.exe"
        if not iscc.exists():
            iscc = Path.home() / "AppData" / "Local" / "Programs" / "Inno Setup 6" / "ISCC.exe"
        if iscc.exists():
            run([iscc, "installer.iss"], cwd=BUILD)
            artifacts_done.append(f"Windows 首次安装包 build/Setup/orcsy-setup-{v_str}.exe")
        else:
            print("警告：未找到 ISCC.exe（Inno Setup 6），已跳过 Windows exe 安装包制作")
        build_update_zip(ver)
        artifacts_done.append(f"Windows 自更新包   build/Setup/orcsy-update-{v_str}.zip")

    # Linux 产物构建 (linux-amd64 与 linux-arm64)
    if not args.windows_only:
        build_linux_tarball(ver, "amd64")
        build_linux_tarball(ver, "arm64")
        artifacts_done.append(f"Linux (amd64) 发布包 build/Setup/orcsy-{v_str}-linux-amd64.tar.gz")
        artifacts_done.append(f"Linux (arm64) 发布包 build/Setup/orcsy-{v_str}-linux-arm64.tar.gz")

    print(f"\n==================== 构建完成 ====================")
    for art in artifacts_done:
        print(f"  {art}")
    print("\n发布命令（多平台产物与 sha256 一并发布）：uv run python build/release.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
