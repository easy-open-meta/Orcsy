"""任务内动态只读子代理（Task Subagent Fan-Out）并发探索与委托引擎。

主 Agent（如 PM 或复杂开发角色）在执行大任务时，可调用 delegate_explore 一次性派发
2~4 个轻量只读子代理，在独立线程池中并发探索不同模块、目录或排查点，并在数秒内汇总出
高度提炼的结构化 Markdown 调研报告返回给主 Agent。

核心特性与安全熔断：
1. F1 递归锁死：子代理仅放行纯只读探索工具，工具集内严格剔除 delegate_explore 与写工具（深度锁死为 1）；
2. F2 扇出上限：单次最多并发派发 4 个子代理，单任务内最多允许委托 2 次；
3. F3 轮数熔断：每个子代理 max_turns 强制锁在 6 轮以内，杜绝子代理长跑死循环；
4. F4 共享强停：挂靠父任务 stop_event，用户在看板点「强制停止」时秒级打断所有在途子代理；
5. F5 用量合并：子代理消耗的 Tokens 实时汇总并累加进父任务用量台账。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from .config import LLMConfig
from .llm import FinishSignal, LLMClient, TaskStopped, finish_response
from .logging_setup import get_logger
from .tools import ToolContext

# 子代理严格只读工具集（坚决不包含任何写入、命令执行、任务拆分或二次派生工具）
SUBAGENT_ALLOWED_TOOLS = frozenset({
    "list_dir", "find_files", "read_file", "grep", "read_skill", "finish",
})

DEFAULT_SUBAGENT_MAX_TURNS = 6
MAX_SUBAGENTS_PER_CALL = 4


@dataclass
class SubagentTask:
    name: str
    focus: str
    paths: list[str] = field(default_factory=list)


@dataclass
class SubagentResult:
    name: str
    summary: str
    notes: str
    turns: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_seconds: float = 0.0
    success: bool = True
    error: str = ""


_SUBAGENT_SYSTEM_PROMPT = """\
# 角色：只读代码探索调研员 (Code Exploration Subagent)

你是受主 Agent 委派的专职轻量只读代码调研员，任务是针对指定的具体关注点进行快速、精准的代码摸底。

## 行为与协作准则
1. **只读高效**：你的可用工具严格受限为只读检索工具（`list_dir`, `find_files`, `read_file`, `grep`）；
2. **快速收敛**：请在 2~5 轮工具调用内摸清目标，紧扣给定的关注点提取关键模型、接口路由、关键函数与文件位置；
3. **结构化汇报**：一旦掌握足够信息，**必须立即调用 `finish` 工具输出结论**：
   - `action`："completed"
   - `summary`：一句话核心调研结论
   - `notes`：分点列出关键发现、核心数据结构/接口定义与涉及的文件路径（建议 300 字以内）
