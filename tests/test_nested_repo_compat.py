"""caresy 类特殊仓库兼容：含 gitlink/嵌套 Git 仓库时自动回退主工作区模式。"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import LLMConfig, ProjectConfig
from orcsy.executor import TaskExecutor
from orcsy.prompts import build_system_prompt


def _git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(cwd), *args],
                       capture_output=True, text=True)
    assert p.returncode == 0, (args, p.stdout, p.stderr)
    return p.stdout.strip()


def _make_repo_with_gitlink(root: Path) -> tuple[Path, Path]:
    parent = root / "caresy"
    parent.mkdir()
    nested = parent / "caresy-api"
    nested.mkdir()
    _git(nested, "init", "-q")
    _git(nested, "config", "user.email", "t@t")
    _git(nested, "config", "user.name", "t")
    (nested / "Program.cs").write_text("class A {}\n", encoding="utf-8")
    _git(nested, "add", "-A")
    _git(nested, "commit", "-qm", "init")
    nested_sha = _git(nested, "rev-parse", "HEAD")

    _git(parent, "init", "-q")
    _git(parent, "config", "user.email", "t@t")
    _git(parent, "config", "user.name", "t")
    _git(parent, "checkout", "-b", "main")
    _git(parent, "update-index", "--add", "--cacheinfo",
         f"160000,{nested_sha},caresy-api")
    _git(parent, "commit", "-qm", "add nested repo")
    return parent, nested


def test_gitlink_detection_and_workspace_fallback() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="nested-repo-"))
    try:
        parent, nested = _make_repo_with_gitlink(tmp)
        project = ProjectConfig(name="caresy", path=parent)
        ex = TaskExecutor(project, LLMConfig(
            api_key="k", base_url="http://localhost:9", model="m"))
        assert ex.is_git_repo is True
        assert ex.gitlinks == ["caresy-api"], ex.gitlinks
        assert ex.worktree_isolation is False, "含 gitlink 应回退主工作区模式"
        assert ex.per_task_worktree is False
        assert ex.worktree_mgr.worktree_isolation is False

        prompt = build_system_prompt(project, "# guide", True,
                                     nested_repos=ex.gitlinks)
        assert "嵌套 Git 仓库" in prompt and "caresy-api" in prompt
        assert "不做 worktree 隔离" in prompt

        # 嵌套仓库改动能被工作区 diff 收集（父仓库 git diff 看不到这些文件）
        (nested / "Program.cs").write_text("class A { int x; }\n", encoding="utf-8")
        diff = ex._workspace_diff()
        assert "nested repo: caresy-api" in diff, diff[:500]
        assert "int x" in diff, diff[:500]
        print("ok: gitlink 检测 / 主工作区回退 / 嵌套 diff / 提示词")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    test_gitlink_detection_and_workspace_fallback()
    print("\n嵌套 Git 仓库兼容自检通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
