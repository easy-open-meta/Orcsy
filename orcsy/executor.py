"""单任务执行器：准备环境 → Agent 工具循环改代码 → 依据结论提交/回写状态。

git 项目的执行流程：
1. 在 <项目根>/.worktree-arranger 创建/复用独立 worktree（隔离 Agent 的改动）；
2. Agent 通过工具（list_dir/read_file/write_file/edit_file/grep/run_command）
   在 worktree 里探索与修改代码，最后调用 finish 给出结论；
3. action=completed 时：worktree 先 commit 到 arranger/<name> 工作分支 →
   （审查门开启时）独立审查 agent 合并前审查，通过才 merge → push（如有远端）；
   审查不通过不合并且不清 worktree（返工从分支尖续作）；
   blocked/cannot_do 时：丢弃 worktree 变更，主仓库任务状态置为 blocked。
非 git 项目：Agent 直接在项目目录工作，只回写任务状态，不提交。
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .approvals import args_preview, get_manager, requires_approval
from .config import (LLMConfig, LocalDebugConfig, ProjectConfig, ReviewConfig, TaskStatus,
                     assert_review_model_distinct, resolve_commands,
                     resolve_permission_mode, resolve_review_llm,
                     resolve_review_rounds, review_enabled_for)
from .git_ops import Git, GitError
from .llm import AgentResponse, LLMClient, LLMError, finish_response
from .logging_setup import get_logger
from .mirrors import decorate_skill_body
from .prompts import build_system_prompt
from .skills import Skill, skill_bodies
from .task_control import get_task_controls
from .task_model import Task, append_progress
from .tools import (TOGGLEABLE_TOOLS, FinishSignal, TaskStopped, ToolContext,
                    _tool_path_dirs, persist_path_dirs, scan_tool_env)

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
    # 审查门结论（合并前审查开启时非 None）；skipped = 未启用审查
    review: object | None = None
    # 审查阶段落下的 WIP 提交短哈希（审计可溯；未审查/无提交时为空）
    wip_commit: str = ""


class TaskExecutor:
    def __init__(self, project: ProjectConfig, llm: LLMConfig,
                 tools: dict[str, bool] | None = None,
                 permission_mode: str = "full_access",
                 permission_timeout_minutes: int = 30,
                 allowed_commands: list[str] | None = None,
                 extra_env: dict[str, str] | None = None,
                 mirror_runtime=None,
                 path_autofix: bool = False,
                 review_cfg: ReviewConfig | None = None,
                 review_llm: LLMConfig | None = None,
                 local_debug: LocalDebugConfig | None = None,
                 commit_and_merge: bool = False) -> None:
        self.project = project
        self.llm = llm
        self.commit_and_merge = bool(commit_and_merge)
        # 生效的工具开关 map（resolve_tools 的产物，支持组名展开）；未列出的工具默认启用
        from .config import _expand_tools_map
        self.tools_map: dict[str, bool] = _expand_tools_map(tools)
        # 生效的访问权限模式（resolve_permission_mode 的产物）与审批超时
        self.permission_mode = permission_mode
        self.permission_timeout_minutes = permission_timeout_minutes
        # 生效的命令白名单（resolve_commands 的产物）；None = 工具层回退默认表
        self.allowed_commands = list(allowed_commands) if allowed_commands else None
        # 注入子进程的自定义环境变量（config env.vars，代理/国内镜像等）
        self.extra_env = dict(extra_env or {})
        # 任务启动时把扫描到的缺失工具目录持久写入用户 PATH（config
        # env.path_autofix，看板开关；默认关——系统级修改须用户显式开启）
        self.path_autofix = bool(path_autofix)
        # 审查门配置：cfg 为 None 时取默认（未配置模型 = 不审查，兼容旧调用）；
        # review_llm 为调用方按配置解析出的生效审查模型（None = 审查未启用）
        self.review_cfg = review_cfg or ReviewConfig()
        self.review_llm = review_llm
        # 本地调试配置（Chrome/Edge 与后台服务）
        self.local_debug = local_debug or LocalDebugConfig()
        # 镜像源运行时（mirrors.MirrorRuntime）：speedtest_mirrors 工具 + env-setup
        # 技能正文动态追加「测速优选」段用；None = 未接线（技能保持静态默认序）
        self.mirror_runtime = mirror_runtime
        self.git = Git(project.path)
        self.is_git_repo = project.path.is_dir() and self.git.is_repo()
        self.worktree = project.path / WORKTREE_DIR
        self.worktree_git = Git(self.worktree)
        self.log = get_logger(f"executor.{project.name}")

    def probe_git_repo(self) -> bool:
        """实时探测项目目录是否已是 git 仓库，并回写 is_git_repo 缓存。

        会话创建时缓存的 is_git_repo 会过期（用户事后手动 init git），这里
        先做廉价的 .git 存在性预判：没有 .git 直接判否；有 .git 才跑一次
        git rev-parse 复核（目录可能还没真正 init 完）。回写缓存让本会话
        内的 worktree / 提交逻辑立即跟上实际状态。
        """
        if not self.project.path.is_dir() or not (self.project.path / ".git").exists():
            self.is_git_repo = False
        else:
            self.is_git_repo = self.git.is_repo()
        return self.is_git_repo

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
            if not requires_approval(mode, name, args) or name in always_allowed:
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
        from .tools import DEFAULT_OFF_TOOLS, TOGGLEABLE_TOOLS

        enabled = set()
        for name in TOGGLEABLE_TOOLS:
            if name in DEFAULT_OFF_TOOLS:
                if self.tools_map.get(name, False):
                    enabled.add(name)
            else:
                if self.tools_map.get(name, True):
                    enabled.add(name)
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
        turns_desc = ("不限轮数" if self.project.max_agent_turns is not None
                      and self.project.max_agent_turns <= 0
                      else f"最多 {self.project.max_agent_turns} 轮")
        append_progress(task.path,
                        f"开始执行（{task.priority}，{turns_desc}）")

        result = TaskResult(task_id=task.id, outcome="blocked")
        usage: dict = {}
        # 强制停止注册：看板可随时置位，工具循环在轮次/工具边界响应
        controls = get_task_controls()
        stop_event = controls.begin(self.project.name, task.id)
        try:
            response = self._agent_loop(
                task, guide_content, skills or [], usage_out=usage,
                stop_event=stop_event,
                on_progress=lambda line: append_progress(task.path, line))
            result.turns = int(usage.get("turns", 0))
            result.prompt_tokens = int(usage.get("prompt_tokens", 0))
            result.completion_tokens = int(usage.get("completion_tokens", 0))
            result.summary = response.summary
            result.notes = response.notes
            result.files_changed = response.files_changed
            if response.action == "completed":
                if self.is_git_repo and self.commit_and_merge:
                    outcome, review = self._complete_with_review(task, response, result)
                    result.outcome = outcome
                    result.review = review
                else:
                    # 未开启提交合并或非 git 项目：无自动提交/合并，改动直接保留在工作区供人工检查
                    outcome, review = self._review_non_git(task, response, result)
                    result.outcome = outcome
                    result.review = review
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
        except TaskStopped:
            self.log.warning("任务 %s 已被用户在看板强制停止", task.id)
            result.outcome = "cancelled"
            result.notes = "已被用户在看板强制停止"
            result.turns = int(usage.get("turns", 0))
            result.prompt_tokens = int(usage.get("prompt_tokens", 0))
            result.completion_tokens = int(usage.get("completion_tokens", 0))
            append_progress(task.path, "⏹ 已被用户在看板强制停止（可按需重新入队）")
            self._cleanup_worktree_changes()
        except Exception as e:  # noqa: BLE001 — 单任务失败不能拖垮调度循环
            self.log.exception("任务 %s 执行出现未预期异常", task.id)
            result.llm_error = str(e)
            result.notes = f"执行异常：{e}"
            result.turns = int(usage.get("turns", 0))
            result.prompt_tokens = int(usage.get("prompt_tokens", 0))
            result.completion_tokens = int(usage.get("completion_tokens", 0))
            self._cleanup_worktree_changes()

        # 任务结束后移除 worktree：改动要么已合并要么已放弃，避免残留未跟踪目录。
        # 审查不通过/审查失败例外：WIP 提交保留在分支上供返工续作或人工裁决，
        # worktree 同步保留（D8/D9：审查受阻 ≠ 成果无效）
        review_kept = getattr(result.review, "verdict", "") in ("fail", "error")
        if self.is_git_repo and self.commit_and_merge and result.outcome in ("done", "cancelled") and not review_kept:
            self.remove_worktree()

        controls.end(self.project.name, task.id)
        result.duration_seconds = round(time.monotonic() - started, 1)
        self.log.info(
            "任务 %s 结束：%s（%.1fs）%s",
            task.id, result.outcome, result.duration_seconds,
            result.commit and f"commit={result.commit}",
        )
        # 用量台账：失败的任务也记（token 已真实消耗）；记录失败绝不影响任务
        try:
            from .usage import get_usage_store
            get_usage_store().record(
                self.project.name, task.id, result.outcome, result.turns,
                result.prompt_tokens, result.completion_tokens, self.llm.model)
        except Exception:  # noqa: BLE001 — 台账是旁路，任何异常都吞掉
            self.log.warning("用量记录失败（可忽略）", exc_info=True)
        # 任务结束提醒：完成/受阻时提示音 + 系统通知（同款旁路，失败不影响任务）
        try:
            from .notify import notify_task_end
            notify_task_end(self.project.name, task.id, task.title, result.outcome)
        except Exception:  # noqa: BLE001 — 提醒是旁路，任何异常都吞掉
            self.log.warning("任务提醒失败（可忽略）", exc_info=True)
        return result

    # ---- Agent 循环 ----

    def _agent_loop(self, task: Task, guide_content: str, skills: list[Skill],
                    on_progress=None, usage_out: dict | None = None,
                    stop_event=None) -> AgentResponse:
        use_worktree = self.is_git_repo and self.commit_and_merge
        work_root = self.worktree if use_worktree else self.project.path
        if use_worktree:
            branch = self._prepare_worktree()
            self.log.debug("worktree 就绪，工作分支：%s", branch)

        # 技能正文按任务新鲜预载：read_skill 工具直接查内存，不碰文件系统；
        # env-setup 正文动态追加「镜像源测速优选」段（有测速结果时）——模型
        # 无需先调 speedtest_mirrors 也能按最快源下载
        enabled = self._enabled_tools()
        bodies = skill_bodies(skills)
        if self.mirror_runtime is not None and "env-setup" in bodies:
            bodies["env-setup"] = decorate_skill_body(
                "env-setup", bodies["env-setup"], self.mirror_runtime)
        # 任务启动扫描：可选先把缺失工具目录持久写入用户 PATH（env.path_autofix，
        # 只增不删、失败不阻断），再生成工具地图进系统提示词——模型不用再自己
        # 一轮轮探测命令装在哪（npm 不在 PATH 的事故教训）
        tool_map = None
        try:
            if self.path_autofix:
                persist_path_dirs(_tool_path_dirs())
            tool_map = scan_tool_env()
            if tool_map["extra_dirs"]:
                self.log.info("任务启动工具扫描：注入会话 PATH %s；发现的命令：%s",
                              "; ".join(tool_map["extra_dirs"]),
                              ", ".join(sorted(tool_map["tools"])) or "（无）")
        except Exception:  # noqa: BLE001 — 扫描是旁路增强，失败不阻断任务
            self.log.warning("任务启动工具扫描失败（忽略）", exc_info=True)
        # 构建本地调试会话（DebugSession）
        session = None
        try:
            from .debug_session import DebugSession
            from .config import resolve_debug_hosts
            effective_hosts = resolve_debug_hosts(self.local_debug, self.project.debug_allowed_hosts)
            session = DebugSession(
                root=work_root,
                allowed_commands=self.allowed_commands,
                local_debug=self.local_debug,
                effective_hosts=effective_hosts,
                extra_env=self.extra_env,
                stop_event=stop_event,
            )
        except Exception:
            self.log.warning("初始化调试会话失败（可忽略）", exc_info=True)

        ctx = ToolContext(root=work_root,
                          allowed_commands=self.allowed_commands,
                          skills=bodies, enabled_tools=enabled,
                          approval_gate=self._make_approval_gate(task),
                          stop_event=stop_event, extra_env=self.extra_env,
                          mirror_runtime=self.mirror_runtime,
                          current_task=task,
                          session=session)
        client = LLMClient(self.llm)
        client.reset(build_system_prompt(self.project, guide_content, self.is_git_repo,
                                         skills=skills, tools=enabled,
                                         permission_mode=self.permission_mode,
                                         tool_map=tool_map))

        try:
            resp = client.run_task_loop(self._build_task_prompt(task, enabled), ctx,
                                        max_turns=self.project.max_agent_turns,
                                        on_progress=on_progress)
        except FinishSignal as sig:
            # run_task_loop 内部会捕获并转换；这里兜底处理（替身/异常路径）
            resp = finish_response(sig.payload)
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
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
        if self.is_git_repo and not self.git.has_commits():
            try:
                self.git.ensure_gitignore([
                    f"{self.project.tasks_dir.strip('/')}/",
                    self.project.guide_file,
                    f"{WORKTREE_DIR}/",
                ])
                self.git.add_all()
                self.git.commit("chore: 初始化项目仓库")
                self.log.info("空仓库已自动创建初始提交")
            except GitError as e:
                self.log.warning("空仓库创建初始提交失败：%s", e)

        if self._branch_exists(branch):
            self.git.run("worktree", "add", str(self.worktree), branch)
        else:
            self.git.run("worktree", "add", "-b", branch, str(self.worktree))
        return branch

    def _branch_exists(self, branch: str) -> bool:
        return bool(self.git.run("branch", "--list", branch).strip())

    def _cleanup_worktree_changes(self) -> None:
        """任务结束后清空 worktree 的未提交改动。"""
        if not self.is_git_repo or not self.commit_and_merge or not (self.worktree / ".git").exists():
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

    # ---- 提交、审查门与合并 ----

    def _review_active(self) -> tuple[bool, str]:
        """审查门是否生效：返回 (active, skip_reason)。

        生效条件 = 审查开关开启（项目级覆盖全局）+ 审查模型已配置；运行时
        守卫在此兜底拦截模型互斥冲突（防手改 yaml 绕过配置层校验）。
        """
        cfg = self.review_cfg
        if not review_enabled_for(cfg, self.project):
            return False, "审查未开启"
        if self.review_llm is None:
            return False, "未配置审查模型"
        try:
            assert_review_model_distinct(self.llm, self.review_llm,
                                         context=f"项目 {self.project.name} 的 review")
        except ValueError as e:
            # 运行时守卫：冲突时跳过并告警，绝不执行同模型审查（R4 第三层）
            self.log.warning("模型互斥守卫拦截：%s（本次跳过审查）", e)
            return False, "审查模型与任务模型冲突（守卫拦截）"
        return True, ""

    def _review_diff(self) -> str:
        """待合并改动 diff：相对主分支 merge-base 的累计改动（含返工轮 WIP 提交）。"""
        main_branch = self.project.allowed_branch or self.git.current_branch()
        work_branch = f"arranger/{self.project.name}"
        if not self.git.has_commits():
            try:
                # 零提交空仓库：直接读取 worktree 上的改动
                return self.worktree_git.run("show", "--stat", "--patch", "HEAD")
            except GitError:
                return ""
        try:
            self.git.run("merge-base", main_branch, work_branch)
        except GitError:
            try:
                return self.worktree_git.run("diff", f"{main_branch}...HEAD")
            except GitError:
                return ""
        return self.git.run("diff", f"{main_branch}...{work_branch}")

    def _run_review(self, task: Task, review_pending_path: Path | None) -> "object":
        """构建审查上下文并执行审查 agent（见 review.review_task）。"""
        from .review import review_task  # 局部导入避免 executor→review 循环依赖

        skills = self._load_skills_for_review()
        gate = self._make_approval_gate(task)
        try:
            return review_task(
                self.project, task, self.worktree,
                "", "", self._review_diff(), self.review_llm,
                guide_content=self._load_guide(), skill_bodies=skills,
                allowed_commands=self.allowed_commands,
                approval_gate=gate,
                max_turns=max(1, self.review_cfg.max_turns),
                allow_test_commands=self.review_cfg.allow_test_commands,
                extra_env=self.extra_env)
        finally:
            pass

    def _load_skills_for_review(self) -> dict[str, str]:
        """审查 agent 的技能正文预载（与任务 agent 同库；read_skill 工具直接查内存）。"""
        try:
            from .agent import ProjectAgent  # 仅复用 _load_skills 逻辑
            bodies = {}
            if self.review_llm is not None:
                # 审查与任务共用技能库；ProjectAgent._load_skills 是实例方法，
                # 这里直接走底层函数，避免为一个加载动作构造整个 ProjectAgent
                from .skills import load_skills, skills_for_project
                base = getattr(self, "skills_base", None)
                if base:
                    bodies = skill_bodies(skills_for_project(load_skills(base),
                                                            self.project.name))
            return bodies
        except Exception:  # noqa: BLE001 — 技能是旁路增强，失败不阻断审查
            self.log.warning("审查技能加载失败（忽略）", exc_info=True)
            return {}

    def _record_review_usage(self, task: Task, review) -> None:
        """审查用量入台账（kind=review）；旁路，任何异常吞掉。"""
        try:
            from .usage import get_usage_store
            get_usage_store().record(
                self.project.name, task.id, review.verdict, review.turns,
                review.prompt_tokens, review.completion_tokens,
                review.model, kind="review")
        except Exception:  # noqa: BLE001 — 台账是旁路
            self.log.warning("审查用量记录失败（可忽略）", exc_info=True)

    def _complete_with_review(self, task: Task, response: AgentResponse,
                              result: "TaskResult") -> tuple[str, object]:
        """git 项目 action=completed 的完整路径：WIP 提交 → 审查门 → 合并。

        返回 (outcome, ReviewResult)。合并前审查开启时：
        - pass → 合并（返工任务且 squash_merge 开启时 --squash 收敛）→ done；
        - fail/error → 不合并、不清 worktree（成果留分支供返工/人工），outcome
          由 agent 层按 review_round 分派返工或受阻，此处一律 blocked；
        - skipped（未启用）→ 走原合并路径。
        """
        branch = f"arranger/{self.project.name}"
        message = response.commit_message or f"fix: {task.title}"
        # 第一步提交（原 _commit_and_merge 的 commit 步骤前移）：提交是崩溃
        # 安全点——审查前落 WIP 提交，任何时点被杀成果都不丢（返工轮续作）
        committed = self.worktree_git.commit(message)
        if not committed:
            self.log.warning("模型报告 completed 但 worktree 无任何改动（任务 %s）", task.id)
            result.notes = (result.notes + "\n" if result.notes else "") + \
                "注意：Agent 报告完成但未产生文件改动，任务按 blocked 处理"
            self._cleanup_worktree_changes()
            return "blocked", None
        result.wip_commit = self.worktree_git.run("rev-parse", "--short", "HEAD")

        active, skip_reason = self._review_active()
        if not active:
            self.log.info("审查门跳过（%s），直接合并", skip_reason)
            merged = self._merge_and_push(task, result, branch)
            review = self._skipped_review(skip_reason)
            return ("done" if merged else "blocked"), review

        append_progress(task.path, "🔍 独立审查开始（合并前）")
        from .task_model import set_task_file_review_pending
        set_task_file_review_pending(task.path, True)
        review = self._run_review(task, task.path)
        result.review = review
        set_task_file_review_pending(task.path, False)
        self._record_review_usage(task, review)

        if review.verdict == "pass":
            merged = self._merge_and_push(task, result, branch)
            if not merged:
                return "blocked", review
            return "done", review

        # fail / error：不合并且不清 worktree（成果与 WIP 提交保留）
        if review.verdict == "fail":
            self.log.info("任务 %s 审查不通过：%s", task.id, review.summary)
        else:
            self.log.warning("任务 %s 审查失败（保持待审可补审）：%s", task.id, review.error)
        return "blocked", review

    def _skipped_review(self, reason: str):
        """构造 skipped 审查结论（未启用审查时随结果落档）。"""
        from .review import ReviewResult, VERDICT_SKIPPED
        return ReviewResult(verdict=VERDICT_SKIPPED, summary=f"审查未执行（{reason}）",
                            model=self.review_llm.model if self.review_llm else "")

    def _review_non_git(self, task: Task, response: AgentResponse,
                        result: "TaskResult") -> tuple[str, object]:
        """非 git 项目 action=completed：无提交/合并，审查直接读项目目录。

        fail 同样走返工（agent 层分派）；Agent 的改动已在盘上，返工从现状续作。
        """
        active, skip_reason = self._review_active()
        if not active:
            review = self._skipped_review(skip_reason)
            return "done", review
        append_progress(task.path, "🔍 独立审查开始（合并前）")
        from .task_model import set_task_file_review_pending
        set_task_file_review_pending(task.path, True)
        review = self._run_review_non_git(task)
        result.review = review
        set_task_file_review_pending(task.path, False)
        self._record_review_usage(task, review)
        if review.verdict == "pass":
            return "done", review
        return "blocked", review

    def _run_review_non_git(self, task: Task):
        """工作区模式（未开启提交合并或非 git 项目）的审查执行：根为项目目录。"""
        from .review import review_task
        skills = self._load_skills_for_review()
        diff = ""
        if self.is_git_repo:
            try:
                diff = self.git.run("diff")
            except GitError:
                diff = ""
        return review_task(
            self.project, task, self.project.path,
            "", "", diff, self.review_llm,
            guide_content=self._load_guide(), skill_bodies=skills,
            allowed_commands=self.allowed_commands,
            approval_gate=self._make_approval_gate(task),
            max_turns=max(1, self.review_cfg.max_turns),
            allow_test_commands=self.review_cfg.allow_test_commands,
            extra_env=self.extra_env)

    def _merge_and_push(self, task: Task, result: "TaskResult",
                        branch: str) -> bool:
        """审查通过后合并工作分支回主分支（返回是否成功）。

        返工任务（review_round>0）且 squash_merge 开启时用 --squash 收敛分支
        上全部 WIP 提交为单提交；未返工任务维持 --no-ff（历史行为不变）。
        """
        reworked = (getattr(result.review, "verdict", "") == "pass"
                    and self._review_round_of(task) > 0
                    and self.review_cfg.squash_merge)
        main_branch = self.project.allowed_branch or self.git.current_branch()
        try:
            if not self.git.has_commits():
                # 零提交空仓库：主分支尚无 commit，直接以工作分支成果作为主分支起始提交
                self.git.run("checkout", "-B", main_branch, branch)
            else:
                self.git.checkout(main_branch)
                if reworked:
                    # --squash 把分支尖内容暂存到索引但不产生 merge commit：
                    # 用 Agent 的提交信息收敛为单提交（正文附任务 ID 与审查轮数可溯）
                    message = (getattr(result.review, "summary", "") or f"fix: {task.title}")
                    self.git.run("merge", "--squash", branch)
                    commit_msg = (f"{message.splitlines()[0][:200]}\n\n"
                                  f"Task: {task.id}（审查返工后合并，squash 自 {branch}）")
                    self.git.commit(commit_msg)
                else:
                    self.git.run("merge", "--no-ff", "-m",
                                 f"Merge arranger work for {task.id}", result.wip_commit or branch)
        except GitError:
            self.log.exception("合并任务 %s 的提交失败，保留分支 %s 供人工处理",
                               task.id, branch)
            result.notes = (result.notes + "\n" if result.notes else "") + \
                f"合并主分支失败，提交保留在 {branch} 分支，需人工介入"
            return False

        result.commit = self.git.run("rev-parse", "--short", "HEAD")
        result.pushed = False
        if self.git.has_remote("origin"):
            try:
                self.git.push("origin", main_branch)
                result.pushed = True
            except GitError as e:
                self.log.warning("推送失败（不影响本地结果）：%s", e)

        self._cleanup_worktree_changes()
        return True

    def _review_round_of(self, task: Task) -> int:
        """任务文件的当前返工轮数（解析回读；审查通过前 agent 层尚未累加）。"""
        try:
            from .task_model import parse_task_file
            return parse_task_file(task.path).review_round
        except Exception:  # noqa: BLE001 — 回读失败按未返工处理
            return 0

    def _load_guide(self) -> str:
        """项目指引内容（审查 agent 的验收参照；任务 agent 的一份由 agent 层传）。"""
        guide_path = self.project.path / self.project.guide_file
        try:
            return guide_path.read_text(encoding="utf-8")
        except OSError:
            return ""

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
            if not self.git.has_commits():
                self.git.run("checkout", "-B", main_branch, result.commit)
            else:
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
