"""Git 操作封装：状态查询、分支、提交、推送。

全部走 `git -C <repo>`，不做任何交互；失败抛 GitError 由上层决定如何处理。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .logging_setup import get_logger

log = get_logger("git")

# 无窗口实例（托盘 exe）派生控制台程序时，Windows 会自动分配新控制台导致
# 任务执行期间黑窗闪现；CREATE_NO_WINDOW 抑制之（非 Windows 平台为 0）
_SUBPROCESS_FLAGS = (
    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
)


class GitError(RuntimeError):
    pass


class Git:
    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def _run(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_SUBPROCESS_FLAGS,
        )
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} 失败：{proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout.strip()

    def run(self, *args: str, check: bool = True) -> str:
        """公开的 git 命令入口，供其它模块组合使用。"""
        return self._run(*args, check=check)

    # ---- 查询 ----

    def is_repo(self) -> bool:
        proc = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, creationflags=_SUBPROCESS_FLAGS,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def current_branch(self) -> str:
        return self._run("rev-parse", "--abbrev-ref", "HEAD")

    def status_porcelain(self) -> list[str]:
        out = self._run("status", "--porcelain")
        return [line for line in out.splitlines() if line.strip()]

    def is_dirty(self) -> bool:
        return bool(self.status_porcelain())

    def untracked_in(self, rel_path: str) -> bool:
        """指定相对路径下是否存在未跟踪文件（如任务目录不在 .gitignore 中时）。"""
        out = self._run("ls-files", "--others", "--exclude-standard", "--", rel_path)
        return bool(out.strip())

    # ---- 变更 ----

    def add_all(self) -> None:
        self._run("add", "-A")

    def commit(self, message: str) -> bool:
        """提交全部变更。工作区干净时返回 False（不产生空提交）。"""
        if not self.is_dirty():
            return False
        self._run("add", "-A")
        proc = subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", message],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            raise GitError(f"git commit 失败：{proc.stderr.strip() or proc.stdout.strip()}")
        return True

    def push(self, remote: str, branch: str) -> None:
        self._run("push", remote, branch)

    def has_remote(self, remote: str = "origin") -> bool:
        try:
            out = self._run("remote")
            return remote in out.splitlines()
        except GitError:
            return False

    def checkout(self, branch: str, create: bool = False) -> None:
        if create:
            self._run("checkout", "-B", branch)
        else:
            self._run("checkout", branch)
