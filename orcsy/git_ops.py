"""Git 操作封装：状态查询、分支、提交、推送。

全部走 `git -C <repo>`，不做任何交互；失败抛 GitError 由上层决定如何处理。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .logging_setup import get_logger
from .tools import _windows_startupinfo

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
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            startupinfo=_windows_startupinfo(),
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
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True,
            startupinfo=_windows_startupinfo(),
            creationflags=_SUBPROCESS_FLAGS,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def has_commits(self) -> bool:
        """仓库是否已有至少一个 commit。"""
        proc = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "--verify", "HEAD"],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True,
            startupinfo=_windows_startupinfo(),
            creationflags=_SUBPROCESS_FLAGS,
        )
        return proc.returncode == 0

    def current_branch(self) -> str:
        # git branch --show-current 在空仓库（尚未产生任何 commit）也能正确返回当前分支名（如 main）
        proc = subprocess.run(
            ["git", "-C", str(self.repo), "branch", "--show-current"],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True,
            startupinfo=_windows_startupinfo(),
            creationflags=_SUBPROCESS_FLAGS,
        )
        branch = proc.stdout.strip()
        if proc.returncode == 0 and branch:
            return branch
        # 分离头指针或老版本 Git 兜底
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

    def init(self) -> None:
        """git init 当前目录（幂等：已是仓库时 no-op，不报错）。"""
        if self.is_repo():
            return
        self._run("init")

    def ensure_gitignore(self, entries: list[str]) -> list[str]:
        """把 entries 追加进 .gitignore（幂等：已有的条目不重复写）。

        逐行精确匹配（含注释行在内整行比对）判定是否已忽略，不做模糊
        contains——避免 `tasks` 误判已覆盖 `tasks/` 之类的半覆盖状态。
        返回实际新追加的条目；文件不存在则创建。失败抛 GitError 由上层兜底。
        """
        gitignore = self.repo / ".gitignore"
        existing: set[str] = set()
        if gitignore.exists():
            existing = {
                line.strip() for line in
                gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
            }
        missing = [e for e in entries if e not in existing]
        if not missing:
            return []
        with gitignore.open("a", encoding="utf-8") as f:
            # 追加前补一个空行，把新增段落与用户已有内容隔开
            if existing:
                f.write("\n")
            f.write("# Orcsy：任务队列 / 指引 / worktree 不入版本库\n")
            for e in missing:
                f.write(f"{e}\n")
        return missing

    def ensure_local_exclude(self, entries: list[str]) -> None:
        """把 entries 写入 .git/info/exclude（本地私有忽略，不改动仓库 .gitignore）。"""
        exclude = self.repo / ".git" / "info" / "exclude"
        if not exclude.parent.is_dir():
            return
        existing: set[str] = set()
        if exclude.exists():
            existing = {
                line.strip() for line in
                exclude.read_text(encoding="utf-8", errors="replace").splitlines()
            }
        missing = [e for e in entries if e not in existing]
        if not missing:
            return
        with exclude.open("a", encoding="utf-8") as f:
            if existing:
                f.write("\n")
            for e in missing:
                f.write(f"{e}\n")

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

    def branch_exists(self, branch: str) -> bool:
        return bool(self._run("branch", "--list", branch).strip())

    def rev_parse(self, ref: str = "HEAD") -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "--verify", ref],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            startupinfo=_windows_startupinfo(),
            creationflags=_SUBPROCESS_FLAGS,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def delete_branch(self, branch: str, force: bool = True) -> None:
        flag = "-D" if force else "-d"
        self._run("branch", flag, branch, check=False)

    def ensure_stage_branch(self, stage_branch: str, main_branch: str,
                            stage_worktree: Path | None = None) -> str:
        """确保集成分支存在；若主工作区干净，自动将其同步对齐到主分支最新 HEAD。

        设计原因：当人工在主分支检查无误并手动执行 git commit 后，主工作区变为
        干净且 main HEAD 前进；此时重置 stage 分支，确保后续任务严格基于人工
        确认后的最新基线继续开发。若主工作区仍有未提交的已落地改动（dirty），
        保留 stage 分支上的已验收进度以供同需求下游任务无缝衔接。
        """
        if not self.branch_exists(stage_branch):
            self._run("branch", stage_branch, main_branch)
        elif not self.is_dirty():
            if stage_worktree and (stage_worktree / ".git").exists():
                Git(stage_worktree).run("reset", "--hard", main_branch)
            else:
                self._run("branch", "-f", stage_branch, main_branch)
        return self.rev_parse(stage_branch)

    def merge_into_stage(
        self,
        stage_branch: str,
        stage_worktree: Path,
        source_ref: str,
        message: str,
        *,
        squash: bool = False,
        commit_msg: str = "",
    ) -> tuple[bool, str, str, str]:
        """在隔离的 stage worktree 中将 source_ref 合并入 stage_branch（绝不触碰主工作区）。

        返回 (ok, prev_hash, new_hash, error_detail)。
        """
        prev_hash = self.rev_parse(stage_branch)
        if not (stage_worktree / ".git").exists():
            if stage_worktree.exists():
                import shutil
                shutil.rmtree(stage_worktree, ignore_errors=True)
            self._run("worktree", "add", str(stage_worktree), stage_branch)

        wt = Git(stage_worktree)
        try:
            wt.run("reset", "--hard", stage_branch)
            wt.run("clean", "-fd")
            if squash:
                wt.run("merge", "--squash", source_ref)
                if wt.is_dirty():
                    wt.commit(commit_msg or message)
            else:
                wt.run("merge", "--no-ff", "-m", message, source_ref)
        except GitError as e:
            wt.run("merge", "--abort", check=False)
            wt.run("reset", "--hard", stage_branch, check=False)
            return False, prev_hash, prev_hash, str(e)

        new_hash = self.rev_parse(stage_branch)
        return True, prev_hash, new_hash, ""

    def apply_commit_delta_to_main(self, base_ref: str, head_ref: str) -> tuple[bool, str]:
        """将 base_ref..head_ref 的二进制安全增量 diff 落地到主工作区（不执行 commit/push）。

        使用原始 bytes 传递补丁并启用 --3way，防止 Windows CRLF/UTF-8 转换损毁，
        同时保留用户在主工作区其他未跟踪或未改动文件。
        """
        if not base_ref or not head_ref or base_ref == head_ref:
            return True, ""
        diff_proc = subprocess.run(
            ["git", "-C", str(self.repo), "diff", "--binary", base_ref, head_ref],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            startupinfo=_windows_startupinfo(),
            creationflags=_SUBPROCESS_FLAGS,
        )
        if diff_proc.returncode != 0:
            err = diff_proc.stderr.decode("utf-8", errors="replace").strip()
            return False, f"生成增量补丁失败：{err}"
        patch_bytes = diff_proc.stdout
        if not patch_bytes.strip():
            return True, ""

        apply_proc = subprocess.run(
            ["git", "-C", str(self.repo), "apply", "--3way"],
            input=patch_bytes,
            capture_output=True,
            startupinfo=_windows_startupinfo(),
            creationflags=_SUBPROCESS_FLAGS,
        )
        if apply_proc.returncode != 0:
            err = apply_proc.stderr.decode("utf-8", errors="replace").strip()
            return False, f"增量合入主工作区冲突：{err}"
        return True, ""

    def pending_changes_summary(self) -> dict:
        """统计主工作区当前未提交的改动文件信息（看板「待人工审阅提交」展示用）。"""
        if not self.is_repo():
            return {"is_git": False, "count": 0, "files": []}
        try:
            lines = self.status_porcelain()
        except GitError:
            return {"is_git": True, "count": 0, "files": []}
        files: list[str] = []
        for ln in lines:
            parts = ln.split(maxsplit=1)
            if len(parts) == 2:
                files.append(parts[1].strip())
        return {"is_git": True, "count": len(files), "files": files[:50]}

    def file_diff(self, file_path: str) -> str:
        """单个文件在主工作区的未提交改动 diff（人工把关提交弹窗预览用）。

        file_path 为相对仓库根的路径（与 pending_changes_summary 的 files
        同源）。未跟踪文件展示新增文件全文摘要；其余走 git diff -- <path>。
        路径穿越/绝对路径拒绝；diff 输出截断到 60K 字符防超长打爆看板。
        """
        if not self.is_repo():
            raise GitError("项目不是 git 仓库，无法查看 diff")
        rel = str(file_path or "").strip()
        if not rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
            raise GitError(f"非法的文件路径：{rel}")
        # porcelain 输出的文件名可能带引号（含空格/中文时），先去引号
        rel_clean = rel.strip('"')
        if not (self.repo / rel_clean).exists():
            raise GitError(f"文件不存在：{rel_clean}")
        proc = subprocess.run(
            ["git", "-C", str(self.repo), "diff", "--", rel_clean],
            capture_output=True, startupinfo=_windows_startupinfo(),
            creationflags=_SUBPROCESS_FLAGS,
        )
        diff_text = proc.stdout.decode("utf-8", errors="replace")
        if not diff_text.strip():
            # 无 diff 说明是未跟踪的新文件：给出「新增文件」形态预览（前 200 行）
            status = subprocess.run(
                ["git", "-C", str(self.repo), "status", "--porcelain", "--", rel_clean],
                capture_output=True, startupinfo=_windows_startupinfo(),
                creationflags=_SUBPROCESS_FLAGS,
            )
            if status.stdout.strip():
                try:
                    content = (self.repo / rel_clean).read_text(encoding="utf-8",
                                                                errors="replace")
                except OSError as e:
                    return f"（无法读取新文件内容：{e}）"
                lines = content.splitlines()
                head = "\n".join(lines[:200])
                more = f"\n…（共 {len(lines)} 行，已截断）" if len(lines) > 200 else ""
                return f"（新增文件 · 前 {min(200, len(lines))} 行预览）\n{head}{more}"
            return "（该文件当前无未提交改动，可能已被其他流程提交）"
        if len(diff_text) > 60000:
            diff_text = diff_text[:60000] + "\n…（diff 过长，已截断至 60K 字符）"
        return diff_text

    def commit_and_push_manual(self, message: str, *, push: bool = False,
                               branch: str | None = None) -> dict:
        """人工确认把关后执行的最终主分支提交（可选推送）。"""
        msg = str(message or "").strip()
        if not msg:
            raise GitError("提交信息（commit message）不能为空")
        if not self.is_dirty():
            raise GitError("主工作区当前没有可提交的改动")
        committed = self.commit(msg)
        commit_hash = self.rev_parse("HEAD")[:7] if committed else ""
        pushed = False
        push_note = ""
        target_branch = branch or self.current_branch()
        if push and committed and self.has_remote("origin"):
            try:
                self.push("origin", target_branch)
                pushed = True
            except GitError as e:
                push_note = str(e)
        return {
            "committed": committed,
            "commit": commit_hash,
            "pushed": pushed,
            "branch": target_branch,
            "push_note": push_note,
        }

