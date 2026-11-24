"""Optional deterministic verification on an isolated latest-main merge candidate."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path
from threading import Event

from .git_ops import Git, GitError
from .tools_cmd import (check_command_allowed, _augmented_env, _decode_proc_output,
                        _kill_process_tree, _resolve_new_install, _windows_startupinfo,
                        register_child_proc, unregister_child_proc)


class PremergeError(RuntimeError):
    """A test or isolation failure; never treat this as a merge conflict."""


class PremergeConflict(PremergeError):
    """Candidate merge conflict eligible for existing conflict healing."""


def stage_matches_main_workspace(repo: Git, main_branch: str,
                                 stage_branch: str) -> bool:
    """当前未提交主工作区内容恰等于 stage 相对主分支的已验收增量。"""
    if repo.current_branch() != main_branch:
        return False
    main_sha = repo.rev_parse(main_branch)
    if repo.run("merge-base", main_branch, stage_branch) != main_sha:
        return False
    if repo.run("ls-files", "--others", "--exclude-standard"):
        return False
    return (repo.run("diff", "--binary", "HEAD") ==
            repo.run("diff", "--binary", main_branch, stage_branch))


def verify_candidate(repo: Git, base_branch: str, source_ref: str,
                     command: list[str], allowed: list[str] | None,
                     stop_event: Event | None = None,
                     extra_env: dict[str, str] | None = None,
                     allow_stage_workspace: bool = False) -> tuple[str, str]:
    """Verify WIP against a clean current main; return (main SHA, truncated output)."""
    ok, reason = check_command_allowed(command, allowed)
    if not ok:
        raise PremergeError(reason)
    if stop_event is not None and stop_event.is_set():
        raise PremergeError("任务已停止，合入前重测取消")
    if not repo.has_commits():
        raise PremergeError("空仓库尚无可重测的主分支基线")
    main_branch = repo.current_branch()
    if not allow_stage_workspace and main_branch != base_branch:
        raise PremergeError("主工作区不在项目允许的主分支，无法安全重测")
    main_before = repo.rev_parse(main_branch)
    if allow_stage_workspace:
        if not stage_matches_main_workspace(repo, main_branch, base_branch):
            raise PremergeError("主工作区改动与 stage 不一致，无法证明候选包含全部已落地改动")
        workspace_before = repo.run("diff", "--binary", "HEAD")
    elif repo.is_dirty():
        raise PremergeError("主工作区存在未提交改动，无法证明合入前重测覆盖全部变更")
    else:
        workspace_before = ""
    main_sha = repo.rev_parse(base_branch)
    source_sha = repo.rev_parse(source_ref)
    if not main_sha or not source_sha:
        raise PremergeError("主分支或任务提交不存在")

    with tempfile.TemporaryDirectory(prefix="orcsy-premerge-") as td:
        candidate = Path(td) / "candidate"
        repo.run("worktree", "add", "--detach", str(candidate), main_sha)
        try:
            wt = Git(candidate)
            try:
                wt.run("merge", "--no-commit", "--no-ff", source_sha)
            except GitError as exc:
                raise PremergeConflict(f"候选代码与最新主分支冲突：{exc}") from exc
            candidate_tree = wt.run("write-tree")
            env = _augmented_env()
            env.update(extra_env or {})
            command = _resolve_new_install(command)
            with tempfile.TemporaryFile() as log_file:
                try:
                    proc = subprocess.Popen(
                        command, cwd=candidate, env=env, shell=False,
                        stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
                        startupinfo=_windows_startupinfo(),
                        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                        | getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except OSError as exc:
                    raise PremergeError(f"重测命令无法启动：{exc}") from exc
                register_child_proc(proc)
                started = time.monotonic()
                try:
                    while proc.poll() is None:
                        if stop_event is not None and stop_event.is_set():
                            raise PremergeError("任务已停止，合入前重测取消")
                        if time.monotonic() - started >= 600:
                            raise PremergeError("合入前重测超时（600 秒）")
                        if os.fstat(log_file.fileno()).st_size > 16 * 1024 * 1024:
                            raise PremergeError("合入前重测输出超过 16 MiB，已停止")
                        try:
                            proc.wait(timeout=0.2)
                        except subprocess.TimeoutExpired:
                            continue
                finally:
                    try:
                        if proc.poll() is None:
                            _kill_process_tree(proc)
                            try:
                                proc.wait(timeout=3)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                                proc.wait(timeout=3)
                    finally:
                        unregister_child_proc(proc)
                log_file.seek(0, os.SEEK_END)
                size = log_file.tell()
                if size > 16 * 1024 * 1024:
                    raise PremergeError("合入前重测输出超过 16 MiB")
                log_file.seek(max(0, size - 32_000))
                text = _decode_proc_output(log_file.read())[-16_000:]
            if stop_event is not None and stop_event.is_set():
                raise PremergeError("任务已停止，合入前重测取消")
            if proc.returncode != 0:
                raise PremergeError(f"合入前重测失败（退出码 {proc.returncode}）：{text[-2000:]}")
            if (wt.rev_parse("HEAD") != main_sha or wt.run("write-tree") != candidate_tree
                    or wt.run("diff")):
                raise PremergeError("重测命令修改了候选代码或 Git 索引，拒绝合入未经验证的变更")
            if repo.rev_parse(base_branch) != main_sha or repo.rev_parse(main_branch) != main_before:
                raise PremergeError("重测期间主分支发生变化，候选结果已过期")
            if allow_stage_workspace and (
                not stage_matches_main_workspace(repo, main_branch, base_branch)
                or repo.run("diff", "--binary", "HEAD") != workspace_before
            ):
                raise PremergeError("重测期间主工作区发生变化，候选结果已过期")
            if not allow_stage_workspace and repo.is_dirty():
                raise PremergeError("重测期间主工作区出现未提交改动，候选结果已过期")
            return main_sha, text
        finally:
            Git(candidate).run("merge", "--abort", check=False)
            repo.run("worktree", "remove", "--force", str(candidate), check=False)
