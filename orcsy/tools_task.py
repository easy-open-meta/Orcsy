"""任务生命周期、拆单编排协议、缺陷提单与子代理并发探索工具集。

包含：
- create_tasks：将任务拆解为子任务并进行 DAG 依赖闭环/环检测；
- report_bug：QA 角色缺陷快速提单并挂靠回归前置依赖；
- reassign_task：PM 角色直接交接指派；
- finish：任务结束协议信号；
- delegate_explore：派出只读子代理并行摸底。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from .logging_setup import get_logger
from .task_model import (
    _set_frontmatter_field,
    append_progress,
    detect_dependency_cycle,
    parse_task_file,
    priority_rank,
    set_task_file_assignee,
    set_task_file_status,
)

if TYPE_CHECKING:
    from .tools import ToolContext

log = get_logger("tools.task")


class FinishSignal(Exception):
    """工具 finish 抛出的任务结束信号，payload 即结论 JSON 参数。"""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        super().__init__("finish")


def tool_finish(ctx: ToolContext, args: dict) -> str:
    """Agent 主动结束任务并给出结构化结论；由执行器捕获处理。"""
    raise FinishSignal(args)


def tool_create_tasks(ctx: ToolContext, args: dict) -> str:
    """把当前任务拆分为若干子任务（写入本项目任务队列），并可选关闭当前任务。

    支持：
    - assignee：指派负责角色（项目小组模式下校验是否在已启用可指派角色内）；
    - depends_on / depends_on_refs：声明外部前置任务 ID 或批内下标依赖，落盘前校验环依赖；
    - parent_id / split_depth / created_by：防递归无限拆分与链路追踪。
    """
    task = ctx.current_task
    if task is None or getattr(task, "path", None) is None:
        return "错误：无法确定当前任务（上下文未注入任务信息），不能拆分"
    items = args.get("tasks")
    if not isinstance(items, list) or not (1 <= len(items) <= 10):
        return "错误：tasks 必须是 1~10 个 {title, description?, priority?} 的数组"

    # 项目小组模式下的防无限递归拆分校验
    parent_depth = int(getattr(task, "split_depth", 0) or 0)
    if ctx.team_roles is not None and ctx.current_role:
        cur_info = ctx.team_roles.get(ctx.current_role, {})
        if cur_info.get("role") == "pm" and parent_depth >= max(1, int(ctx.max_split_depth or 1)):
            return (
                f"错误：当前任务拆分深度已达上限（split_depth={parent_depth}），"
                "禁止继续拆分，请使用 reassign_task 直接指派给执行角色。"
            )

    tasks_dir = Path(task.path).parent
    done_dir = tasks_dir / "done"
    existing_stems = {p.stem for p in tasks_dir.glob("*.md")} if tasks_dir.is_dir() else set()
    done_stems = {p.stem for p in done_dir.glob("*.md")} if done_dir.is_dir() else set()
    known_task_ids = existing_stems | done_stems
    used_stems = set(known_task_ids)

    orders = []
    if tasks_dir.is_dir():
        for p in tasks_dir.glob("*.md"):
            try:
                o = parse_task_file(p).order
            except Exception:  # noqa: BLE001
                continue
            if o is not None:
                orders.append(o)
    next_order = (max(orders) + 1) if orders else 1
    default_priority = str(getattr(task, "priority", "") or "P2").upper()

    assignable_roles: set[str] | None = None
    if ctx.team_roles is not None:
        non_pm = {k for k, v in ctx.team_roles.items() if v.get("role") != "pm"}
        assignable_roles = non_pm if non_pm else set(ctx.team_roles.keys())

    # 第一遍：字段校验与文件名预分配
    prepared: list[dict] = []
    for idx, it in enumerate(items):
        if not isinstance(it, dict):
            return "错误：tasks 的每项必须是 {title, description?, priority?} 对象"
        title = str(it.get("title") or "").strip()
        if not title:
            return "错误：子任务 title 不能为空"
        if len(title) > 80:
            return f"错误：子任务 title 过长（≤80 字符）：{title[:30]}…"
        priority = str(it.get("priority") or default_priority).strip().upper()
        if priority_rank(priority) > 4:
            return f"错误：子任务优先级不合法：{priority}（P0~P3）"
        description = str(it.get("description") or "").strip()

        assignee = str(it.get("assignee") or "").strip()
        if assignable_roles is not None:
            if not assignee:
                return (
                    f"错误：第 {idx + 1} 个子任务「{title}」必须指定 assignee 负责角色。"
                    f"本项目当前可用角色：{', '.join(sorted(assignable_roles))}"
                )
            if assignee not in assignable_roles:
                return (
                    f"错误：第 {idx + 1} 个子任务指派的角色 {assignee!r} 不可用或未启用。"
                    f"本项目当前可用角色：{', '.join(sorted(assignable_roles))}"
                )

        labels_raw = it.get("labels")
        labels: list[str] = []
        if isinstance(labels_raw, list):
            labels = [str(x).strip() for x in labels_raw if str(x).strip()]

        ext_deps_raw = it.get("depends_on")
        ext_deps: list[str] = []
        if isinstance(ext_deps_raw, list):
            ext_deps = [str(x).strip() for x in ext_deps_raw if str(x).strip()]

        refs_raw = it.get("depends_on_refs")
        refs: list[int] = []
        if refs_raw is not None:
            if not isinstance(refs_raw, list):
                return f"错误：第 {idx + 1} 个子任务的 depends_on_refs 必须是整数下标数组"
            for r in refs_raw:
                if not isinstance(r, int) or isinstance(r, bool) or not (0 <= r < len(items)):
                    return f"错误：第 {idx + 1} 个子任务的 depends_on_refs 下标 {r!r} 越界（有效 0~{len(items) - 1}）"
                if r == idx:
                    return f"错误：第 {idx + 1} 个子任务不能依赖自身（depends_on_refs={r}）"
                refs.append(r)

        slug = re.sub(r"\s+", "-", title)
        slug = re.sub(r"[^\w\-]", "", slug).strip("-")[:40].strip("-") or "task"
        base = f"{priority}-{slug}"
        stem, n = base, 2
        while stem in used_stems:
            stem = f"{base}-{n}"
            n += 1
        used_stems.add(stem)

        prepared.append({
            "stem": stem,
            "title": title,
            "priority": priority,
            "description": description,
            "assignee": assignee,
            "labels": labels,
            "ext_deps": ext_deps,
            "refs": refs,
        })

    # 第二遍：翻译批内下标依赖 depends_on_refs + 校验存在性 + 环检测
    planned_stems = {p["stem"] for p in prepared}
    all_valid_ids = known_task_ids | planned_stems
    extra_edges: dict[str, list[str]] = {}
    for p in prepared:
        merged_deps: list[str] = []
        seen_d: set[str] = set()
        for r in p["refs"]:
            target = prepared[r]["stem"]
            if target not in seen_d:
                seen_d.add(target)
                merged_deps.append(target)
        for d in p["ext_deps"]:
            if d == p["stem"]:
                return f"错误：子任务 {p['stem']} 不能依赖自身"
            if d not in all_valid_ids:
                return f"错误：子任务「{p['title']}」引用的前置任务 {d!r} 不存在"
            if d not in seen_d:
                seen_d.add(d)
                merged_deps.append(d)
        p["depends_on"] = merged_deps
        if merged_deps:
            extra_edges[p["stem"]] = merged_deps

    if extra_edges:
        cyc = detect_dependency_cycle(tasks_dir, extra_edges=extra_edges)
        if cyc:
            return f"错误：子任务依赖存在循环（{' -> '.join(cyc)}），请调整 depends_on / depends_on_refs"

    # 第三遍：统一落盘
    created: list[str] = []
    for p in prepared:
        t_val = p["title"]
        t_yaml = (json.dumps(t_val, ensure_ascii=False)
                  if any(ch in t_val for ch in (":", "#", "[", "]", "{", "}", '"', "'"))
                  else t_val)
        fm = [
            "---",
            f"title: {t_yaml}",
            f"priority: {p['priority']}",
            "status: open",
            f"order: {next_order}",
        ]
        if p["assignee"]:
            fm.append(f"assignee: {p['assignee']}")
        if p["labels"]:
            fm.append("labels: [" + ", ".join(p["labels"]) + "]")
        if p["depends_on"]:
            fm.append("depends_on: [" + ", ".join(p["depends_on"]) + "]")
        if ctx.team_roles is not None or parent_depth > 0:
            fm.append(f"parent_id: {task.id}")
            fm.append(f"split_depth: {parent_depth + 1}")
            if ctx.current_role:
                fm.append(f"created_by: {ctx.current_role}")
        fm.append("---")
        next_order += 1
        body = p["description"] or p["title"]
        (tasks_dir / f"{p['stem']}.md").write_text(
            "\n".join(fm) + "\n\n" + body + "\n", encoding="utf-8")
        created.append(p["stem"])

    lines = [f"已创建 {len(created)} 个子任务（status=open，按优先级与手动序领取）："
             + "、".join(created)]
    if bool(args.get("close_current", True)):
        set_task_file_status(Path(task.path), "done")
        append_progress(Path(task.path),
                        f"✂ 已成功拆分为 {len(created)} 个子任务：{'、'.join(created)}"
                        "（原任务拆分完成并归档，由子任务接力推进）")
        lines.append(f"已创建 {len(created)} 个子任务并指派。原任务已圆满完成拆分，"
                     "请立即调用 finish(action=\"split\", summary=一句话拆分总结) 进行归档交接。")
    else:
        lines.append("当前任务保持原状态。")
    log.info("create_tasks：%s（当前任务 %s）", created, task.id)
    return "\n".join(lines)


def tool_report_bug(ctx: ToolContext, args: dict) -> str:
    """测试工程师（QA）在联调中发现问题时，向项目任务队列新建 Bug 任务。"""
    task = ctx.current_task
    if task is None or getattr(task, "path", None) is None:
        return "错误：无法确定当前任务，不能创建 Bug 任务"

    items = args.get("bugs")
    if not isinstance(items, list) or not (1 <= len(items) <= 10):
        return "错误：bugs 必须是 1~10 个 Bug 描述对象的数组"

    tasks_dir = Path(task.path).parent
    done_dir = tasks_dir / "done"
    existing_stems = {p.stem for p in tasks_dir.glob("*.md")} if tasks_dir.is_dir() else set()
    done_stems = {p.stem for p in done_dir.glob("*.md")} if done_dir.is_dir() else set()
    used_stems = existing_stems | done_stems

    orders = []
    if tasks_dir.is_dir():
        for p in tasks_dir.glob("*.md"):
            try:
                o = parse_task_file(p).order
            except Exception:  # noqa: BLE001
                continue
            if o is not None:
                orders.append(o)
    next_order = (max(orders) + 1) if orders else 1

    fix_roles: set[str] | None = None
    if ctx.team_roles is not None:
        devs = {k for k, v in ctx.team_roles.items() if v.get("role") not in ("pm", "qa")}
        fix_roles = devs if devs else {k for k in ctx.team_roles if k != ctx.current_role}

    prepared: list[dict] = []
    for idx, it in enumerate(items):
        if not isinstance(it, dict):
            return "错误：bugs 每项必须是对象"
        title = str(it.get("title") or "").strip()
        if not title:
            return f"错误：第 {idx + 1} 个 Bug 缺少 title"
        if len(title) > 80:
            return f"错误：第 {idx + 1} 个 Bug 标题过长（≤80 字符）"
        repro = str(it.get("repro_steps") or "").strip()
        expected = str(it.get("expected") or "").strip()
        actual = str(it.get("actual") or "").strip()
        if not repro or not expected or not actual:
            return f"错误：第 {idx + 1} 个 Bug「{title}」必须填写 repro_steps、expected 与 actual"

        severity = str(it.get("severity") or "major").strip().lower()
        if severity not in ("blocker", "major", "minor"):
            return f"错误：第 {idx + 1} 个 Bug 的 severity 不合法：{severity}（应为 blocker/major/minor）"

        priority = str(it.get("priority") or "P1").strip().upper()
        if priority_rank(priority) > 4:
            return f"错误：第 {idx + 1} 个 Bug 优先级不合法：{priority}（P0~P3）"

        assignee = str(it.get("assignee") or "").strip()
        if fix_roles is not None:
            if not assignee:
                return (
                    f"错误：第 {idx + 1} 个 Bug「{title}」必须指派修复角色 assignee。"
                    f"可用开发角色：{', '.join(sorted(fix_roles))}"
                )
            if assignee not in fix_roles:
                return (
                    f"错误：第 {idx + 1} 个 Bug 指派的角色 {assignee!r} 无效或不可接 Bug。"
                    f"可用开发角色：{', '.join(sorted(fix_roles))}"
                )
        elif not assignee:
            assignee = "backend"

        evidence = str(it.get("evidence") or "").strip()
        slug = re.sub(r"\s+", "-", title)
        slug = re.sub(r"[^\w\-]", "", slug).strip("-")[:36].strip("-") or "issue"
        base = f"{priority}-bug-{slug}"
        stem, n = base, 2
        while stem in used_stems:
            stem = f"{base}-{n}"
            n += 1
        used_stems.add(stem)

        prepared.append({
            "stem": stem,
            "title": title,
            "priority": priority,
            "severity": severity,
            "assignee": assignee,
            "repro": repro,
            "expected": expected,
            "actual": actual,
            "evidence": evidence,
        })

    created: list[str] = []
    parent_depth = int(getattr(task, "split_depth", 0) or 0)
    for p in prepared:
        fm = [
            "---",
            f"title: {json.dumps('[Bug] ' + p['title'], ensure_ascii=False)}",
            f"priority: {p['priority']}",
            "status: open",
            f"order: {next_order}",
            f"assignee: {p['assignee']}",
            f"labels: [bug, qa-found, {p['severity']}]",
            f"parent_id: {task.id}",
            f"split_depth: {max(1, parent_depth)}",
            f"created_by: {ctx.current_role or 'qa'}",
            "---",
        ]
        next_order += 1
        body_parts = [
            f"## 缺陷说明\n\n- 来源联调任务：{task.id}\n- 严重程度：{p['severity']}\n- 责任角色：{p['assignee']}",
            f"## 复现步骤\n\n{p['repro']}",
            f"## 期望结果\n\n{p['expected']}",
            f"## 实际现象\n\n{p['actual']}",
        ]
        if p["evidence"]:
            body_parts.append(f"## 调试证据\n\n{p['evidence']}")
        (tasks_dir / f"{p['stem']}.md").write_text(
            "\n".join(fm) + "\n\n" + "\n\n".join(body_parts) + "\n",
            encoding="utf-8",
        )
        created.append(p["stem"])
        ctx.created_bugs.append(p["stem"])

    append_progress(
        Path(task.path),
        f"🐞 联调发现 {len(created)} 个 Bug 已派发：{'、'.join(created)}",
    )
    log.info("report_bug：%s（来源任务 %s）", created, task.id)
    summary_items = ", ".join(f"{p['stem']} -> {p['assignee']}" for p in prepared)
    return (
        f"已创建 {len(created)} 个 Bug 任务：{summary_items}。\n"
        "请结束本轮调试（如先停止临时开发服务），然后调用 "
        "finish(action=\"wait_retest\", summary=\"发现 Bug 已派发修复，等待回归验证\")，"
        "系统会自动将以上 Bug 挂靠为本测试任务的前置依赖，待修复完成后自动唤醒回归测试。"
    )


def tool_reassign_task(ctx: ToolContext, args: dict) -> str:
    """项目经理（PM）判定无需拆分时，直接将当前任务指派给对应开发/测试角色。"""
    task = ctx.current_task
    if task is None or getattr(task, "path", None) is None:
        return "错误：无法确定当前任务，不能直接指派"

    assignee = str(args.get("assignee") or "").strip()
    if not assignee:
        return "错误：assignee 不能为空"

    if ctx.team_roles is not None:
        non_pm = {k for k, v in ctx.team_roles.items() if v.get("role") != "pm"}
        valid = non_pm if non_pm else set(ctx.team_roles.keys())
        if assignee not in valid:
            return (
                f"错误：目标角色 {assignee!r} 不在本项目可用角色列表内。"
                f"可用角色：{', '.join(sorted(valid))}"
            )

    priority = str(args.get("priority") or "").strip().upper()
    if priority:
        if priority_rank(priority) > 4:
            return f"错误：优先级不合法：{priority}（P0~P3）"
        _set_frontmatter_field(Path(task.path), None, "priority", priority)

    note = str(args.get("note") or "").strip()
    if not set_task_file_assignee(Path(task.path), assignee):
        return "错误：写入任务指派字段失败"

    ctx.reassigned_to = assignee
    msg = f"👑 项目经理直接指派给 [{assignee}]" + (f"：{note}" if note else "")
    append_progress(Path(task.path), msg)
    log.info("reassign_task：%s -> %s", task.id, assignee)
    return (
        f"已将当前任务直接指派给 {assignee}（无需拆分子任务）。"
        f"请立即调用 finish(action=\"split\", summary=\"单任务直接指派给 {assignee}\") 交接。"
    )


def tool_delegate_explore(ctx: ToolContext, args: dict) -> str:
    """并发派生多个轻量只读子代理，执行并行代码检索与架构摸底。"""
    if ctx.is_subagent:
        return (
            "错误：当前已处于子代理执行环境中，严禁递归派生更多子代理（深度上限=1）。"
            "请使用基础只读工具（list_dir / grep / read_file）完成本次调研并 finish。"
        )

    if ctx.subagent_call_count >= 2:
        return "错误：当前任务内子代理并发探索已达上限（已调用 2 批次）。请基于已有探索报告推进后续开发或拆单决策。"

    subagents_data = args.get("subagents")
    if not isinstance(subagents_data, list) or not subagents_data:
        return "错误：必须通过 subagents 数组指定 1~4 个子代理任务（每项包含 name 和 focus）。"

    if len(subagents_data) > 4:
        return f"错误：单次最多允许派生 4 个子代理（当前：{len(subagents_data)} 项）。请合并精简后重试。"

    llm_cfg = getattr(ctx, "llm_config", None)
    if not llm_cfg:
        return "错误：当前执行环境未绑定大模型配置，无法派生子代理。"

    from .subagent import run_subagents_fan_out
    ctx.subagent_call_count += 1
    report, usage = run_subagents_fan_out(
        parent_ctx=ctx,
        subagents_data=subagents_data,
        llm_cfg=llm_cfg,  # type: ignore[arg-type]
    )

    if ctx.subagent_usage is not None:
        ctx.subagent_usage["turns"] += usage["turns"]
        ctx.subagent_usage["prompt_tokens"] += usage["prompt_tokens"]
        ctx.subagent_usage["completion_tokens"] += usage["completion_tokens"]

    return report


TASK_TOOLS: dict[str, tuple[str, dict, object]] = {
    "delegate_explore": (
        "并发派生 2~4 个轻量只读子代理替自己去探索项目的不同模块或排查问题。子代理并行完成代码检索后，系统自动将调查结论汇总返回给你。适合大范围代码摸底、跨端架构调研或对比排查，大幅压缩单兵逐轮阅读的轮数与耗时。",
        {
            "type": "object",
            "properties": {
                "subagents": {
                    "type": "array",
                    "description": "子代理任务清单（建议 2~4 个，最多 4 个）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "子任务简短标题（如「调研后端用户数据模型」）"},
                            "focus": {"type": "string", "description": "子代理需要调研的核心关注点与需要回答的问题"},
                            "paths": {"type": "array", "items": {"type": "string"}, "description": "建议重点关注的相对文件或目录路径（可选）"},
                        },
                        "required": ["name", "focus"],
                    },
                },
            },
            "required": ["subagents"],
        },
        tool_delegate_explore,
    ),
    "create_tasks": (
        "把当前任务拆分为若干子任务并写入本项目任务队列（每条生成规范任务文件，"
        "状态 open，优先级缺省继承当前任务，手动序接在现有任务之后）。用于执行"
        "**早期**（前几轮）就发现任务体量明显超出单轮预算（预计 15+ 轮、大量新"
        "文件、或设计文档明确要 2 天+ 工作量）时：先拆分，再 finish(action=split)"
        "收尾，拆分后不要再继续当前任务的实现。子任务 title 建议带执行序号"
        "（如「1 model 层补全」）；description 写清范围、对应设计文档章节与"
        "可执行的验收命令（如 go build ./internal/model），并把已验证的现状"
        "（哪些文件已完成可用）写给后续任务，避免重复劳动或误判损坏。",
        {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string",
                                      "description": "子任务标题（≤80 字符，建议带序号）"},
                            "description": {"type": "string",
                                            "description": "任务正文：范围/接口契约/验收命令/已知现状"},
                            "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"],
                                         "description": "缺省继承当前任务优先级"},
                            "assignee": {"type": "string",
                                         "description": "负责执行该子任务的角色 ID（项目小组模式下必填，如 backend / frontend / qa）"},
                            "depends_on": {"type": "array", "items": {"type": "string"},
                                           "description": "依赖的既有前置任务 ID 列表"},
                            "depends_on_refs": {"type": "array", "items": {"type": "integer"},
                                                "description": "依赖本批 tasks 数组内其他子任务的 0-based 下标（如 [0, 1] 表示依赖第 1、2 项）"},
                            "labels": {"type": "array", "items": {"type": "string"},
                                       "description": "任务标签列表（如 [\"backend\", \"api\"]）"},
                        },
                        "required": ["title"],
                    },
                    "description": "1~10 个子任务，按建议执行顺序排列",
                },
                "close_current": {"type": "boolean",
                                  "description": "拆分后完成并归档当前任务（默认 true）"},
            },
            "required": ["tasks"],
        },
        tool_create_tasks,
    ),
    "report_bug": (
        "测试工程师（QA）在联调验证中发现缺陷时，向项目任务列表新建 Bug 修复任务"
        "并指派给对应开发角色（backend/frontend 等）。需写明复现步骤、期望与实际现象。",
        {
            "type": "object",
            "properties": {
                "bugs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Bug 简要标题（≤80 字符）"},
                            "assignee": {"type": "string", "description": "指派修复的开发角色 ID（如 backend / frontend）"},
                            "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"],
                                         "description": "优先级（默认 P1）"},
                            "severity": {"type": "string", "enum": ["blocker", "major", "minor"],
                                         "description": "严重度（默认 major）"},
                            "repro_steps": {"type": "string", "description": "详细复现步骤"},
                            "expected": {"type": "string", "description": "期望正常行为"},
                            "actual": {"type": "string", "description": "实际错误表现"},
                            "evidence": {"type": "string", "description": "接口状态码/Console 报错/截图路径等证据"},
                        },
                        "required": ["title", "repro_steps", "expected", "actual"],
                    },
                    "description": "1~10 个待修复 Bug 清单",
                },
            },
            "required": ["bugs"],
        },
        tool_report_bug,
    ),
    "reassign_task": (
        "项目经理（PM）判断当前需求粒度合适、无需拆分子任务时，直接将其指派给"
        "目标开发/测试角色，避免产生多余子任务文件。",
        {
            "type": "object",
            "properties": {
                "assignee": {"type": "string", "description": "目标责任角色 ID（如 backend / frontend）"},
                "note": {"type": "string", "description": "技术评估与交接说明（将写入任务进度）"},
                "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"],
                             "description": "可选：调整任务优先级"},
            },
            "required": ["assignee"],
        },
        tool_reassign_task,
    ),
    "finish": (
        "任务完成或受阻时调用，给出最终结论。必须提供 action/summary 等字段。",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["completed", "blocked", "cannot_do", "split", "wait_retest"],
                           "description": "completed=已完成且验证通过；blocked=受阻；"
                                          "cannot_do=超出能力或需人工决策；"
                                          "split=任务过大，已用 create_tasks 拆分为子任务；"
                                          "wait_retest=QA 联调发现 Bug 已派发修复，等待修复后回归测试"},
                "summary": {"type": "string"},
                "files_changed": {"type": "array", "items": {"type": "string"}},
                "commit_message": {"type": "string"},
                "notes": {"type": "string"},
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string",
                                     "description": "相对项目根的文件路径"},
                            "line": {"type": "integer",
                                     "description": "行号（可省略）"},
                            "severity": {"type": "string",
                                         "enum": ["blocker", "major", "minor"],
                                         "description": "blocker=阻断验收；major=功能缺陷；minor=次要问题"},
                            "message": {"type": "string",
                                        "description": "问题描述与建议"},
                        },
                        "required": ["file", "severity", "message"],
                    },
                    "description": "结构化问题清单（审查场景使用；每条含 file/severity/message）",
                },
            },
            "required": ["action", "summary"],
        },
        tool_finish,
    ),
}

__all__ = [
    "FinishSignal",
    "TASK_TOOLS",
    "tool_create_tasks",
    "tool_delegate_explore",
    "tool_finish",
    "tool_reassign_task",
    "tool_report_bug",
]
