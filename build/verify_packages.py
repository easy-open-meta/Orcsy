"""发布包内容校验（预发布 smoke 用，纯标准库，不依赖 unzip/dpkg-deb 等外部命令）。

用法：python3 build/verify_packages.py <dist 目录> <版本号>
失败时打印缺失/冲突的成员并以非 0 退出。
"""

from __future__ import annotations

import io
import sys
import tarfile
import zipfile
from pathlib import Path


class PackageError(ValueError):
    pass


def _need(path: Path) -> Path:
    if not path.is_file() or path.stat().st_size == 0:
        raise PackageError(f"缺少发布包：{path.name}")
    return path


def _zip_names(path: Path) -> set[str]:
    _need(path)
    try:
        with zipfile.ZipFile(path) as zf:
            return set(zf.namelist())
    except zipfile.BadZipFile as e:
        raise PackageError(f"{path.name} 不是有效 zip：{e}") from e


def _tar_names(path: Path) -> set[str]:
    _need(path)
    try:
        with tarfile.open(path, "r:gz") as tf:
            return set(tf.getnames())
    except tarfile.TarError as e:
        raise PackageError(f"{path.name} 不是有效 tar.gz：{e}") from e


def _ar_entries(blob: bytes) -> dict[str, bytes]:
    if not blob.startswith(b"!<arch>\n"):
        raise PackageError("deb 不是 ar 归档")
    out: dict[str, bytes] = {}
    off = 8
    while off + 60 <= len(blob):
        header = blob[off:off + 60]
        name = header[:16].decode("ascii").strip()
        size = int(header[48:58].decode("ascii").strip() or 0)
        out[name] = blob[off + 60:off + 60 + size]
        off += 60 + size + (size % 2)
    return out


def _deb_data_names(path: Path) -> set[str]:
    _need(path)
    entries = _ar_entries(path.read_bytes())
    if "data.tar.gz" not in entries:
        raise PackageError(f"{path.name} 缺少 data.tar.gz")
    with tarfile.open(fileobj=io.BytesIO(entries["data.tar.gz"]), mode="r:gz") as tf:
        return set(tf.getnames())


def _require(names: set[str], required: list[str], label: str) -> None:
    missing = [n for n in required if n not in names]
    if missing:
        raise PackageError(f"{label} 缺少：{', '.join(missing)}")


def verify(dist: Path, version: str) -> list[str]:
    """校验 dist 目录下本次发布的关键包内容，返回逐项通过说明。"""
    report: list[str] = []

    for label, name in (
        ("amd64 更新包", f"orcsy-update-{version}.zip"),
        ("arm64 更新包", f"orcsy-update-{version}-windows-arm64.zip"),
    ):
        names = _zip_names(dist / name)
        _require(names, ["orcsy-web.exe", "orcsy-tray.exe"], label)
        report.append(f"{label}: {name} 含 orcsy-web.exe / orcsy-tray.exe")

    source_zip = f"orcsy-{version}-windows-arm64.zip"
    prefix = f"orcsy-{version}-windows-arm64"
    names = _zip_names(dist / source_zip)
    _require(names, [f"{prefix}/orcsy.bat", f"{prefix}/pyproject.toml"], "arm64 源码便携包")
    report.append(f"arm64 源码便携包: {source_zip} 含启动器与 pyproject.toml")

    for arch in ("amd64", "arm64"):
        tar_name = f"orcsy-{version}-linux-{arch}.tar.gz"
        prefix = f"orcsy-{version}-linux-{arch}"
        names = _tar_names(dist / tar_name)
        _require(names, [f"{prefix}/orcsy.sh", f"{prefix}/orcsy/tools.py"],
                 f"linux-{arch} 便携包")
        if f"{prefix}/orcsy" in names:
            raise PackageError(
                f"{tar_name} 中启动器 {prefix}/orcsy 与包目录同名，解包会失败")
        report.append(f"linux-{arch} 便携包: 启动器 orcsy.sh 与 orcsy/ 目录共存")

    for arch in ("amd64", "arm64"):
        deb_name = f"orcsy_{version}_{arch}.deb"
        names = _deb_data_names(dist / deb_name)
        _require(names, ["./opt/orcsy/orcsy", "./opt/orcsy/tools.py"], f"deb {arch}")
        report.append(f"deb {arch}: 启动器与 /opt/orcsy 代码目录结构正确")

    return report


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法: verify_packages.py <dist 目录> <版本号>", file=sys.stderr)
        return 2
    dist, version = Path(argv[0]), argv[1]
    try:
        for line in verify(dist, version):
            print(f"✓ {line}")
    except PackageError as e:
        print(f"发布包校验失败：{e}", file=sys.stderr)
        return 1
    print(f"发布包内容校验通过（{version}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))