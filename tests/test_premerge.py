"""Isolated latest-main test gate: success, failure, dirty main and cleanup."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from threading import Event

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orcsy.git_ops import Git
from orcsy.config import LLMConfig, ProjectConfig
from orcsy.executor import TaskExecutor, TaskResult
from orcsy.task_model import Task
from orcsy.premerge import PremergeConflict, PremergeError, verify_candidate


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def check_auto_merge_conflict_aborts() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.name", "Test")
        _git(repo, "config", "user.email", "test@example.org")
        source = repo / "shared.txt"
        source.write_text("base\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "base")
        _git(repo, "checkout", "-b", "task")
        source.write_text("task\n", encoding="utf-8")
        _git(repo, "commit", "-am", "task")
        _git(repo, "checkout", "main")
        source.write_text("main\n", encoding="utf-8")
        _git(repo, "commit", "-am", "main")
        before = _git(repo, "rev-parse", "HEAD")
        executor = TaskExecutor(ProjectConfig(name="p", path=repo),
                                LLMConfig(api_key="k", model="m"), commit_and_merge=True)
        result = TaskResult(task_id="t", outcome="blocked")
        assert executor._merge_and_push(Task(id="t", path=repo / "task.md", title="test"),
                                        result, "task") is False
        assert _git(repo, "rev-parse", "HEAD") == before
        assert not _git(repo, "status", "--porcelain")
        assert not (repo / ".git" / "MERGE_HEAD").exists()


def check_stage_apply_conflict_preserves_main() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.name", "Test")
        _git(repo, "config", "user.email", "test@example.org")
        target = repo / "shared.txt"
        target.write_text("base\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "base")
        base = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "-b", "stage")
        target.write_text("stage\n", encoding="utf-8")
        _git(repo, "commit", "-am", "stage")
        stage = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "main")
        target.write_text("user change\n", encoding="utf-8")
        before = _git(repo, "status", "--porcelain")
        ok, _ = Git(repo).apply_commit_delta_to_main(base, stage)
        assert not ok
        assert target.read_text(encoding="utf-8") == "user change\n"
        assert _git(repo, "status", "--porcelain") == before


def check_consecutive_stage_candidates() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.name", "Test")
        _git(repo, "config", "user.email", "test@example.org")
        (repo / "base.txt").write_text("base", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "base")
        _git(repo, "branch", "stage", "main")
        _git(repo, "checkout", "-b", "task-one")
        (repo / "one.txt").write_text("one", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "one")
        _git(repo, "checkout", "main")
        stage_wt = Path(td) / "stage"
        git = Git(repo)
        ok, prev, head, err = git.merge_into_stage("stage", stage_wt, "task-one", "merge one")
        assert ok, err
        landed, err = git.apply_commit_delta_to_main(prev, head)
        assert landed, err
        assert (repo / "one.txt").read_text(encoding="utf-8") == "one"
        _git(repo, "branch", "task-two", "stage")
        task_wt = Path(td) / "task-two"
        _git(repo, "worktree", "add", str(task_wt), "task-two")
        _git(task_wt, "config", "user.name", "Test")
        _git(task_wt, "config", "user.email", "test@example.org")
        (task_wt / "two.txt").write_text("two", encoding="utf-8")
        _git(task_wt, "add", ".")
        _git(task_wt, "commit", "-m", "two")
        cmd = [sys.executable, "-c", "from pathlib import Path; assert Path('one.txt').exists(); assert Path('two.txt').exists()"]
        tested, _ = verify_candidate(git, "stage", "task-two", cmd, ["python*"],
                                     allow_stage_workspace=True)
        assert tested == _git(repo, "rev-parse", "stage")
        assert (repo / "one.txt").read_text(encoding="utf-8") == "one"
        assert not (repo / "two.txt").exists()
        (repo / "unrelated.txt").write_text("not in stage", encoding="utf-8")
        try:
            verify_candidate(git, "stage", "task-two", cmd, ["python*"],
                             allow_stage_workspace=True)
            raise AssertionError("stage mismatch must block")
        except PremergeError as exc:
            assert "不一致" in str(exc)


def main() -> None:
    check_auto_merge_conflict_aborts()
    check_consecutive_stage_candidates()
    check_stage_apply_conflict_preserves_main()
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.name", "Test")
        _git(repo, "config", "user.email", "test@example.org")
        (repo / "base.txt").write_text("base", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "base")
        _git(repo, "checkout", "-b", "task")
        (repo / "feature.txt").write_text("feature", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "feature")
        _git(repo, "checkout", "main")
        (repo / "latest.txt").write_text("latest", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "latest")
        main_sha = _git(repo, "rev-parse", "HEAD")
        command = [sys.executable, "-c", "from pathlib import Path; assert Path('feature.txt').exists(); assert Path('latest.txt').exists()"]
        tested_sha, _ = verify_candidate(Git(repo), "main", "task", command, ["python*"])
        assert tested_sha == main_sha
        assert _git(repo, "rev-parse", "HEAD") == main_sha
        assert not _git(repo, "status", "--porcelain")
        assert len(_git(repo, "worktree", "list", "--porcelain").split("worktree ")) == 2
        try:
            verify_candidate(Git(repo), "main", "task",
                             [sys.executable, "-c", "from pathlib import Path; Path('feature.txt').write_text('mutated')"], ["python*"])
            raise AssertionError("mutated candidate must not pass")
        except PremergeError as exc:
            assert "修改了候选代码" in str(exc)
        stopped = Event()
        stopped.set()
        try:
            verify_candidate(Git(repo), "main", "task", command, ["python*"], stop_event=stopped)
            raise AssertionError("stopped task must not launch tests")
        except PremergeError as exc:
            assert "取消" in str(exc)
        try:
            verify_candidate(Git(repo), "main", "task",
                             [sys.executable, "-c", "print('x' * (17 * 1024 * 1024))"], ["python*"])
            raise AssertionError("excessive output must block")
        except PremergeError as exc:
            assert "16 MiB" in str(exc)

        try:
            verify_candidate(Git(repo), "main", "task", [sys.executable, "-c", "raise SystemExit(4)"], ["python*"])
            raise AssertionError("nonzero test must block merge")
        except PremergeError as exc:
            assert "退出码 4" in str(exc)
        try:
            verify_candidate(Git(repo), "main", "task", [sys.executable, "-c", "pass"], ["git"])
            raise AssertionError("disallowed executable must block merge")
        except PremergeError as exc:
            assert "白名单" in str(exc)
        (repo / "base.txt").write_text("dirty", encoding="utf-8")
        try:
            verify_candidate(Git(repo), "main", "task", command, ["python*"])
            raise AssertionError("dirty main must block merge")
        except PremergeError as exc:
            assert "未提交改动" in str(exc)
        assert (repo / "base.txt").read_text(encoding="utf-8") == "dirty"
        assert _git(repo, "rev-parse", "HEAD") == main_sha
        (repo / "base.txt").write_text("base", encoding="utf-8")

        project = ProjectConfig(name="p", path=repo, allowed_branch="main",
                                premerge_test_enabled=True,
                                premerge_test_command=[sys.executable, "-c", "raise SystemExit(4)"])
        executor = TaskExecutor(project, LLMConfig(api_key="k", model="m"),
                                allowed_commands=["python*"], commit_and_merge=True)
        task = Task(id="t", path=repo / "task.md", title="test")
        result = TaskResult(task_id="t", outcome="blocked")
        before = _git(repo, "rev-parse", "HEAD")
        assert executor._merge_with_premerge(task, result, "task", repo) is None
        assert "退出码 4" in result.notes
        assert result.block_reason == "premerge_test_failed"
        assert _git(repo, "rev-parse", "HEAD") == before
        assert executor._merge_heal == {}
        project.premerge_test_command = command
        invoked: list[str] = []
        executor._merge_and_push = lambda *args, **kwargs: invoked.append("merge") or True
        assert executor._merge_with_premerge(task, result, "task", repo) is True
        assert invoked == ["merge"]
        _git(repo, "branch", "orcsy-stage", "main")
        stage_project = ProjectConfig(name="p", path=repo, allowed_branch="main",
                                      premerge_test_enabled=True,
                                      premerge_test_command=command)
        stage_executor = TaskExecutor(stage_project, LLMConfig(api_key="k", model="m"),
                                      allowed_commands=["python*"], commit_and_merge=False,
                                      worktree_isolation=True)
        stage_executor._stage_branch = lambda: "orcsy-stage"
        stage_executor._merge_to_stage_and_land_main = lambda *args, **kwargs: invoked.append("stage") or True
        assert stage_executor._merge_with_premerge(task, result, "task", repo) is True
        assert invoked[-1] == "stage"
        _git(repo, "checkout", "-b", "other")
        (repo / "other.txt").write_text("different", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "other")
        _git(repo, "checkout", "main")
        _git(repo, "branch", "-f", "orcsy-stage", "other")
        assert stage_executor._merge_with_premerge(task, result, "task", repo) is None
        assert invoked == ["merge", "stage"]
        race_script = ("import subprocess; subprocess.run(['git', '-C', "
                       + repr(str(repo)) + ", 'commit', '--allow-empty', '-m', 'race'], check=True)")
        try:
            verify_candidate(Git(repo), "main", "task", [sys.executable, "-c", race_script], ["python*"])
            raise AssertionError("advanced main must invalidate candidate")
        except PremergeError as exc:
            assert "主分支发生变化" in str(exc)
        assert not _git(repo, "status", "--porcelain")
    print("test_premerge: all checks passed")


if __name__ == "__main__":
    main()
