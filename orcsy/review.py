"""审查 Agent：任务合并前的独立结果审查（合并前审查门，见 docs/design-review-agent.md）。

与任务 agent 的分工：
- 任务 agent（executor.py）：改代码、自验证，结论落在 finish 工具；
- 审查 agent（本模块）：用**另一个模型**只读核查任务成果（对照需求与验收
  标准抽查实现真实性，可选跑测试实测验证），结论同样落在 finish 工具——
  action=completed 映射 pass、blocked 映射 fail。

执行方式复用 LLMClient.run_task_loop + 只读工具集（可选 run_command 跑测试），
不引入第二套循环；停止信号与任务共用同一注册表，看板「强制停止」审查期间
同样生效。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import LLMConfig, ProjectConfig
from .llm import FinishSignal, LLMClient, LLMError, finish_response
from .logging_setup import get_logger
from .prompts import build_review_prompt, build_review_system_prompt
from .task_control import get_task_controls
from .task_model import Task
from .tools import TaskStopped, ToolContext

# 审查结论（ReviewResult.verdict）
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_ERROR = "error"
VERDICT_SKIPPED = "skipped"


@dataclass
class ReviewIssue:
    """结构化审查意见（finish.issues 的规范化形态）。"""

    file: str
    severity: str   # blocker / major / minor
    message: str
    line: int | None = None


@dataclass
class ReviewResult:
    """审查结论：verdict + 审计信息。error 表示审查本身失败（非任务不合格）。"""

    verdict: str          # pass / fail / error / skipped
    summary: str = ""     # 一句话审查结论
    issues: list[ReviewIssue] = field(default_factory=list)
    issues_raw: str = ""  # 结构化清单为空时的纯文本兜底（finish notes 原文）
    model: str = ""       # 实际审查模型（审计）
    turns: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""       # error 时的失败原因


def review_task(project: ProjectConfig, task: Task, work_root: Path,
                agent_summary: str, agent_notes: str, diff_text: str,
                review_llm: LLMConfig, *, guide_content: str = "",
                skill_bodies: dict[str, str] | None = None,
                allowed_commands: list[str] | None = None,
                approval_gate=None, max_turns: int = 10,
                allow_test_commands: bool = True,
                extra_env: dict[str, str] | None = None) -> ReviewResult:
    """执行一次审查，返回结论。本函数不抛异常——所有失败都体现在结果里。

    work_root：审查工具的根目录（git 项目 = worktree，非 git 项目 = 项目根），
    由调用方传入——审查读到的都是待合并形态。approval_gate 由执行器按访问
    权限模式构建后传入（审查的 run_command 与任务 agent 同一审批语义）。
    """
    log = get_logger(f"review.{project.name}")
    started = time.monotonic()
    result = ReviewResult(verdict=VERDICT_ERROR, model=review_llm.model)
    controls = get_task_controls()
    stop_event = controls.begin(project.name, task.id)
    try:
        system_prompt = build_review_system_prompt(
            project, work_root, guide_content,
            allow_test_commands=allow_test_commands,
            skill_bodies=skill_bodies or {})
        task_prompt = build_review_prompt(
            task, agent_summary, agent_notes, diff_text,
            allow_test_commands=allow_test_commands)

        # 审查工具集：只读探索 + finish；allow_test_commands 时追加 run_command
        # （跑测试实测验证；受项目命令白名单与审批门约束，与任务 agent 同闸门）
        enabled = {"list_dir", "find_files", "read_file", "grep", "read_skill", "finish"}
        if allow_test_commands:
            enabled.add("run_command")

        ctx = ToolContext(root=work_root,
                          allowed_commands=allowed_commands,
                          skills=skill_bodies or {},
                          enabled_tools=frozenset(enabled),
                          approval_gate=approval_gate,
                          stop_event=stop_event,
                          extra_env=extra_env,
                          current_task=task)
        client = LLMClient(review_llm)
        client.reset(system_prompt)
        try:
            resp = client.run_task_loop(task_prompt, ctx, max_turns=max_turns)
        except FinishSignal as sig:
            # run_task_loop 内部会捕获并转换；这里兜底处理（异常路径）
            resp = finish_response(sig.payload)

        result.turns = int(client.usage.get("turns", 0))
        result.prompt_tokens = int(client.usage.get("prompt_tokens", 0))
        result.completion_tokens = int(client.usage.get("completion_tokens", 0))
        result.summary = resp.summary
        result.issues = [ReviewIssue(file=i["file"], severity=i["severity"],
                                     message=i["message"], line=i.get("line"))
                         for i in resp.issues]
        # 结构化清单为空但模型给了文字说明：留作兜底展示
        if not result.issues and resp.notes:
            result.issues_raw = resp.notes

        if resp.action == "completed":
            result.verdict = VERDICT_PASS
        elif resp.action == "blocked":
            result.verdict = VERDICT_FAIL
        else:
            # cannot_do/split 不是合法审查结论：按 error 处理（不合并、可重试）
            result.verdict = VERDICT_ERROR
            result.error = f"审查结论不合法：action={resp.action}"
    except LLMError as e:
        result.verdict = VERDICT_ERROR
        result.error = f"审查调用大模型失败：{e}"
    except TaskStopped:
        result.verdict = VERDICT_ERROR
        result.error = "审查已被用户在看板强制停止"
    except Exception as e:  # noqa: BLE001 — 审查失败不能拖垮调度循环
        log.exception("任务 %s 审查出现未预期异常", task.id)
        result.verdict = VERDICT_ERROR
        result.error = f"审查执行异常：{e}"
    finally:
        controls.end(project.name, task.id)
    log.info("任务 %s 审查结论：%s（%.1fs，%d 轮）%s",
             task.id, result.verdict, time.monotonic() - started, result.turns,
             result.summary[:80] if result.summary else result.error[:80])
    return result
