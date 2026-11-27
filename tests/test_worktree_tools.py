"""worktree 目录名与忽略集合同名时，文件探索工具仍应看到跟踪文件。

回归背景（0.36.0 实测）：默认 worktree 目录名为 ``.worktree-arranger``，
而忽略集合里也包含该名字；旧实现按绝对路径 ``p.parts`` 判断，导致
worktree 内所有文件被过滤，``list_dir`` / ``find_files`` / ``grep`` 一律
返回空目录，Agent 误判项目为空、审查直接判失败。修复为按 ``ctx.root``
之下的相对路径判断。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.tools import ToolContext
from orcsy.tools_fs import tool_find_files, tool_grep, tool_list_dir


def make_worktree() -> Path:
    # 刻意把根目录命名为 .worktree-arranger（与默认 worktree 一致）
    root = Path(tempfile.mkdtemp(prefix="wt-tools-")) / ".worktree-arranger"
    (root / "deploy").mkdir(parents=True)
    (root / "deploy" / "server.yaml").write_text(
        "replicas: 1\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("x\n", encoding="utf-8")
    return root


def main() -> int:
    root = make_worktree()
    ctx = ToolContext(root=root)

    listing = tool_list_dir(ctx, {"path": "."})
    assert "deploy/server.yaml" in listing, listing
    assert "node_modules" not in listing, listing

    found = tool_find_files(ctx, {"pattern": "*.yaml"})
    assert "deploy/server.yaml" in found, found

    hits = tool_grep(ctx, {"pattern": "replicas"})
    assert "deploy/server.yaml" in hits and "replicas" in hits, hits

    print("worktree 同名忽略项下 list_dir/find_files/grep 可见性 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
