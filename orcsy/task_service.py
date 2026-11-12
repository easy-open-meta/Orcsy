"""任务文件 CRUD 服务（从 orcsy/web.AppState 逐字拆分，Stage 3 绞杀者重构）。

职责：看板侧的任务文件创建/更新、状态流转、拖拽排序、批量删除、清空与单任务读取。
纯业务函数：显式传入 `agents: dict[str, ProjectAgent]`，不感知 HTTP；
错误抛出顺序与消息文本与拆分前逐字一致（含「未知项目」晚于「标题不能为空」
的校验优先级）。

日志沿用 `orcsy.web` 流（get_logger("web")）保持运维侧日志身份不变。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .logging_setup import get_logger
from .task_model import (
    _next_requeue_seq,
    _set_frontmatter_field,
    detect_dependency_cycle,
    parse_task_file,
    priority_rank,
    set_task_file_order,
)

log = get_logger("web")

# 任务状态机（与 task_model.TaskStatus 同域：open → in_progress → done/blocked/cancelled）
VALID_STATUS = ("open", "in_progress", "done", "blocked", "cancelled")

# 任务文件 frontmatter 识别（status 重写用）
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def task_path(agents: dict, project: str, task_id: str) -> Path:
    agent = agents.get(project)
    if agent is None:
        raise ValueError(f"未知项目：{project}")
    if not re.fullmatch(r"[\w][\w\-. ]{0,80}", task_id):
        raise ValueError(f"任务 ID 不合法：{task_id!r}（只允许字母数字_-. 和空格）")
    path = (agent.tasks_dir / f"{task_id}.md").resolve()
    if agent.tasks_dir.resolve() not in path.parents:
        raise ValueError("任务路径越界")
    return path


def create_or_update_task(agents: dict, data: dict) -> dict:
    project = str(data.get("project", ""))
    task_id = str(data.get("id", "")).strip()
    title = str(data.get("title", "")).strip()
    priority = str(data.get("priority", "P2")).strip().upper()
    status = str(data.get("status", "open")).strip().lower()
    description = str(data.get("description", "")).strip()

    if not title:
        raise ValueError("标题不能为空")
    if priority_rank(priority) > 4:
        raise ValueError(f"优先级不合法：{priority}")
    if status not in VALID_STATUS:
        raise ValueError(f"状态不合法：{status}")

    # id 留空 = 自动生成「优先级-标题slug」，与模板的 P1-example-bugfix 命名一致
    if not task_id:
        task_id = auto_task_id(agents, project, priority, title)

    path = task_path(agents, project, task_id)
    labels = data.get("labels") or []
    labels_line = ", ".join(str(x) for x in labels) if isinstance(labels, list) else ""

    # 保留已有任务的协同元字段（编辑任务不得静默丢失 parent_id/split_depth/qa_round 等）
    old = parse_task_file(path) if path.exists() else None
    if old is not None and old.error:
        old = None

    # 前置依赖：请求带 depends_on 键则以请求为准（空列表=清除），否则沿用原值
    if "depends_on" in data:
        deps_raw = data.get("depends_on") or []
        if isinstance(deps_raw, str):
            deps_raw = [x for x in re.split(r"[,，\s]+", deps_raw) if x]
        if not isinstance(deps_raw, list):
            raise ValueError("depends_on 必须是任务 ID 列表")
        depends_on: list[str] = []
        for d in deps_raw:
            d = str(d).strip()
            if d and d not in depends_on:
                depends_on.append(d)
    else:
        depends_on = list(old.depends_on) if old else []
    if task_id in depends_on:
        raise ValueError("任务不能依赖自身")
    if depends_on:
        agent = agents.get(project)
        known = {p.stem for p in agent.tasks_dir.glob("*.md")} if agent and agent.tasks_dir.is_dir() else set()
        done_dir = agent.tasks_dir / "done" if agent else None
        if done_dir and done_dir.is_dir():
            known |= {p.stem for p in done_dir.glob("*.md")}
        missing = [d for d in depends_on if d not in known]
        if missing:
            raise ValueError(f"前置任务不存在：{', '.join(missing)}")
        cyc = detect_dependency_cycle(agent.tasks_dir, extra_edges={task_id: depends_on})
        if cyc:
            raise ValueError(f"前置依赖存在循环：{' -> '.join(cyc)}")

    assignee = str(data.get("assignee") or "").strip()
    agent_cfg = agents.get(project)
    if (assignee and agent_cfg is not None and agent_cfg.team_enabled
            and assignee != project and assignee not in agent_cfg.members):
        raise ValueError(
            f"指派角色 {assignee!r} 不在本项目小组内，可用角色："
            f"{', '.join(agent_cfg.members) or '（无）'}")

    title_yaml = (json.dumps(title, ensure_ascii=False)
                  if any(ch in title for ch in (":", "#", "[", "]", "{", "}", '"', "'"))
                  else title)
    fm = [
        "---",
        f"title: {title_yaml}",
        f"priority: {priority}",
        f"status: {status}",
    ]
    if labels_line:
        fm.append(f"labels: [{labels_line}]")
    if assignee:
        fm.append(f"assignee: {assignee}")
    if depends_on:
        fm.append("depends_on: [" + ", ".join(depends_on) + "]")
    if old is not None:
        if old.parent_id:
            fm.append(f"parent_id: {old.parent_id}")
        if old.split_depth:
            fm.append(f"split_depth: {old.split_depth}")
        if old.qa_round:
            fm.append(f"qa_round: {old.qa_round}")
        if old.created_by:
            fm.append(f"created_by: {old.created_by}")
    else:
        fm.append("created_by: user")
    # 新建任务排到手动序末尾（已有 order 的最大值 +1）：看板里新建的
    # 任务接在已排序列后面，而不是插队到最前；更新已有任务不动 order
    if not path.exists():
        fm.append(f"order: {next_task_order(agents, project)}")
    elif old is not None and old.order is not None:
        fm.append(f"order: {old.order}")
    fm.append("---")
    path.parent.mkdir(parents=True, exist_ok=True)
    # 描述留空用标题兜底：空正文会被解析器判为格式错误任务
    body = description or title
    path.write_text("\n".join(fm) + "\n\n" + body + "\n", encoding="utf-8")
    log.info("Web 创建/更新任务 %s/%s [%s]", project, task_id, priority)
    return {"ok": True, "file": str(path), "id": task_id}


def next_task_order(agents: dict, project: str) -> int:
    """新建任务的手动序号：项目内已有 order 的最大值 +1（无则 1）。"""
    agent = agents.get(project)
    if agent is None or not agent.tasks_dir.is_dir():
        return 1
    orders = []
    for f in agent.tasks_dir.glob("*.md"):
        t = parse_task_file(f)
        if t.order is not None:
            orders.append(t.order)
    return max(orders) + 1 if orders else 1


def reorder_tasks(agents: dict, data: dict) -> dict:
    """看板拖拽排序落盘：按给定 id 顺序重写 frontmatter 的 order（1..N）。

    只写发生变化的文件；ids 里不存在的任务跳过（并发删除等竞态 tolerable）。
    仅影响看板显示顺序，Agent 领取仍按优先级（scan_tasks 不受影响）。
    """
    project = str(data.get("project") or "")
    ids = data.get("ids")
    if project not in agents:
        raise ValueError(f"未知项目：{project}")
    if not isinstance(ids, list) or not ids:
        raise ValueError("ids 必须是非空数组（拖拽后的完整任务顺序）")
    agent = agents[project]
    updated = 0
    for i, tid in enumerate(ids, 1):
        tid = str(tid).strip()
        if not tid:
            continue
        path = agent.tasks_dir / f"{tid}.md"
        if not path.is_file():
            continue  # 并发删除/改名：跳过，不让整次排序失败
        t = parse_task_file(path)
        if t.error:
            continue  # 解析失败的文件不碰，避免把脏 frontmatter 写得更脏
        if t.order != i:
            if set_task_file_order(path, i):
                updated += 1
    log.info("Web 拖拽排序 %s：%d 个任务重编号（%d 个文件落盘）",
             project, len(ids), updated)
    return {"ok": True, "updated": updated}


def auto_task_id(agents: dict, project: str, priority: str, title: str) -> str:
    """从优先级 + 标题生成唯一任务 ID：P2-修复登录超时 / P1-fix-login。"""
    agent = agents.get(project)
    if agent is None:
        raise ValueError(f"未知项目：{project}")
    slug = re.sub(r"\s+", "-", title.strip())
    slug = re.sub(r"[^\w\-]", "", slug).strip("-")[:40].strip("-") or "task"
    existing = {p.stem for p in agent.tasks_dir.glob("*.md")} if agent.tasks_dir.is_dir() else set()
    base = f"{priority}-{slug}"
    task_id, n = base, 2
    while task_id in existing:
        task_id = f"{base}-{n}"
        n += 1
    return task_id


def set_task_status(agents: dict, data: dict) -> dict:
    project = str(data.get("project", ""))
    task_id = str(data.get("id", ""))
    status = str(data.get("status", "")).strip().lower()
    if status not in VALID_STATUS:
        raise ValueError(f"状态不合法：{status}")
    path = task_path(agents, project, task_id)
    if not path.exists():
        raise ValueError(f"任务不存在：{task_id}")
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("任务文件缺少 frontmatter，无法修改状态")
    front, rest = m.group(1), text[m.end():]
    if re.search(r"^status:", front, re.MULTILINE):
        front = re.sub(r"^status:.*$", f"status: {status}", front, flags=re.MULTILINE)
    else:
        front += f"\nstatus: {status}"
    path.write_text(f"---\n{front}\n---\n{rest}", encoding="utf-8")
    # 受阻重入队插队戳：→ open 时递增（下一轮扫描优先领取），离开 open 清除
    if status == "open":
        _set_frontmatter_field(path, None, "requeue_seq",
                               str(_next_requeue_seq(path.parent)))
    else:
        _set_frontmatter_field(path, None, "requeue_seq", None)
    log.info("Web 修改任务状态 %s/%s -> %s", project, task_id, status)
    return {"ok": True}


def delete_task(agents: dict, data: dict) -> dict:
    project = str(data.get("project", ""))
    task_id = str(data.get("id", ""))
    path = task_path(agents, project, task_id)
    if not path.exists():
        raise ValueError(f"任务不存在：{task_id}")
    path.unlink()
    log.info("Web 删除任务 %s/%s", project, task_id)
    return {"ok": True}


def delete_tasks_batch(agents: dict, data: dict) -> dict:
    """批量删除任务：{project, ids: [...]}。单条缺失不阻断，返回删除/失败明细。"""
    project = str(data.get("project", "")).strip()
    if not agents.get(project):
        raise ValueError(f"项目不存在：{project}")
    ids = data.get("ids")
    if not isinstance(ids, list) or not ids:
        raise ValueError("ids 必须是非空数组")
    if len(ids) > 500:
        raise ValueError("单次最多删除 500 个任务")
    deleted, failed = 0, []
    for raw in ids[:500]:
        task_id = str(raw).strip()
        try:
            path = task_path(agents, project, task_id)
            if not path.exists():
                failed.append(task_id)
                continue
            path.unlink()
            deleted += 1
        except (ValueError, OSError) as e:
            failed.append(f"{task_id}（{e}）")
    log.info("Web 批量删除任务 %s：删除 %d，失败 %d", project, deleted, len(failed))
    msg = f"已删除 {deleted} 个任务"
    if failed:
        msg += f"，{len(failed)} 个失败：{'、'.join(failed[:5])}{'…' if len(failed) > 5 else ''}"
    return {"ok": True, "deleted": deleted, "failed": failed, "message": msg}


def clear_tasks(agents: dict, data: dict) -> dict:
    """一键清空项目任务：删除 tasks/ 根目录全部 .md（含解析失败文件），
    已归档任务（tasks/done/）不动。"""
    project = str(data.get("project", "")).strip()
    agent = agents.get(project)
    if agent is None:
        raise ValueError(f"项目不存在：{project}")
    tasks_dir = agent.tasks_dir
    files = list(tasks_dir.glob("*.md"))
    cleared = 0
    for path in files:
        try:
            path.unlink()
            cleared += 1
        except OSError as e:
            log.warning("清空任务：删除 %s 失败（%s）", path, e)
    log.info("Web 清空项目任务 %s：清除 %d/%d 个任务文件（归档未动）",
             project, cleared, len(files))
    return {"ok": True, "cleared": cleared,
            "message": f"已清空 {cleared} 个任务（已归档任务不受影响）"}


def get_task(agents: dict, params: dict) -> dict:
    """读取单个任务的完整内容（编辑/查看弹窗用；/api/state 的描述截断到 2000 字）。

    待办在 tasks/ 根目录；已完成任务被归档进 tasks/done/ ——两处都找，
    归档命中时带 archived=true，看板以只读模式打开。
    """
    project = str(params.get("project", ""))
    task_id = str(params.get("id", ""))
    agent = agents.get(project)
    if agent is None:
        raise ValueError(f"未知项目：{project}")
    path = task_path(agents, project, task_id)  # 复用 ID 合法性与路径越界校验
    archived = False
    if not path.exists():
        archived = True
        path = agent.tasks_dir / "done" / path.name
    if not path.exists():
        raise ValueError(f"任务不存在：{task_id}")

    t = parse_task_file(path)
    if t.error:
        return {"ok": True, "id": task_id, "title": t.title, "archived": archived,
                "priority": "P2", "labels": [], "assignee": "",
                "depends_on": [], "description": "", "error": t.error}
    return {"ok": True, "id": t.id, "title": t.title, "priority": t.priority,
            "status": t.status, "labels": t.labels, "assignee": t.assignee,
            "depends_on": list(t.depends_on), "parent_id": t.parent_id,
            "qa_round": t.qa_round,
            "description": t.description, "archived": archived}


__all__ = [
    "FRONTMATTER_RE",
    "VALID_STATUS",
    "auto_task_id",
    "clear_tasks",
    "create_or_update_task",
    "delete_task",
    "delete_tasks_batch",
    "get_task",
    "next_task_order",
    "reorder_tasks",
    "set_task_status",
    "task_path",
]
