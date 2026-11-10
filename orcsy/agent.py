"""项目 Agent：负责一个项目的完整任务循环。

每轮 scan()：
1. 读取项目的 AGENT.md 指引（缺失时使用默认指引并告警）；
2. 扫描 tasks/ 目录，解析所有任务，按优先级排序；
3. 依次领取前 N 个 open 任务（尊重 assignee），交给 TaskExecutor 执行；
4. 依据执行结果回写任务文件状态（status / result 节）；审查门开启时任务
   完成先经独立审查 agent 合并前审查，通过才归档到 done/。
"""

from __future__ import annotations

import datetime as _dt
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import (AgentProfileConfig, LocalDebugConfig, ProjectConfig,
                     ReviewConfig, ToolsConfig, resolve_project_team_members,
                     resolve_review_llm, resolve_review_rounds, review_enabled_for)
from .executor import TaskExecutor, TaskResult
from .logging_setup import get_logger
from .prompts import build_role_prompt_extra
from .skills import Skill, load_skills, skills_for_project
from .task_model import (Task, _parse_frontmatter, append_progress,
                         append_task_dependencies, collect_task_statuses,
                         evaluate_task_dependencies, increment_task_file_qa_round,
                         increment_task_file_review_round, parse_task_file,
                         scan_tasks, set_task_file_review_pending,
                         set_task_file_status)
from .templates import default_guide

# 回写到任务文件的结果节
_RESULT_HEADING = "## Agent 处理结果"

# 审查结论节（通过/不通过共用；问题清单逐条列在其下）
_REVIEW_HEADING = "## 审查结果"

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _now_str() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def parse_task_fresh(path: Path) -> Task:
    """实时解析单个任务文件（补审扫描用）：扫描间隔内文件可能已被改写。"""
    return parse_task_file(path)


def recover_interrupted_tasks(project: ProjectConfig) -> list[str]:
    """进程启动时恢复上次意外退出遗留的 in_progress 任务。

    强杀/断电等场景下任务文件停在 in_progress，而扫描只认 open——不处理
    就永远卡死。这里把遗留任务重置为 open 重新入队，并在「执行进度」节
    写入中断说明（最后活动时间取自文件 mtime，即最后一次进度落盘时刻）。
    返回被重置的任务 ID 列表。只在进程启动时调用一次（配置热重载不触发，
    避免误伤正在执行中的任务）。

    review_pending=true 的任务跳过：那是「已完成、WIP 提交已落、审查门未出
    结论」的残留——交给补审扫描只重跑审查，不整任务重跑（否则执行 Agent 的
    token 白烧一遍）。
    """
    tasks_dir = project.path / project.tasks_dir
    if not tasks_dir.is_dir():
        return []
    reset: list[str] = []
    for path in sorted(tasks_dir.glob("*.md")):
        try:
            meta, _ = _parse_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if str(meta.get("status") or "").strip().lower() != "in_progress":
            continue
        if bool(meta.get("review_pending", False)):
            continue  # 待审查残留：补审扫描接续（scan_and_work 内处理）
        try:
            last = _dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except OSError:
            last = "未知时间"
        append_progress(
            path,
            f"**进程意外退出，任务被中断**（最后活动 {last}）。"
            "已重置为待办重新执行；git 项目 worktree 里未提交的改动会在下次执行时重置丢弃。")
        if set_task_file_status(path, "open"):
            reset.append(path.stem)
    return reset


