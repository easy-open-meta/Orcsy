"""项目归档（评估⑤）回归：任务/指引打包、排除噪音、保留数裁剪。

运行：uv run python tests/test_archive.py
"""

from __future__ import annotations

import os
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

from orcsy.archive import archive_project, prune_archives


class _Proj:
    name = "demo"
    path = ""
    tasks_dir = "tasks"
    guide_file = "AGENT.md"


def check_archive_content() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        proj_root = td / "repo"
        tasks = proj_root / "tasks"
        (tasks / "done").mkdir(parents=True)
        (tasks / "open.md").write_text("open\n", encoding="utf-8")
        (tasks / "done" / "done.md").write_text("done\n", encoding="utf-8")
        (proj_root / "AGENT.md").write_text("guide\n", encoding="utf-8")
        (proj_root / "ARCHITECTURE.md").write_text("arch\n", encoding="utf-8")
        (proj_root / "PROJECT_NOTES.md").write_text("notes\n", encoding="utf-8")
        (proj_root / "LONG_TERM_MEMORY.md").write_text("ltm\n", encoding="utf-8")
        (proj_root / ".worktree-arranger").mkdir()
        (proj_root / ".worktree-arranger" / "x.txt").write_text("x", encoding="utf-8")

        p = _Proj()
        p.path = str(proj_root)
        archive = archive_project(p, data_dir=td)

        names: set[str] = set()
        with tarfile.open(archive, "r:gz") as tf:
            for m in tf.getmembers():
                names.add(m.name)
        assert "tasks/open.md" in names, names
        assert "tasks/done/done.md" in names, names
        assert "AGENT.md" in names, names
        # 项目长期记忆随快照一起打包
        assert {"ARCHITECTURE.md", "PROJECT_NOTES.md", "LONG_TERM_MEMORY.md"} <= names, names
        assert ".worktree-arranger/x.txt" not in names  # worktree 不进归档
    print("归档内容 ✅")


def check_prune() -> None:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "archive"
        d.mkdir()
        for i in range(4):
            f = d / f"orcsy-project-demo-20260101-00000{i}.tar.gz"
            f.write_text(str(i), encoding="utf-8")
            os.utime(f, (1000 + i, 1000 + i))
        prune_archives(d, keep_last=2, project_name="demo")
        left = sorted(p.name for p in d.iterdir())
        assert len(left) == 2, left
        assert left[-1].endswith("3.tar.gz"), left
    print("归档保留数裁剪 ✅")


if __name__ == "__main__":
    check_archive_content()
    check_prune()
    print("test_archive: 全部通过")