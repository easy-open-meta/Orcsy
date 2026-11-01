"""单任务执行器：准备环境 → Agent 工具循环改代码 → 依据结论提交/回写状态。

git 项目的执行流程：
1. 在 <项目根>/.worktree-arranger 创建/复用独立 worktree（隔离 Agent 的改动）；
2. Agent 通过工具（list_dir/read_file/write_file/edit_file/grep/run_command）
   在 worktree 里探索与修改代码，最后调用 finish 给出结论；
3. action=completed 时：worktree 内 commit → 主仓库 merge → push（如有远端）；
   blocked/cannot_do 时：丢弃 worktree 变更，主仓库任务状态置为 blocked。
非 git 项目：Agent 直接在项目目录工作，只回写任务状态，不提交。
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .approvals import args_preview, get_manager, requires_approval
from .config import LLMConfig, ProjectConfig, TaskStatus
from .git_ops import Git, GitError
from .llm import AgentResponse, LLMClient, LLMError, finish_response
from .logging_setup import get_logger
from .prompts import build_system_prompt
from .skills import Skill, skill_bodies
from .task_model import Task, append_progress
from .tools import TOGGLEABLE_TOOLS, FinishSignal, ToolContext

WORKTREE_DIR = ".worktree-arranger"


@dataclass
class TaskResult:
    task_id: str
    outcome: TaskStatus            # done / blocked / cancelled
    summary: str = ""
    notes: str = ""
    commit: str = ""               # 生成的 commit 短哈希
    pushed: bool = False
    duration_seconds: float = 0.0
    llm_error: str = ""            # LLM/网络层面的错误（区别于业务 blocked）
    files_changed: list[str] = field(default_factory=list)
    turns: int = 0                 # Agent 工具循环轮数
    prompt_tokens: int = 0         # 输入 token 累计（各轮之和，含历史重发）
    completion_tokens: int = 0     # 输出 token 累计


class TaskExecutor:
    def __init__(self, project: ProjectConfig, llm: LLMConfig,
                 tools: dict[str, bool] | None = None,
                 permission_mode: str = "full_access",
                 permission_timeout_minutes: int = 30) -> None:
        self.project = project
        self.llm = llm
        # 生效的工具开关 map（resolve_tools 的产物）；未列出的工具默认启用
        self.tools_map: dict[str, bool] = dict(tools or {})
        # 生效的访问权限模式（resolve_permission_mode 的产物）与审批超时
        self.permission_mode = permission_mode
        self.permission_timeout_minutes = permission_timeout_minutes
        self.git = Git(project.path)
        self.is_git_repo = project.path.is_dir() and self.git.is_repo()
        self.worktree = project.path / WORKTREE_DIR
        self.worktree_git = Git(self.worktree)
        self.log = get_logger(f"executor.{project.name}")

    def _make_approval_gate(self, task: Task):
        """按访问权限模式构建本任务的审批门；full_access 返回 None（零开销）。

        闭包持有 always_allowed 集合：「本任务内始终允许」批准后，同一工具
        在本任务剩余轮次不再挂起（任务边界即失效，下个任务重新询问）。
        """
        if self.permission_mode == "full_access":
            return None
        mode = self.permission_mode
        timeout_minutes = max(1, int(self.permission_timeout_minutes or 30))
        timeout_s = timeout_minutes * 60
        manager = get_manager()
        project_name = self.project.name
        always_allowed: set[str] = set()

        def gate(name: str, args: dict) -> str | None:
            if not requires_approval(mode, name) or name in always_allowed:
                return None
            brief = args_preview(args, limit=120)
            append_progress(task.path,
                            f"⏸ 等待审批：{name} {brief}"
                            f"（{timeout_minutes} 分钟未响应将自动拒绝）")
            outcome = manager.request(project_name, task.id, name, args, mode, timeout_s)
            if outcome.approved:
                if outcome.always:
                    always_allowed.add(name)
                    append_progress(task.path, f"✅ 审批通过：{name}（本任务内后续不再询问）")
                else:
                    append_progress(task.path, f"✅ 审批通过：{name}")
                return None
            if outcome.timed_out:
                append_progress(task.path,
                                f"⏱ 审批超时（{timeout_minutes} 分钟未响应），已自动拒绝：{name}")
                return ("错误：用户未在时限内响应审批，该操作已被自动拒绝且未执行。"
                        "不要原样重试；调整方案继续，或调用 finish（action=blocked）说明受阻原因")
            append_progress(task.path, f"⛔ 审批被拒绝：{name}")
            return ("错误：用户拒绝了该操作（未执行）。不要原样重试；"
                    "调整方案继续，或调用 finish（action=blocked）说明受阻原因")

        return gate

    def _enabled_tools(self) -> frozenset[str] | None:
        """把开关 map 解析成启用工具名集合；全启用时返回 None（不过滤，保持旧行为）。"""
        if not self.tools_map:
            return None
        enabled = {name for name in TOGGLEABLE_TOOLS if self.tools_map.get(name, True)}
        if len(enabled) == len(TOGGLEABLE_TOOLS):
            return None
        return frozenset(enabled | {"finish"})  # finish 是协议工具，恒可用

    # ---- 对外 ----

    def run_task(self, task: Task, guide_content: str,
                 skills: list[Skill] | None = None) -> TaskResult:
        """执行一个任务，返回结果。本方法不抛异常——所有失败都体现在结果里。"""
        started = time.monotonic()
        self.log.info("开始执行任务 %s [%s] %s", task.id, task.priority, task.title)
        # 进度实时落盘到任务文件：进程被强杀时，已完成的轮次都留在文件里
        append_progress(task.path,
                        f"开始执行（{task.priority}，最多 {self.project.max_agent_turns} 轮）")

        result = TaskResult(task_id=task.id, outcome="blocked")
        usage: dict = {}
        try:
            response = self._agent_loop(
                task, guide_content, skills or [], usage_out=usage,
                on_progress=lambda line: append_progress(task.path, line))
            result.turns = int(usage.get("turns", 0))
            result.prompt_tokens = int(usage.get("prompt_tokens", 0))
            result.completion_tokens = int(usage.get("completion_tokens", 0))
            result.summary = response.summary
            result.notes = response.notes
            result.files_changed = response.files_changed
            if response.action == "completed":
                if self.is_git_repo:
                    merged = self._commit_and_merge(task, response, result)
                    result.outcome = "done" if merged else "blocked"
                else:
                    result.outcome = "done"
            else:
                self._cleanup_worktree_changes()
                result.outcome = "blocked" if response.action == "blocked" else "cancelled"
        except LLMError as e:
            self.log.error("任务 %s 调用大模型失败：%s", task.id, e)
            result.llm_error = str(e)
            result.notes = f"调用大模型失败：{e}"
            result.turns = int(usage.get("turns", 0))
            result.prompt_tokens = int(usage.get("prompt_tokens", 0))
            result.completion_tokens = int(usage.get("completion_tokens", 0))
            self._cleanup_worktree_changes()
        except Exception as e:  # noqa: BLE001 — 单任务失败不能拖垮调度循环
            self.log.exception("任务 %s 执行出现未预期异常", task.id)
            result.llm_error = str(e)
            result.notes = f"执行异常：{e}"
            result.turns = int(usage.get("turns", 0))
            result.prompt_tokens = int(usage.get("prompt_tokens", 0))
            result.completion_tokens = int(usage.get("completion_tokens", 0))
            self._cleanup_worktree_changes()

        # 任务结束后移除 worktree：改动要么已合并要么已放弃，避免残留未跟踪目录
        if result.outcome in ("done", "cancelled"):
            self.remove_worktree()

        result.duration_seconds = round(time.monotonic() - started, 1)
        self.log.info(
            "任务 %s 结束：%s（%.1fs）%s",
            task.id, result.outcome, result.duration_seconds,
            result.commit and f"commit={result.commit}",
        )
        return result

    # ---- Agent 循环 ----

    def _agent_loop(self, task: Task, guide_content: str, skills: list[Skill],
                    on_progress=None, usage_out: dict | None = None) -> AgentResponse:
        work_root = self.worktree if self.is_git_repo else self.project.path
        if self.is_git_repo:
            branch = self._prepare_worktree()
            self.log.debug("worktree 就绪，工作分支：%s", branch)

        # 技能正文按任务新鲜预载：read_skill 工具直接查内存，不碰文件系统
        enabled = self._enabled_tools()
        ctx = ToolContext(root=work_root, allowed_commands=self.project.allowed_commands,
                          skills=skill_bodies(skills), enabled_tools=enabled,
                          approval_gate=self._make_approval_gate(task))
        client = LLMClient(self.llm)
        client.reset(build_system_prompt(self.project, guide_content, self.is_git_repo,
                                         skills=skills, tools=enabled,
                                         permission_mode=self.permission_mode))

        try:
            resp = client.run_task_loop(self._build_task_prompt(task, enabled), ctx,
                                        max_turns=self.project.max_agent_turns,
                                        on_progress=on_progress)
        except FinishSignal as sig:
            # run_task_loop 内部会捕获并转换；这里兜底处理（替身/异常路径）
            resp = finish_response(sig.payload)
        finally:
            # 任何结束路径（含 LLM 异常）都回收 token 计量
            if usage_out is not None:
                usage_out.update(getattr(client, "usage", {}) or {})
        return resp

    # ---- worktree 管理 ----

    def _prepare_worktree(self) -> str:
        """创建或复用 worktree，返回其工作分支名。"""
        branch = f"arranger/{self.project.name}"
        if self.worktree.exists() and (self.worktree / ".git").exists():
            wt = self.worktree_git
            if self._branch_exists(branch):
                wt.run("reset", "--hard", branch)
            else:
                wt.run("reset", "--hard")
            wt.run("clean", "-fd")
            return branch
        if self.worktree.exists():
            shutil.rmtree(self.worktree)

        self.project.path.mkdir(parents=True, exist_ok=True)
        if self._branch_exists(branch):
            self.git.run("worktree", "add", str(self.worktree), branch)
        else:
            self.git.run("worktree", "add", "-b", branch, str(self.worktree))
        return branch

    def _branch_exists(self, branch: str) -> bool:
        return bool(self.git.run("branch", "--list", branch).strip())

    def _cleanup_worktree_changes(self) -> None:
        """任务结束后清空 worktree 的未提交改动。"""
        if not self.is_git_repo or not (self.worktree / ".git").exists():
            return
        try:
            self.worktree_git.run("reset", "--hard")
            self.worktree_git.run("clean", "-fd")
        except GitError as e:
            self.log.warning("清理 worktree 失败：%s", e)

    def remove_worktree(self) -> None:
        """整个移除 worktree（任务全部完成后调用，避免主仓库出现未跟踪目录）。"""
        if not self.is_git_repo or not (self.worktree / ".git").exists():
            return
        try:
            self.git.run("worktree", "remove", "--force", str(self.worktree))
        except GitError as e:
            self.log.warning("移除 worktree 失败：%s", e)

    # ---- 提交与合并 ----

    def _commit_and_merge(self, task: Task, response: AgentResponse, result: TaskResult) -> bool:
        """提交 worktree 改动并合回主分支。返回是否成功完成合并。"""
        message = response.commit_message or f"fix: {task.title}"
        committed = self.worktree_git.commit(message)
        if not committed:
            self.log.warning("模型报告 completed 但 worktree 无任何改动（任务 %s）", task.id)
            result.notes = (result.notes + "\n" if result.notes else "") + \
                "注意：Agent 报告完成但未产生文件改动，任务按 blocked 处理"
            return False

        result.commit = self.worktree_git.run("rev-parse", "--short", "HEAD")

        # 合回主分支
        main_branch = self.project.allowed_branch or self.git.current_branch()
        try:
            self.git.checkout(main_branch)
            self.git.run("merge", "--no-ff", "-m", f"Merge arranger work for {task.id}",
                         result.commit)
        except GitError:
            self.log.exception("合并任务 %s 的提交失败，保留分支 %s 供人工处理",
                               task.id, f"arranger/{self.project.name}")
            result.notes = (result.notes + "\n" if result.notes else "") + \
                f"合并主分支失败，提交保留在 arranger/{self.project.name} 分支，需人工介入"
            return False

        result.pushed = False
        if self.git.has_remote("origin"):
            try:
                self.git.push("origin", main_branch)
                result.pushed = True
            except GitError as e:
                self.log.warning("推送失败（不影响本地结果）：%s", e)

        self._cleanup_worktree_changes()
        return True

    # ---- 任务提示词 ----

    def _build_task_prompt(self, task: Task,
                           enabled: frozenset[str] | None = None) -> str:
        # 工作要求里点名的工具按启用集过滤：被禁的工具不出现在提示词里，
        # 避免模型反复尝试调用一个不存在的工具浪费轮次
        def on(*names: str) -> bool:
            return enabled is None or any(n in enabled for n in names)

        explore = " / ".join(n for n in ("list_dir", "read_file", "find_files", "grep")
                             if on(n)) or "可用工具"
        modify = " / ".join(n for n in ("write_file", "edit_file", "multi_edit",
                                        "delete_file", "move_file") if on(n))
        lines = [
            f"1. 先用 {explore} 了解项目结构与相关代码，遵守 AGENT.md 指引。",
        ]
        if modify:
            lines.append(f"2. 用 {modify} 修改代码"
                         + ("；用 run_command 运行测试或构建验证。" if on("run_command")
                            else "。"))
        elif on("run_command"):
            lines.append("2. 用 run_command 运行测试或构建验证。")
        lines += [
            f"{len(lines) + 1}. 验证通过后调用 finish 工具（action=completed）给出结论与提交信息。",
            f"{len(lines) + 1}. 无法完成时同样调用 finish（action=blocked 或 cannot_do），说明原因。",
            f"{len(lines) + 1}. 所有路径都使用相对项目根的相对路径。",
        ]
        return (
            f"# 当前任务\n\n"
            f"- 任务 ID：{task.id}\n"
            f"- 标题：{task.title}\n"
            f"- 优先级：{task.priority}\n"
            f"- 类型：{task.kind}\n"
            f"- 标签：{', '.join(task.labels) if task.labels else '无'}\n\n"
            f"## 任务内容\n\n{task.description}\n\n"
            f"## 工作要求\n\n" + "\n".join(lines)
        )
