"""Git 操作封装：状态查询、分支、提交、推送。

全部走 `git -C <repo>`，不做任何交互；失败抛 GitError 由上层决定如何处理。
"""

from __future__ import annotations

import shutil
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

    def _status_raw_lines(self) -> list[str]:
        """原始 porcelain 行（不做整体 strip）。

        status_porcelain() 的 _run 会整体 strip 掉首行开头的空格，导致
        ` M path`（工作区改动）被裁成 `M path`、固定 2 位状态前缀错位。
        需要精确解析 XY 前缀时用本方法。
        """
        proc = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            startupinfo=_windows_startupinfo(), creationflags=_SUBPROCESS_FLAGS,
        )
        if proc.returncode != 0:
            raise GitError(f"git status --porcelain 失败：{proc.stderr.strip()}")
        return [ln for ln in proc.stdout.splitlines() if ln.strip()]

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

    def _status_entries(self) -> list[dict]:
        """解析 status --porcelain 为结构化条目：[{xy, paths, display, delete_only}]。

        - 普通改动/未跟踪：paths=[p]，display=p
        - 重命名/复制（XY 首字母 R/C）：`OLD -> NEW` → paths=[OLD, NEW]，
          display="OLD -> NEW"，任一路径被排除则整条不提交
        - 删除（XY 含 D）：delete_only=True（用于「暂存删除」判定）
        文件名为含空格/中文时 porcelain 会加引号，这里统一去引号。
        """
        entries: list[dict] = []
        for ln in self._status_raw_lines():
            if len(ln) < 4:
                continue
            xy = ln[:2]
            rest = ln[3:]
            renamed = xy[0] in ("R", "C") and " -> " in rest
            if renamed:
                old, new = rest.split(" -> ", 1)
                paths = [old.strip().strip('"'), new.strip().strip('"')]
                display = f"{paths[0]} -> {paths[1]}"
            else:
                p = rest.strip().strip('"').rstrip("/")  # 未跟踪目录去掉结尾斜杠，便于前缀匹配
                paths = [p]
                display = p
            entries.append({
                "xy": xy,
                "paths": [x for x in paths if x],
                "display": display,
                "delete_only": "D" in xy,
            })
        return entries

    def pending_changes_summary(self) -> dict:
        """统计主工作区当前未提交的改动文件信息（看板「待人工审阅提交」展示用）。"""
        if not self.is_repo():
            return {"is_git": False, "count": 0, "files": []}
        try:
            entries = self._status_entries()
        except GitError:
            return {"is_git": True, "count": 0, "files": []}
        files = [e["display"] for e in entries]
        # 上限放宽到 2000：批量生成/删除时也要能看全，避免列表被截断
        return {"is_git": True, "count": len(files), "files": files[:2000]}

    # diff 预览上限：人工把关要看全，给足空间（1MB 文本 ≈ 数万行，浏览器仍可流畅滚动）
    _DIFF_MAX_CHARS = 1_000_000
    _DIFF_MAX_NEW_FILE_LINES = 5000

    def file_diff(self, file_path: str) -> dict:
        """单个文件在主工作区的未提交改动 diff（人工把关提交页预览用）。

        返回 {diff, truncated, total_chars, lines}：
        - diff：diff 文本；未跟踪的新文件返回带「新增文件」标注的全文预览
        - truncated：是否因超长被截断（前端据此提示并提供下载完整补丁）
        - total_chars / lines：原始总量（截断时告知真实规模）

        file_path 为相对仓库根的路径（与 pending_changes_summary 的 files 同源）。
        路径穿越/绝对路径拒绝。
        """
        def _pack(text: str, total_chars: int, truncated: bool) -> dict:
            return {"diff": text, "truncated": truncated,
                    "total_chars": total_chars,
                    "lines": text.count("\n") + (1 if text else 0)}

        if not self.is_repo():
            raise GitError("项目不是 git 仓库，无法查看 diff")
        rel = str(file_path or "").strip()
        # 重命名条目（`OLD -> NEW`）预览新路径的内容
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1].strip().strip('"')
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
            # 无 diff 说明是未跟踪的新文件：给出「新增文件」形态全文预览
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
                    return _pack(f"（无法读取新文件内容：{e}）", 0, False)
                all_lines = content.splitlines()
                total_lines = len(all_lines)
                shown = all_lines[:self._DIFF_MAX_NEW_FILE_LINES]
                body = "\n".join(shown)
                truncated = total_lines > len(shown)
                # 超长单行（如压缩产物/长 Base64）也要按字符截断，避免撑爆浏览器
                if len(body) > self._DIFF_MAX_CHARS:
                    body = body[:self._DIFF_MAX_CHARS]
                    truncated = True
                if truncated:
                    body += (f"\n…（共 {total_lines} 行 / {len(content):,} 字符，已截断；"
                             "可用「下载补丁」获取本文件全部改动）")
                return _pack(f"（新增文件 · 共 {total_lines} 行）\n{body}",
                             len(content), truncated)
            return _pack("（该文件当前无未提交改动，可能已被其他流程提交）", 0, False)
        total_chars = len(diff_text)
        if total_chars > self._DIFF_MAX_CHARS:
            cut = diff_text[:self._DIFF_MAX_CHARS]
            return _pack(
                cut + (f"\n…（diff 共 {total_chars:,} 字符，已截断至 "
                       f"{self._DIFF_MAX_CHARS:,}；可用「下载补丁」查看完整内容）"),
                total_chars, True)
        return _pack(diff_text, total_chars, False)

    def discard_changes(self, files: list[str]) -> dict:
        """丢弃指定文件的未提交改动（可视化管理临时/测试产物）。

        - 未跟踪（`??`）：从磁盘物理删除（文件或目录，目录递归删除）
        - 已跟踪：取消暂存并还原到 HEAD（`git reset` + `git checkout --`），
          即「撤销本次改动」，不会删除仓库里的原始文件
        只接受当前 status 中真实存在的条目名（用户传入的名字仅用于匹配，
        不直接拼进删除路径），避免任意路径删除风险。
        """
        wanted: set[str] = set()
        for f in files or []:
            s = str(f).strip().strip('"')
            if " -> " in s:
                s = s.split(" -> ", 1)[1].strip()
            if s:
                wanted.add(s)
        if not wanted:
            raise GitError("未指定要丢弃的文件")
        entries = self._status_entries()
        deleted: list[str] = []
        restored: list[str] = []
        skipped: list[str] = []
        for e in entries:
            if not (e["display"] in wanted or any(p in wanted for p in e["paths"])):
                continue
            if e["xy"] == "??":
                for p in e["paths"]:
                    target = self.repo / p
                    try:
                        if target.is_dir():
                            shutil.rmtree(target)
                        elif target.exists():
                            target.unlink()
                        deleted.append(p)
                    except OSError as err:
                        skipped.append(f"{p}（{err}）")
            else:
                try:
                    self._run("reset", "-q", "--", *e["paths"], check=False)
                    self._run("checkout", "--", *e["paths"])
                    restored.append(e["display"])
                except GitError as err:
                    skipped.append(f"{e['display']}（{err}）")
        if not deleted and not restored and not skipped:
            raise GitError("没有匹配到可丢弃的改动（可能已提交或被其他流程处理）")
        return {"deleted": deleted, "restored": restored, "skipped": skipped}

    def commit_and_push_manual(self, message: str, *, push: bool = False,
                               branch: str | None = None,
                               exclude: list[str] | None = None) -> dict:
        """人工确认把关后执行的最终主分支提交（可选推送 + 排除部分文件）。

        exclude：不纳入本次提交的改动（如临时测试产物）。命中任一文件名的整条
        改动都不暂存——重命名条目（OLD -> NEW）只要任一端被排除即整条跳过。
        被排除的文件**保留在工作区**（不删除、不回滚），后续仍会出现在待提交
        列表中，可再次选择提交或自行删除。全部改动都被排除时报错，不产生空提交。
        """
        msg = str(message or "").strip()
        if not msg:
            raise GitError("提交信息（commit message）不能为空")
        if not self.is_dirty():
            raise GitError("主工作区当前没有可提交的改动")
        exclude_set = {str(x).strip().strip('"') for x in (exclude or []) if str(x).strip()}

        def _hit_exclude(paths: list[str]) -> bool:
            """判断条目是否被排除：支持「目录 ↔ 目录内文件」双向前缀匹配。

            git 对未跟踪目录只报目录本身（`?? __pycache__/`），用户排除的往往是
            目录内具体文件；反过来排除父目录也应覆盖其下所有文件。
            """
            if not exclude_set:
                return False
            for p in paths:
                if p in exclude_set:
                    return True
                pfx = p.rstrip("/") + "/"
                if any(e.startswith(pfx) for e in exclude_set):
                    return True
                if any(p.startswith(e.rstrip("/") + "/") for e in exclude_set):
                    return True
            return False

        entries = self._status_entries()
        staged_paths: list[str] = []
        skipped: list[str] = []
        for e in entries:
            if _hit_exclude(e["paths"]):
                skipped.append(e["display"])
            else:
                staged_paths.extend(e["paths"])
        if not staged_paths:
            raise GitError("所有改动都被排除，没有可提交的文件（请至少保留一个文件）")

        # 先清空索引（不动工作区），再仅暂存未被排除的路径（含删除），
        # 避免用户此前已手动 git add 的排除文件被顺带提交
        self._run("reset")
        self._run("add", "-A", "--", *staged_paths)
        proc = subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", msg],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            startupinfo=_windows_startupinfo(), creationflags=_SUBPROCESS_FLAGS,
        )
        if proc.returncode != 0:
            raise GitError(f"git commit 失败：{proc.stderr.strip() or proc.stdout.strip()}")
        committed = True
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
            "skipped": skipped,
        }


__all__ = [
    "Git",
    "GitError",
]


