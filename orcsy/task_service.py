"""任务文件 CRUD 服务（从 orcsy/web.AppState 逐字拆分，Stage 3 绞杀者重构）。

职责：看板侧的任务文件创建/更新、状态流转、拖拽排序、批量删除、清空与单任务读取。
纯业务函数：显式传入 `agents: dict[str, ProjectAgent]`，不感知 HTTP；
错误抛出顺序与消息文本与拆分前逐字一致（含「未知项目」晚于「标题不能为空」
的校验优先级）。

并发控制：任务文件版本快照（task_version_token）+ 乐观锁（base_version 前置
校验）——手机端远程编辑保存时，若后台 Agent 或其他终端已改过任务，抛出
TaskConflictError，由 Web 层转为 409 Conflict，绝不静默覆盖。

日志沿用 `orcsy.web` 流（get_logger("web")）保持运维侧日志身份不变。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .logging_setup import get_logger
from .session_bus import cleanup_if_finished
from .task_model import (
    _next_requeue_seq,
    _set_frontmatter_field,
    archive_task_file,
    detect_dependency_cycle,
    parse_task_file,
    priority_rank,
    read_dependency_overview,
    set_task_file_order,
)

log = get_logger("web")

# 任务状态机（与 task_model.TaskStatus 同域：open → in_progress → done/blocked/cancelled）
VALID_STATUS = ("open", "in_progress", "done", "blocked", "cancelled")

# 任务文件 frontmatter 识别（status 重写用）
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


class TaskConflictError(Exception):
    """任务并发编辑冲突（乐观锁命中）：任务在 base_version 之后已被修改。

    payload 携带当前最新任务内容，Web 层转 409 Conflict 随响应体回给前端，
    由用户决定「保留我的修改（强制覆盖）」还是「采用最新状态」。
    """

    def __init__(self, message: str, latest_task: dict | None = None) -> None:
        super().__init__(message)
        self.latest_task = latest_task or {}


def task_version_token(path: Path, status: str = "") -> str:
    """任务文件的版本快照签名：MD5(mtime_ns + 文件大小 + 状态) 前 12 位。

    无状态轻量签名：Agent 或其他终端对任务文件的任何落盘改动（mtime/大小/
    状态变化）都会改变签名，作为乐观锁的 base_version 比对基准。
    文件不存在时返回空串（新建任务无需版本校验）。
    """
    try:
        st = path.stat()
    except OSError:
        return ""
    raw = f"{st.st_mtime_ns}-{st.st_size}-{status}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]  # noqa: S324 — 非安全用途，仅快照比对


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

    # 乐观并发校验（移动端远程编辑防脏写）：编辑已有任务时前端回传 base_version
    # （GET /api/task 下发的版本快照）。后台 Agent 已改过任务（状态流转/审查
    # 回写）则拒绝覆盖，抛 409 由用户决定强制覆盖还是采用最新。
    base_version = str(data.get("base_version") or "").strip()
    force_overwrite = bool(data.get("force_overwrite"))
    if path.exists() and base_version and not force_overwrite:
        old_probe = parse_task_file(path)
        current_version = task_version_token(
            path, "" if old_probe.error else old_probe.status)
        if base_version != current_version:
            cur_status = "" if old_probe.error else old_probe.status
            log.info("Web 任务保存冲突 %s/%s：base=%s 现为=%s（状态 %s）",
                     project, task_id, base_version, current_version, cur_status or "?")
            raise TaskConflictError(
                "任务已被后台 Agent 或其他终端修改"
                + (f"（状态已变更为 {cur_status}）" if cur_status else ""),
                latest_task=get_task(agents, {"project": project, "id": task_id}))

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
    # 回传落盘后的新版本快照：前端下次编辑以此作为 base_version
    new_version = task_version_token(path, status)
    return {"ok": True, "file": str(path), "id": task_id,
            "version_token": new_version}


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
    agent = agents.get(project)
    if agent is None:
        raise ValueError(f"未知项目：{project}")
    path = task_path(agents, project, task_id)
    if not path.exists():
        # 已归档（tasks/done/）的任务也允许改状态——典型场景是「重新入队」；
        # 改回 open 时文件会回迁到 tasks/ 根目录重新参与调度。
        archived_path = agent.tasks_dir / "done" / path.name
        if not archived_path.exists():
            raise ValueError(f"任务不存在：{task_id}")
        path = archived_path
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
    # 手工重开已归档任务：回迁到 tasks/ 根目录重新参与调度
    if status == "open" and path.parent.name == "done":
        restored = path.parent.parent / path.name
        try:
            path.replace(restored)
            path = restored
            log.info("Web 重新入队已归档任务 %s/%s，回迁到 %s", project, task_id, restored)
        except OSError as e:
            log.warning("回迁任务 %s/%s 失败（留在 done/）：%s", project, task_id, e)
    # Phase 1：人工把批次内最后一个任务标为 done/cancelled 时同步销毁共享剪贴板
    if status in ("done", "cancelled"):
        try:
            if getattr(agent, "claims", None) is not None:
                t = parse_task_file(path)
                cleanup_if_finished(agent.project, agent.claims.state_dir, t,
                                    tasks_dir=agent.tasks_dir)
        except Exception:  # noqa: BLE001 — 清理旁路
            log.warning("按批次清理共享剪贴板失败（忽略）", exc_info=True)
    # 手工标 done 与 Agent 完成走同一归档落点（tasks/done/），保证看板
    # 「已归档」列表状态一致；cancelled/blocked 留在原地便于重新入队。
    if status == "done":
        target = archive_task_file(path, agent.tasks_dir)
        if target is not None:
            log.info("Web 人工完成任务 %s/%s 已归档到 %s", project, task_id, target)
    log.info("Web 修改任务状态 %s/%s -> %s", project, task_id, status)
    return {"ok": True}


def delete_task(agents: dict, data: dict) -> dict:
    project = str(data.get("project", ""))
    task_id = str(data.get("id", ""))
    path = task_path(agents, project, task_id)
    if not path.exists():
        raise ValueError(f"任务不存在：{task_id}")
    t = parse_task_file(path)
    path.unlink()
    try:  # 删除后批次可能已无未完成任务：回收共享剪贴板
        agent = agents.get(project)
        if agent is not None and getattr(agent, "claims", None) is not None:
            cleanup_if_finished(agent.project, agent.claims.state_dir, t,
                                tasks_dir=agent.tasks_dir)
    except Exception:  # noqa: BLE001 — 清理旁路
        log.warning("删除任务后清理共享剪贴板失败（忽略）", exc_info=True)
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
                "depends_on": [], "dep_tasks": [], "description": "", "error": t.error,
                "version_token": ""}
    return {"ok": True, "id": t.id, "title": t.title, "priority": t.priority,
            "status": t.status, "labels": t.labels, "assignee": t.assignee,
            "depends_on": list(t.depends_on),
            # Phase 1：前序依赖概览（状态/归档/成果摘要），供任务详情一键查看/跳转
            "dep_tasks": read_dependency_overview(agent.tasks_dir, t.depends_on),
            "parent_id": t.parent_id,
            "qa_round": t.qa_round,
            "description": t.description, "archived": archived,
            # 版本快照：编辑保存时回传 base_version 触发乐观锁比对
            "version_token": task_version_token(path, t.status)}


__all__ = [
    "FRONTMATTER_RE",
    "VALID_STATUS",
    "TaskConflictError",
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
    "task_version_token",
]