4. **不要发散**：不要探索与当前关注点无关的其余模块，不要尝试修改任何文件。
"""


def _run_single_subagent(
    parent_ctx: ToolContext,
    task: SubagentTask,
    llm_cfg: LLMConfig,
    max_turns: int = DEFAULT_SUBAGENT_MAX_TURNS,
) -> SubagentResult:
    """在独立线程中执行单个子代理的只读探索对话循环。"""
    log = get_logger(f"subagent.{task.name}")
    start_time = time.monotonic()
    result = SubagentResult(name=task.name, summary="", notes="")

    # 为子代理构造独立的只读 ToolContext（严防权限越界与递归调用）
    sub_ctx = ToolContext(
        root=parent_ctx.root,
        allowed_commands=[],
        skills=parent_ctx.skills,
        enabled_tools=SUBAGENT_ALLOWED_TOOLS,
        approval_gate=None,
        stop_event=parent_ctx.stop_event,
        extra_env=parent_ctx.extra_env,
        current_task=parent_ctx.current_task,
        mirror_runtime=parent_ctx.mirror_runtime,
        session=parent_ctx.session,
        is_subagent=True,
        # 子代理是纯只读调研员：不产出代码，连续只读熔断不适用
        produces_code=False,
    )

    paths_hint = (
        f"建议重点探索的路径：{', '.join(task.paths)}\n"
        if task.paths else ""
    )
    user_prompt = (
        f"## 调研任务：{task.name}\n\n"
        f"### 核心关注点与问题\n{task.focus}\n\n"
        f"{paths_hint}"
        "请立即调用只读工具展开调研，并在 2~5 轮内调用 finish 输出结构化结论与关键代码依据。"
    )

    client = LLMClient(llm_cfg)
    client.reset(_SUBAGENT_SYSTEM_PROMPT)

    try:
        try:
            resp = client.run_task_loop(
                user_prompt,
                sub_ctx,
                max_turns=min(max_turns, DEFAULT_SUBAGENT_MAX_TURNS),
            )
        except FinishSignal as sig:
            resp = finish_response(sig.payload)
        result.summary = resp.summary or "调研完成"
        result.notes = resp.notes or resp.summary
        result.success = True
    except TaskStopped:
        result.success = False
        result.error = "任务被用户强制停止"
        result.summary = "已中断"
        result.notes = "执行被强制停止"
    except Exception as e:
        log.warning("子代理 [%s] 执行异常：%s", task.name, e)
        result.success = False
        result.error = str(e)
        result.summary = "调研遇到异常"
        result.notes = f"异常信息：{e}"
    finally:
        result.turns = int(client.usage.get("turns", 0))
        result.prompt_tokens = int(client.usage.get("prompt_tokens", 0))
        result.completion_tokens = int(client.usage.get("completion_tokens", 0))
        result.duration_seconds = time.monotonic() - start_time

    return result


def run_subagents_fan_out(
    parent_ctx: ToolContext,
    subagents_data: list[dict[str, Any]],
    llm_cfg: LLMConfig,
    max_turns: int = DEFAULT_SUBAGENT_MAX_TURNS,
) -> tuple[str, dict[str, int]]:
    """并发执行多个子代理，并汇总为格式化的 Markdown 报告与累加的 Token 用量。

    返回：(markdown_report, usage_dict)
    """
    tasks: list[SubagentTask] = []
    for item in subagents_data[:MAX_SUBAGENTS_PER_CALL]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        focus = str(item.get("focus") or "").strip()
        if not name or not focus:
            continue
        raw_paths = item.get("paths")
        paths = [str(p).strip() for p in raw_paths if str(p).strip()] if isinstance(raw_paths, list) else []
        tasks.append(SubagentTask(name=name, focus=focus, paths=paths))

    if not tasks:
        return "未指定有效的子代理任务（每项必须包含 name 和 focus）。", {
            "turns": 0, "prompt_tokens": 0, "completion_tokens": 0,
        }

    results: list[SubagentResult] = []
    total_usage = {"turns": 0, "prompt_tokens": 0, "completion_tokens": 0}

    # 线程池并发驱动所有子代理
    workers = min(len(tasks), MAX_SUBAGENTS_PER_CALL)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_run_single_subagent, parent_ctx, t, llm_cfg, max_turns): t
            for t in tasks
        }
        for fut in as_completed(futures):
            res = fut.result()
            results.append(res)
            total_usage["turns"] += res.turns
            total_usage["prompt_tokens"] += res.prompt_tokens
            total_usage["completion_tokens"] += res.completion_tokens

    # 按原始任务顺序排序展示报告
    name_order = {t.name: i for i, t in enumerate(tasks)}
    results.sort(key=lambda r: name_order.get(r.name, 99))

    report_lines: list[str] = [
        f"### 🔍 动态子代理并发探索报告（共 {len(results)} 个子任务完成）：\n"
    ]

    for i, res in enumerate(results, 1):
        status_icon = "✅" if res.success else "⚠️"
        report_lines.append(
            f"#### {i}. {status_icon} {res.name}（耗时 {res.duration_seconds:.1f}s，{res.turns} 轮）"
        )
        report_lines.append(f"- **结论**：{res.summary}")
        if res.notes and res.notes.strip() != res.summary.strip():
            report_lines.append(f"- **发现依据**：\n{res.notes.strip()}\n")
        else:
            report_lines.append("")

    return "\n".join(report_lines), total_usage