class ProjectAgent:
    """一个项目一个实例；scan() 由调度器周期性调用。"""

    def __init__(self, project: ProjectConfig, llm,
                 skills_base=None,
                 tools: dict[str, bool] | None = None,
                 permission_mode: str = "full_access",
                 permission_timeout_minutes: int = 30,
                 allowed_commands: list[str] | None = None,
                 extra_env: dict[str, str] | None = None,
                 mirror_runtime=None,
                 path_autofix: bool = False,
                 review_cfg: ReviewConfig | None = None,
                 local_debug: LocalDebugConfig | None = None,
                 commit_and_merge: bool = False,
                 *,
                 agent_profiles: dict[str, AgentProfileConfig] | None = None,
                 global_tools: ToolsConfig | None = None) -> None:
        self.project = project
        self.skills_base = skills_base  # 技能库根目录（配置目录/skills）；None 表示无技能
        self.review_cfg = review_cfg or ReviewConfig()
        self.local_debug = local_debug or LocalDebugConfig()
        self.commit_and_merge = bool(commit_and_merge)
        self.review_llm = resolve_review_llm(self.review_cfg, project.review_llm,
                                             context=f"项目 {project.name} 的 review_llm")
        if self.review_llm is not None:
            # 运行时守卫的第一道：构造时即比对（互斥校验与执行器一致）
            from .config import assert_review_model_distinct
            try:
                assert_review_model_distinct(llm, self.review_llm,
                                             context=f"项目 {project.name} 的 review")
            except ValueError as e:
                get_logger(f"agent.{project.name}").warning(
                    "审查模型互斥守卫：%s（审查将跳过）", e)
                self.review_llm = None

        self.executor = TaskExecutor(project, llm, tools=tools,
                                     permission_mode=permission_mode,
                                     permission_timeout_minutes=permission_timeout_minutes,
                                     allowed_commands=allowed_commands,
                                     extra_env=extra_env,
                                     mirror_runtime=mirror_runtime,
                                     path_autofix=path_autofix,
                                     review_cfg=self.review_cfg,
                                     review_llm=self.review_llm,
                                     local_debug=self.local_debug,
                                     commit_and_merge=commit_and_merge,
                                     skills_base=skills_base)
        self.log = get_logger(f"agent.{project.name}")

        # 项目小组（多模型协同模式）初始化
        self.team_cfg = project.team
        self.team_enabled = bool(self.team_cfg and self.team_cfg.enabled)
        self.merge_lock = threading.Lock()
        self._active_lock = threading.Lock()
        self.active_roles: dict[str, str] = {}  # task_id -> member_id
        self.members: dict[str, dict] = {}
        self.team_roles_meta: dict[str, dict] = {}
        self.member_executors: dict[str, TaskExecutor] = {}

        if self.team_enabled and self.team_cfg is not None:
            gt = global_tools if global_tools is not None else ToolsConfig(enabled=dict(tools or {}))
            self.members = resolve_project_team_members(project, agent_profiles or {}, llm, gt)
            self.team_roles_meta = {
                mid: {
                    "name": m["name"],
                    "role": m["role"],
                    "icon": m["icon"],
                    "description": m["description"],
                    "produces_code": m["produces_code"],
                }
                for mid, m in self.members.items()
            }
            from .config import assert_review_model_distinct
            for mid, m in self.members.items():
                mem_review_llm = resolve_review_llm(
                    self.review_cfg, project.review_llm,
                    context=f"项目 {project.name} 成员 {mid} 的 review_llm",
                )
                if mem_review_llm is not None:
                    try:
                        assert_review_model_distinct(
                            m["llm"], mem_review_llm,
                            context=f"项目 {project.name} 成员 {mid} 的 review",
                        )
                    except ValueError as e:
                        self.log.warning("成员 %s 审查模型互斥守卫：%s（该成员跳过审查）", mid, e)
                        mem_review_llm = None

                extra_sys = build_role_prompt_extra(
                    m["role"],
                    m["name"],
                    self.team_roles_meta,
                    allow_test_scripts=m["allow_test_scripts"],
                    extra_prompt=m["extra_prompt"],
                )
                self.member_executors[mid] = TaskExecutor(
                    project,
                    m["llm"],
                    tools=m["tools"],
                    permission_mode=permission_mode,
                    permission_timeout_minutes=permission_timeout_minutes,
                    allowed_commands=allowed_commands,
                    extra_env=extra_env,
                    mirror_runtime=mirror_runtime,
                    path_autofix=path_autofix,
                    review_cfg=self.review_cfg,
                    review_llm=mem_review_llm,
                    local_debug=self.local_debug,
                    commit_and_merge=commit_and_merge,
                    worktree_isolation=True,
                    per_task_worktree=True,
                    produces_code=m["produces_code"],
                    team_role=m["role"],
                    agent_name=m["name"],
                    team_roles=self.team_roles_meta,
                    max_split_depth=self.team_cfg.max_split_depth,
                    max_agent_turns=m["max_turns"],
                    extra_system_prompt=extra_sys,
                    merge_lock=self.merge_lock,
                    skills_base=skills_base,
                )

    # ---- 对外 ----

    @property
    def tasks_dir(self) -> Path:
        return self.project.path / self.project.tasks_dir

    def _select_member_for_task(self, task: Task) -> tuple[str, TaskExecutor] | None:
        """为任务匹配执行者（单兵模式返回 ('', self.executor)；小组模式按角色路由）。"""
        if not self.team_enabled:
            if task.assignee and task.assignee != self.project.name:
                self.log.debug("任务 %s 指派给 %s，跳过", task.id, task.assignee)
                return None
            return "", self.executor

        assignee = (task.assignee or "").strip()
        if assignee and assignee != self.project.name:
            ex = self.member_executors.get(assignee)
            if ex is None:
                self.log.debug("任务 %s 指派的角色 %s 未启用或不在小组内，跳过", task.id, assignee)
                return None
            return assignee, ex

        # 未指派（或指派为项目名）的原始需求：优先派给 PM 拆解
        pm_id = next((mid for mid, m in self.members.items() if m["role"] == "pm"), None)
        if pm_id and task.split_depth == 0:
            return pm_id, self.member_executors[pm_id]

        # 无 PM 或已是子任务：按 fallback_role → 标签匹配 → 单一非 PM 成员兜底
        fb = (self.team_cfg.fallback_role or "").strip() if self.team_cfg else ""
        if fb and fb in self.member_executors:
            return fb, self.member_executors[fb]
        for lbl in task.labels:
            if lbl in self.member_executors:
                return lbl, self.member_executors[lbl]
        non_pm = [mid for mid, m in self.members.items() if m["role"] != "pm"]
        if len(non_pm) == 1:
            mid = non_pm[0]
            return mid, self.member_executors[mid]
        self.log.debug("任务 %s 缺少 assignee 且无 PM/兜底角色可认领，等待指派", task.id)
        return None

    def _run_member_task(self, task: Task, member_id: str, executor: TaskExecutor,
                         guide: str, skills: list[Skill]) -> tuple[Task, str, TaskResult]:
        if member_id:
            with self._active_lock:
                self.active_roles[task.id] = member_id
        try:
            res = executor.run_task(task, guide, skills)
            return task, member_id, res
        finally:
            if member_id:
                with self._active_lock:
                    self.active_roles.pop(task.id, None)

    def scan_and_work(self) -> list[TaskResult]:
        """执行一轮扫描与任务处理（支持依赖就绪门控、小组波次并行与周期内链式推进）。"""
        log = self.log
        if not self.project.path.is_dir():
            log.error("项目目录不存在：%s，跳过", self.project.path)
            return []

        guide = self._load_guide()
        skills = self._load_skills()
        results: list[TaskResult] = []
        total_claimed = 0

        if self.team_enabled and self.team_cfg is not None:
            step_limit = (
                max(1, max(self.project.max_tasks_per_cycle, self.team_cfg.max_chain_steps_per_cycle))
                if self.team_cfg.chain_within_cycle
                else max(1, self.project.max_tasks_per_cycle)
            )
        else:
            step_limit = max(1, self.project.max_tasks_per_cycle)

        errors_written = False
        while total_claimed < step_limit:
            tasks, errors = scan_tasks(self.tasks_dir)
            if errors and not errors_written:
                for t in errors:
                    log.warning("任务文件解析失败 %s：%s", t.id, t.error)
                self._write_back_errors(errors)
                errors_written = True

            if not tasks:
                if total_claimed == 0:
                    log.info("没有可执行的 open 任务")
                break

            all_statuses, done_ids = collect_task_statuses(self.tasks_dir)
            ready_candidates: list[tuple[Task, str, TaskExecutor]] = []
            for task in tasks:
                dep_info = evaluate_task_dependencies(task, all_statuses, done_ids)
                if dep_info["dep_state"] != "ready":
                    log.debug("任务 %s 依赖未就绪（%s），跳过", task.id, dep_info["dep_state"])
                    continue
                picked = self._select_member_for_task(task)
                if picked is None:
                    continue
                mid, ex = picked
                ready_candidates.append((task, mid, ex))

            if not ready_candidates:
                if total_claimed == 0:
                    log.info("发现 %d 个 open 任务，但当前无依赖就绪且匹配角色的待领任务", len(tasks))
                break

            remaining = step_limit - total_claimed
            can_parallel = (
                self.team_enabled
                and self.team_cfg is not None
                and self.team_cfg.execution_mode == "parallel_worktree"
                and self.executor.probe_git_repo()
            )
            wave: list[tuple[Task, str, TaskExecutor]] = []
            if not can_parallel:
                wave = [ready_candidates[0]]
            else:
                first_t, first_mid, first_ex = ready_candidates[0]
                if not first_ex.produces_code:
                    # 只读/联调角色在主工作区运行，独占当前波次以保证工作区代码视图一致
                    wave = [(first_t, first_mid, first_ex)]
                else:
                    for item in ready_candidates:
                        if len(wave) >= remaining:
                            break
                        if item[2].produces_code:
                            wave.append(item)

            if total_claimed == 0:
                log.info("发现 %d 个 open 任务，本波次领取 %d 个：%s",
                         len(tasks), len(wave),
                         ", ".join(f"{t.id}->{mid or 'default'}" for t, mid, _ in wave))

            for t, _, _ in wave:
                self._mark_in_progress(t)

            wave_outputs: list[tuple[Task, str, TaskResult]] = []
            if len(wave) == 1:
                t, mid, ex = wave[0]
                wave_outputs.append(self._run_member_task(t, mid, ex, guide, skills))
            else:
                with ThreadPoolExecutor(max_workers=len(wave)) as pool:
                    futs = [
                        pool.submit(self._run_member_task, t, mid, ex, guide, skills)
                        for t, mid, ex in wave
                    ]
                    for fut in as_completed(futs):
                        wave_outputs.append(fut.result())

            for t, _mid, res in wave_outputs:
                self._write_back_result(t, res)
                results.append(res)
                total_claimed += 1

            # 单兵模式若未启用小组链式流转：正常按 max_tasks_per_cycle 循环即可；
            # 小组模式关闭 chain_within_cycle 时只推进 1 波次
            if self.team_enabled and self.team_cfg is not None and not self.team_cfg.chain_within_cycle:
                break

        # 补审扫描：审查 error 待重试 / 审查中被强杀的 review_pending 残留，
        # 只重跑审查不重跑任务（审查默认关闭时 pending 标记不会产生，天然空转）
        results.extend(self._rescan_pending_reviews())
        return results

    # ---- 审查补扫 ----

    def _rescan_pending_reviews(self) -> list[TaskResult]:
        """重审 tasks/ 内 review_pending=true 的任务（不重跑执行 Agent）。

        覆盖：审查调用失败待重试、进程在「WIP 提交后、审查结论前」被杀的
        残留。审查未启用时按 skipped 出结论并归档（理论上不该有 pending，
        兜底清队）。本轮任务额度不含补审——审查是收尾动作，不挤占新任务。
        """
        if not self.tasks_dir.is_dir():
            return []
        pending: list[Task] = []
        for path in sorted(self.tasks_dir.glob("*.md")):
            try:
                t = parse_task_fresh(path)
            except OSError:
                continue
            if t.error or not t.review_pending:
                continue
            pending.append(t)
        if not pending:
            return []
        results: list[TaskResult] = []
        for task in pending:
            self.log.info("补审任务 %s（review_pending，重跑审查不重跑任务）", task.id)
            result = self._review_pending_task(task)
            if result is not None:
                self._write_back_result(task, result)
                results.append(result)
        return results

    def _review_pending_task(self, task: Task) -> TaskResult | None:
        """对 pending 任务只执行审查门并落结果；返回合成 TaskResult 或 None。"""
        ex = self.executor
        if self.team_enabled and task.assignee and task.assignee in self.member_executors:
            ex = self.member_executors[task.assignee]

        if ex.review_llm is None:
            # 审查已停用（如配置热重载清掉了模型）：放行归档，兜底清队
            self.log.info("审查未启用，任务 %s 放行归档（兜底清 pending）", task.id)
            set_task_file_review_pending(task.path, False)
            self._archive_task(task, review_note="审查未执行（审查已停用，兜底放行）")
            return None

        wt_path = ex._task_worktree(task)
        branch = ex._task_branch(task)
        base_branch = ex._base_branch()
        result = TaskResult(task_id=task.id, outcome="blocked",
                            role=ex.team_role, agent_name=ex.agent_name)
        review = ex._run_review(task, task.path, wt_path=wt_path,
                                base_branch=base_branch, work_branch=branch)
        result.review = review
        set_task_file_review_pending(task.path, False)
        self._record_review_usage(task, review)
        if review.verdict == "pass":
            result.outcome = "done"
            lock = ex.merge_lock or threading.Lock()
            with lock:
                if ex.commit_and_merge:
                    merged = ex._merge_and_push(task, result, branch, wt_path=wt_path)
                else:
                    merged = ex._merge_to_stage_and_land_main(task, result, branch, wt_path=wt_path)
            if not merged:
                result.outcome = "blocked"
                result.notes = (result.notes or "") + "审查通过但合并失败，需人工介入"
            else:
                ex.remove_worktree(wt_path=wt_path, branch=branch if ex.per_task_worktree else None)
        # fail/error：outcome=blocked，交 _write_back_result 按 verdict 分派
        return result

    # ---- 内部 ----

    def _load_guide(self) -> str:
        guide_path = self.project.path / self.project.guide_file
        try:
            content = guide_path.read_text(encoding="utf-8")
            if content.strip():
                return content
            self.log.warning("指引文件为空：%s，使用内置模板生成", guide_path)
        except OSError:
            self.log.warning("未找到指引文件 %s，使用内置模板生成", guide_path)
        # 与看板新建项目落盘的初始内容同源（templates.default_guide），不留第二份文案
        return default_guide(self.project.name, self.project.path)

    def _load_skills(self) -> list[Skill]:
        """加载对本项目生效的技能（每轮新鲜读取，看板的增删改即时生效）。"""
        if self.skills_base is None:
            return []
        try:
            skills = skills_for_project(load_skills(self.skills_base), self.project.name)
        except OSError as e:
            self.log.warning("技能库读取失败：%s", e)
            return []
        if skills:
            self.log.debug("适用技能：%s", ", ".join(s.name for s in skills))
        return skills

    def _set_status(self, path: Path, status: str, result_section: str | None = None,
                    extra_section: tuple[str, str] | None = None) -> None:
        """改写任务文件 frontmatter 的 status，可选追加结果节/自定义节。

        extra_section = (标题, 内容)：插在结果节之前（如「## 审查结果」需
        排在「## Agent 处理结果」上方，让审查结论先于执行自述被看到）。
        """
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            self.log.error("回写任务状态失败（无法读取 %s）：%s", path, e)
            return

        m = _FRONTMATTER_RE.match(text)
        if not m:
            # 没有 frontmatter（不太可能，解析阶段已过滤），直接加结果节
            text = f"---\nstatus: {status}\n---\n\n{text}"
        else:
            front, rest = m.group(1), text[m.end():]
            front_lines = [
                re.sub(r"^status:\s*\S+.*$", f"status: {status}", line, flags=re.MULTILINE)
                for line in [front]
            ][0]
            if f"status:" not in front_lines:
                front_lines = front_lines.rstrip() + f"\nstatus: {status}"
            text = f"---\n{front_lines}\n---\n{rest}"

        if result_section or extra_section:
            body = text
            marker = ""
            if result_section:
                # 新结果节固定插正文最前：旧的先删掉（只在本调用提供新结果节时删，
                # 仅加 extra_section 时保留已有结果节不动）
                body = re.sub(
                    rf"{re.escape(_RESULT_HEADING)}.*?(?=\n## |\Z)", "", body,
                    flags=re.DOTALL)
                marker += f"\n{_RESULT_HEADING}\n\n{result_section.strip()}\n"
            if extra_section:
                heading, content = extra_section
                # 旧同名节先删掉再插新的；排在结果节之前（审查结论先于执行自述被看到）
                body = re.sub(rf"{re.escape(heading)}.*?(?=\n## |\Z)", "", body,
                              flags=re.DOTALL)
                marker = f"\n{heading}\n\n{content.strip()}\n" + marker
            # 插到 frontmatter 之后
            m2 = _FRONTMATTER_RE.match(body)
            if m2:
                body = body[:m2.end()] + marker.lstrip("\n") + "\n" + body[m2.end():]
            else:
                body = marker + body
            text = body

        try:
            path.write_text(text, encoding="utf-8")
        except OSError as e:
            self.log.error("回写任务状态失败（无法写入 %s）：%s", path, e)

        # 受阻戳：标 blocked 保留（等人工重入队）；离开 blocked/done/cancelled
        # 的写入由 set_task_file_status 统一处理
        if status == "blocked":
            from .task_model import _set_frontmatter_field
            _set_frontmatter_field(path, None, "requeue_seq", None)

    def _mark_in_progress(self, task: Task) -> None:
        # 标 in_progress 也清除受阻戳：任务已重新开工，不再享受插队优先
        from .task_model import _set_frontmatter_field
        self._set_status(task.path, "in_progress")
        _set_frontmatter_field(task.path, None, "requeue_seq", None)

    def _write_back_result(self, task: Task, result: TaskResult) -> None:
        # 大模型基础设施错误（HTTP 429/500、网络超时等）：只标状态 + 进度留痕，
        # 不写结果节。结果节固定插在正文最前，把错误文本写进去会把任务原内容
        # 顶到下面——看板描述与文件开头看起来就像被错误信息覆盖了。
        # 模型自己给出的 blocked 结论（summary 非空）仍按正常结果节回写。
        if result.llm_error and not result.summary:
            detail = (result.llm_error if len(result.llm_error) <= 300
                      else result.llm_error[:300] + "…")
            append_progress(
                task.path,
                f"⚠ 调用大模型失败：{detail}"
                f"（{result.turns} 轮，输入 {result.prompt_tokens} / "
                f"输出 {result.completion_tokens} tokens）；任务已标为受阻，可重新入队")
            self._set_status(task.path, result.outcome)
            self.log.warning("任务 %s 因大模型错误标为 %s（任务正文未改动）",
                             task.id, result.outcome)
            return

        review = getattr(result, "review", None)
        verdict = getattr(review, "verdict", "") if review is not None else ""

        # 审查门 fail/error：不归档。fail → 返工（未达上限）或受阻（达上限）；
        # error → 保持 pending 由下轮补审重试（连续失败超限转受阻）
        if verdict == "fail":
            self._handle_review_fail(task, result, review)
            return
        if verdict == "error":
            self._handle_review_error(task, result, review)
            return

        # PM 调用 reassign_task 直接转交下游角色：保持 open 且不覆盖 assignee，立即交接
        if getattr(result, "reassigned_to", ""):
            set_task_file_status(task.path, "open")
            self.log.info("任务 %s 已由 PM 直接分派给 %s（保持 open 待接单）",
                          task.id, result.reassigned_to)
            return

        # QA 联调发现 Bug 触发等待回归：绑定 Bug 为前置依赖并回到 open（或超限熔断）
        if getattr(result, "wait_retest", False):
            max_qa = self.team_cfg.max_qa_rounds if self.team_cfg else 3
            new_qa_round = increment_task_file_qa_round(task.path)
            bugs = list(getattr(result, "created_bugs", None) or [])
            if bugs:
                append_task_dependencies(task.path, bugs)
            if bugs and new_qa_round < max_qa:
                lines = [
                    f"- 时间：{_now_str()}",
                    f"- 负责角色：{result.agent_name or result.role or 'qa'}",
                    f"- 轮次：第 {new_qa_round}/{max_qa} 轮联调（待前置 Bug 修复后自动回归）",
                    f"- 派发 Bug：{', '.join(bugs)}",
                ]
                if result.summary:
                    lines.append(f"- 摘要：{result.summary}")
                self._set_status(task.path, "open", result_section="\n".join(lines))
                set_task_file_status(task.path, "open")
                append_progress(
                    task.path,
                    f"🔁 QA 第 {new_qa_round}/{max_qa} 轮联调发现 {len(bugs)} 个 Bug（{', '.join(bugs)}），"
                    "已绑定为前置依赖并等待修复后自动回归",
                )
                self.log.info("任务 %s 等待 Bug 修复后回归（%d/%d）：%s",
                              task.id, new_qa_round, max_qa, bugs)
                return

            reason = (
                f"QA 联调回归已达上限（{new_qa_round}/{max_qa} 轮），转人工裁决"
                if bugs else "QA 请求等待回归但未创建任何 Bug 任务，转人工核查"
            )
            lines = [
                f"- 时间：{_now_str()}",
                f"- 结果：blocked",
                f"- 原因：{reason}",
            ]
            if bugs:
                lines.append(f"- 关联 Bug：{', '.join(bugs)}")
            self._set_status(task.path, "blocked", result_section="\n".join(lines))
            append_progress(task.path, f"⛔ {reason}")
            try:
                from .notify import notify_task_end
                notify_task_end(self.project.name, task.id, task.title, "blocked")
            except Exception:  # noqa: BLE001
                pass
            self.log.warning("任务 %s 转 blocked：%s", task.id, reason)
            return

        lines = [f"- 时间：{_now_str()}",
                 f"- 结果：{result.outcome}"]
        if result.role or result.agent_name:
            who = (f"{result.agent_name}（{result.role}）"
                   if result.agent_name and result.role
                   else (result.agent_name or result.role))
            lines.append(f"- 负责角色：{who}")
        if result.commit:
            lines.append(f"- 提交：{result.commit}{'（已推送）' if result.pushed else ''}")
        if result.turns:
            lines.append(f"- 轮数：{result.turns}（输入 {result.prompt_tokens} / "
                         f"输出 {result.completion_tokens} tokens）")
        if result.summary:
            lines.append(f"- 摘要：{result.summary}")
        if result.files_changed:
            lines.append(f"- 变更文件：{', '.join(result.files_changed)}")
        if result.notes:
            lines.append(f"- 备注：{result.notes.strip()}")
        if result.llm_error:
            lines.append(f"- 异常：{result.llm_error}")

        self._set_status(task.path, result.outcome, result_section="\n".join(lines))

        # 审查门结论节（pass/skipped 随归档落档；fail/error 已在各自分支处理）
        if verdict in ("pass", "skipped"):
            self._write_review_section(task, review)

        # done 任务归档到 tasks/done/，保持清单整洁
        if result.outcome == "done":
            self._archive_task(task)

    def _handle_review_fail(self, task: Task, result: TaskResult, review) -> None:
        """审查不通过：问题清单落档 → 返工（未达上限）或受阻（达上限）。

        返工轮静默（不触发通知）：防骚扰由 notify 层按 outcome 判定，这里
        只负责任务文件的状态机流转。
        """
        self._record_review_usage(task, review)
        max_rounds = resolve_review_rounds(self.review_cfg, self.project)
        new_round = increment_task_file_review_round(task.path)
        self._write_review_section(task, review)
        if max_rounds == -1 or new_round < max_rounds:
            # 返工：回 open 重新入队（requeue_seq 自动插队），任务正文已含
            # 审查意见节，_build_task_prompt 会把它带给下一轮执行 Agent
            round_str = f"第 {new_round} 轮（不设上限）" if max_rounds == -1 else f"第 {new_round}/{max_rounds} 轮"
            append_progress(task.path,
                            f"⚠ 独立审查不通过（{round_str}）：{review.summary}")
            set_task_file_status(task.path, "open")
            self.log.info("任务 %s 审查不通过（%s），转返工：%s",
                          task.id, round_str, review.summary[:80])
            return
        append_progress(task.path,
                        f"⛔ 连续 {new_round} 次审查不通过，转受阻等人工裁决；"
                        f"成果保留在 arranger/{self.project.name} 分支")
        self.log.warning("任务 %s 连续 %d 次审查不通过，转 blocked", task.id, new_round)

    def _handle_review_error(self, task: Task, result: TaskResult, review) -> None:
        """审查本身失败（LLM 错误/协议异常）：不合并不归档，保持 pending 待补审。

        连续 error 达生效返工上限 → blocked（防审查服务故障把任务挂死）。
        error 轮数与 fail 轮数共用 review_round 计数（都是「审查未放行」次数）。
        """
        self._record_review_usage(task, review)
        max_rounds = resolve_review_rounds(self.review_cfg, self.project)
        new_round = increment_task_file_review_round(task.path)
        self._write_review_section(task, review)
        if max_rounds == -1 or new_round < max_rounds:
            set_task_file_review_pending(task.path, True)
            round_str = f"第 {new_round} 次（不设上限）" if max_rounds == -1 else f"第 {new_round}/{max_rounds} 次"
            append_progress(task.path,
                            f"⚠ 审查失败（{round_str}）：{review.error}；"
                            "任务保持待审，下轮自动补审")
            self.log.warning("任务 %s 审查失败（%s），保持 pending：%s",
                             task.id, round_str, review.error[:80])
            return
        set_task_file_review_pending(task.path, False)
        set_task_file_status(task.path, "blocked")
        append_progress(task.path,
                        f"⛔ 审查连续失败 {new_round} 次，转受阻等人工；"
                        f"成果保留在 arranger/{self.project.name} 分支")
        self.log.warning("任务 %s 审查连续失败 %d 次，转 blocked", task.id, new_round)

    def _record_review_usage(self, task: Task, review) -> None:
        """审查用量入台账（kind=review，与执行 Agent 的消耗单列可核算）。

        旁路：任何异常吞掉，审查结论的落档不受影响。
        """
        try:
            from .usage import get_usage_store
            get_usage_store().record(
                self.project.name, task.id, review.verdict, review.turns,
                review.prompt_tokens, review.completion_tokens,
                review.model, kind="review")
        except Exception:  # noqa: BLE001 — 台账是旁路
            self.log.warning("审查用量记录失败（可忽略）", exc_info=True)

    def _write_review_section(self, task: Task, review) -> None:
        """把审查结论写进任务文件的「## 审查结果」节（固定插在正文最前）。"""
        if review is None:
            return
        verdict = review.verdict
        label = {"pass": "通过", "fail": "不通过", "skipped": "未执行"}.get(verdict, verdict)
        lines = [f"### 审查结论：{label}", ""]
        if review.summary:
            lines += [review.summary, ""]
        if review.issues:
            lines.append("**问题清单：**")
            for i in review.issues:
                loc = f"{i.file}" + (f":{i.line}" if i.line else "")
                lines.append(f"- [{i.severity}] {loc} — {i.message}")
            lines.append("")
        elif review.issues_raw:
            lines += [review.issues_raw, ""]
        if review.model:
            lines.append(f"- 审查模型：{review.model}")
        if review.turns:
            lines.append(f"- 审查轮数：{review.turns}（输入 {review.prompt_tokens} / "
                         f"输出 {review.completion_tokens} tokens）")
        if verdict == "error" and review.error:
            lines.append(f"- 失败原因：{review.error}")
        if verdict == "skipped" and review.summary:
            lines.append(f"- 原因：{review.summary}")
        self._set_status(task.path, self._current_status(task.path),
                         result_section=None, extra_section=(_REVIEW_HEADING, "\n".join(lines)))

    @staticmethod
    def _current_status(path: Path) -> str:
        try:
            meta, _ = _parse_frontmatter(path.read_text(encoding="utf-8"))
            return str(meta.get("status") or "open").strip().lower()
        except OSError:
            return "open"

    def _archive_task(self, task: Task, review_note: str = "") -> None:
        """归档任务文件到 tasks/done/（review_note 可选附加说明）。"""
        if review_note:
            append_progress(task.path, review_note)
        done_dir = self.tasks_dir / "done"
        done_dir.mkdir(parents=True, exist_ok=True)
        target = done_dir / task.path.name
        try:
            task.path.replace(target)
            self.log.info("任务 %s 已归档到 %s", task.id, target)
        except OSError as e:
            self.log.warning("归档任务 %s 失败：%s", task.id, e)

    def _write_back_errors(self, errors: list[Task]) -> None:
        """解析失败的任务：把失败原因写回文件头部的 HTML 注释，等人工修复。"""
        for task in errors:
            try:
                text = task.path.read_text(encoding="utf-8")
            except OSError:
                continue
            comment = f"<!-- orcsy: 任务文件解析失败：{task.error} -->\n"
            if "orcsy: 任务文件解析失败" not in text and "task-arranger: 任务文件解析失败" not in text:
                try:
                    task.path.write_text(comment + text, encoding="utf-8")
                except OSError as e:
                    self.log.error("回写解析失败信息到 %s 失败：%s", task.path, e)
