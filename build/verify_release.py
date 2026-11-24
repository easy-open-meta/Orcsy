"""Validate release assets and their SHA256 sidecars before publishing."""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path


class ArtifactError(ValueError):
    pass


def verify_checksums(files: list[Path]) -> None:
    assets = {path.name: path for path in files}
    if len(assets) != len(files):
        raise ArtifactError("重复的发布附件文件名")
    for name, path in assets.items():
        if not path.is_file() or path.stat().st_size == 0:
            raise ArtifactError(f"缺少发布附件或文件为空：{name}")
        if name.endswith(".sha256.sha256"):
            raise ArtifactError(f"重复校验文件后缀：{name}")
    for name, path in assets.items():
        if name.endswith(".sha256"):
            target = name[:-7]
            if target not in assets:
                raise ArtifactError(f"校验文件没有对应附件：{name}")
            try:
                line = path.read_text(encoding="ascii")
            except (OSError, UnicodeError) as exc:
                raise ArtifactError(f"无法读取校验文件：{name}") from exc
            match = re.fullmatch(r"([0-9a-fA-F]{64})  ([^\r\n]+)\n?", line)
            if not match or match.group(2) != target:
                raise ArtifactError(f"校验文件格式或文件名错误：{name}")
            try:
                with assets[target].open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
            except OSError as exc:
                raise ArtifactError(f"无法读取发布附件：{target}") from exc
            if digest.lower() != match.group(1).lower():
                raise ArtifactError(f"SHA256 不匹配：{target}")
        elif name + ".sha256" not in assets:
            raise ArtifactError(f"缺少校验文件：{name}.sha256")


def main(argv: list[str]) -> int:
    if not argv:
        print("用法: verify_release.py <附件...>", file=sys.stderr)
        return 2
    try:
        verify_checksums([Path(arg) for arg in argv])
    except ArtifactError as exc:
        print(f"发布校验失败：{exc}", file=sys.stderr)
        return 1
    print(f"发布校验通过：{len(argv)} 个附件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
