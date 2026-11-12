"""Git Worktree 沙箱隔离生命周期管理器与上下文会话。

本模块将 Git Worktree 的物理目录规划、工作分支检出、陈旧锁恢复与重置清理逻辑
从 TaskExecutor 中解耦，提供独立的 `WorktreeManager` 与上下文管理器 `WorktreeSession`。
"""

from __future__ import annotations

import contextlib
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from .git_ops import Git, GitError
from .logging_setup import get_logger

if TYPE_CHECKING:
    from .task_model import Task

log = get_logger("worktree")

WORKTREE_DIR = ".worktree-arranger"


def safe_wt_name(task_id: str) -> str:
    """将任务 ID 转换为安全的文件系统与分支名。"""
    clean = re.sub(r"[^\w\-.]", "_", str(task_id or "task")).strip("_") or "task"
    return clean[:60]


class WorktreeManager:
    """管理项目 Git Worktree 隔离沙箱的生命周期与分支。"""

    def __init__(
        self,
        project_path: Path,
        project_name: str,
        *,
        allowed_branch: str | None = None,
        per_task_worktree: bool = False,
        worktree_isolation: bool = True,
        commit_and_merge: bool = True,
        merge_lock: object | None = None,
    ) -> None:
        self.project_path = project_path.resolve()
        self.project_name = project_name
        self.allowed_branch = allowed_branch
        self.per_task_worktree = per_task_worktree
        self.worktree_isolation = worktree_isolation
        self.commit_and_merge = commit_and_merge
        self.merge_lock = merge_lock

        self.git = Git(self.project_path)
        self.worktree_root = self.project_path / WORKTREE_DIR
        self.default_worktree = self.worktree_root

    def is_git_repo(self) -> bool:
        """检查当前项目路径是否为合法的 Git 仓库。"""
        if not self.project_path.is_dir() or not (self.project_path / ".git").exists():
            return False
        return self.git.is_repo()

    def task_worktree(self, task: Task | None = None) -> Path:
        """返回任务对应的 Worktree 物理目录。"""
        if self.per_task_worktree and task is not None:
            return self.worktree_root / safe_wt_name(task.id)
        return self.default_worktree

    def task_branch(self, task: Task | None = None) -> str:
        """返回任务对应的临时工作分支名。"""
        if self.per_task_worktree and task is not None:
            return f"orcsy/task/{self.project_name}/{safe_wt_name(task.id)}"
        return f"arranger/{self.project_name}"

    def stage_branch(self) -> str:
        """返回内部集成分支名。"""
        return f"orcsy/stage/{self.project_name}"

    def stage_worktree(self) -> Path:
        """返回内部集成分支的独立 Worktree 目录。"""
        return self.worktree_root / "_stage"

    def base_branch(self) -> str:
        """计算任务沙盒的基准分支。"""
        main_branch = self.allowed_branch or self.git.current_branch()
        if self.worktree_isolation and not self.commit_and_merge:
            return self.stage_branch()
        return main_branch

    def branch_exists(self, branch: str) -> bool:
        """检查分支是否存在。"""
        return bool(self.git.run("branch", "--list", branch).strip())

    def setup(
        self,
        task: Task | None = None,
        branch: str | None = None,
        wt_path: Path | None = None,
        *,
        tasks_dir: str = "tasks",
        guide_file: str = "AGENT.md",
    ) -> str:
        """准备或复用 Worktree，并在其上检出目标分支。"""
        target_path = wt_path or self.task_worktree(task)
        target_branch = branch or self.task_branch(task)
        wt_git = Git(target_path)

        self.project_path.mkdir(parents=True, exist_ok=True)
        # 零提交空仓库特殊处理：首个任务需先在主分支建立初始提交
        if self.is_git_repo() and not self.git.has_commits():
            try:
                self.git.ensure_gitignore([
                    f"{tasks_dir.strip('/')}/",
                    guide_file,
                    f"{WORKTREE_DIR}/",
                ])
                self.git.add_all()
                self.git.commit("chore: 初始化项目仓库")
                log.info("空仓库已自动创建初始提交")
            except GitError as e:
                log.warning("空仓库创建初始提交失败：%s", e)

        if self.is_git_repo():
            self.git.ensure_local_exclude([f"{WORKTREE_DIR}/"])

        main_branch = self.allowed_branch or self.git.current_branch()
        use_stage = self.worktree_isolation and not self.commit_and_merge
        if use_stage:
            lock = self.merge_lock or contextlib.nullcontext()
            with lock:
                self.git.ensure_stage_branch(
                    self.stage_branch(), main_branch, stage_worktree=self.stage_worktree()
                )
            base_branch = self.stage_branch()
        else:
            base_branch = main_branch

        is_rework = bool(task and (task.review_round > 0 or task.review_pending))

        if target_path.exists() and (target_path / ".git").exists():
            if not self.per_task_worktree:
                if self.branch_exists(target_branch):
                    wt_git.run("reset", "--hard", target_branch)
                else:
                    wt_git.run("reset", "--hard")
            elif is_rework and self.branch_exists(target_branch):
                wt_git.run("reset", "--hard", target_branch)
            else:
                wt_git.run("checkout", "-B", target_branch, base_branch)
            wt_git.run("clean", "-fd")
            return target_branch

        if target_path.exists():
            shutil.rmtree(target_path, ignore_errors=True)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(Exception):
            self.git.run("worktree", "prune")

        if self.per_task_worktree:
            if is_rework and self.branch_exists(target_branch):
                self.git.run("worktree", "add", str(target_path), target_branch)
            else:
                self.git.run("worktree", "add", "-B", target_branch, str(target_path), base_branch)
        else:
            if self.branch_exists(target_branch):
                self.git.run("worktree", "add", str(target_path), target_branch)
            else:
                self.git.run("worktree", "add", "-b", target_branch, str(target_path), base_branch)
        return target_branch

    def cleanup(self, wt_path: Path | None = None) -> None:
        """任务结束后清空 Worktree 的未提交改动。"""
        target = wt_path or self.default_worktree
        if not self.worktree_isolation or not (target / ".git").exists():
            return
        try:
            wt_git = Git(target)
            wt_git.run("reset", "--hard")
            wt_git.run("clean", "-fd")
        except GitError as e:
            log.warning("清理 worktree 失败：%s", e)

    def remove(self, wt_path: Path | None = None, branch: str | None = None) -> None:
        """彻底移除 Worktree 目录并清理关联临时分支。"""
        target = wt_path or self.default_worktree
        if not self.is_git_repo():
            return
        if (target / ".git").exists():
            try:
                self.git.run("worktree", "remove", "--force", str(target))
            except GitError as e:
                log.warning("移除 worktree 失败：%s", e)
        if branch and self.per_task_worktree:
            self.git.delete_branch(branch, force=True)

    def session(self, task: Task, active: bool = True) -> WorktreeSession:
        """创建单次任务的 Worktree 生命周期上下文会话。"""
        return WorktreeSession(self, task, active=active)


class WorktreeSession:
    """管理单次任务执行期间 Worktree 生命周期上下文管理器。"""

    def __init__(self, manager: WorktreeManager, task: Task, active: bool = True) -> None:
        self.manager = manager
        self.task = task
        self.active = active
        self.wt_path = manager.task_worktree(task)
        self.branch = manager.task_branch(task)

    def __enter__(self) -> Path:
        if self.active:
            self.manager.setup(self.task, self.branch, self.wt_path)
            return self.wt_path
        return self.manager.project_path

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.active and exc_type is not None:
            self.manager.cleanup(self.wt_path)


__all__ = [
    "WORKTREE_DIR",
    "WorktreeManager",
    "WorktreeSession",
    "safe_wt_name",
]
