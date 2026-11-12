"""任务文件模型与解析。

每个项目用 `tasks/` 目录管理任务，每个任务一个 Markdown 文件，文件名即任务 ID：

    tasks/P1-fix-login-crash.md
    tasks/P2-add-export-feature.md

文件格式（YAML frontmatter + 正文描述）：

    ---
    title: 修复登录页崩溃
    priority: P1
    status: open
    ---

    ## 现象
    登录按钮点击后应用闪退……

    ## 复现步骤
    1. …

    ## 验收标准
    - [ ] …
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import TaskStatus

# 优先级 P0 最高，数字越大越低；无法解析时视为 P9
_PRIORITY_RE = re.compile(r"^P(\d+)$", re.IGNORECASE)

# 任务正文里的执行进度节：执行中每轮追加，进程意外退出后由启动恢复流程接力
PROGRESS_HEADING = "## 执行进度"
_FRONTMATTER_SPLIT_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


def priority_rank(priority: str) -> int:
    m = _PRIORITY_RE.match(priority.strip())
    return int(m.group(1)) if m else 9


@dataclass
class Task:
    id: str               # 文件名（不含 .md）
    path: Path
    title: str = ""
    priority: str = "P9"
    status: str = "open"
    labels: list[str] = field(default_factory=list)
    assignee: str = ""    # 留空表示任意 Agent 均可领取
    description: str = ""
    error: str = ""       # 解析失败原因（仅在 parse_errors 中出现）
    # 看板拖拽排序的手动序号（frontmatter order: N）；None = 未手动排过，
    # 同优先级内决定显示与领取顺序
    order: int | None = None
    # 最近一次受阻的轮次戳（frontmatter requeue_seq: N，重新入队时写入）。
    # 领取排序的最高优先信号：数值越大 = 越近被重新入队，越先领取——
    # 受阻任务（如后端失败）重跑不应排到队尾等所有新任务跑完
    requeue_seq: int = 0
    # 待审查标记（frontmatter review_pending: true）：Agent 已完成并落 WIP
    # 提交到工作分支，审查门未出结论（审查中 / 审查失败待重试 / 进程中断
    # 残留）。补审扫描只认此标记——旧任务文件无此字段（解析为 false）天然
    # 不受影响，已归档任务永不追溯
    review_pending: bool = False
    # 审查不通过累计轮数（frontmatter review_round: N）；达到生效返工上限
    # 转 blocked 等人工。通过后保留作审计
    review_round: int = 0
    # 项目小组协同与 DAG 依赖字段（均可选，老任务缺省取默认值）
    depends_on: list[str] = field(default_factory=list)  # 前置任务 ID 列表；全部进入 done/ 后才可领取
    parent_id: str = ""      # 所属父需求或触发当前 Bug 的 QA 任务 ID
    split_depth: int = 0     # 拆分深度（用户下发=0，PM 拆出=1），防 PM 递归无限拆分
    qa_round: int = 0        # QA 回归测试已执行轮数（防 Bug 修复死循环）
    created_by: str = ""     # 创建者标识（user / issue / pm / qa）

    @property
    def rank(self) -> int:
        return priority_rank(self.priority)

    @property
    def is_actionable(self) -> bool:
        return self.status == "open" and not self.error

    @property
    def kind(self) -> str:
        """从文件名/标签粗略推断任务类型，供日志与提交信息使用。"""
        text = f"{self.id} {' '.join(self.labels)}".lower()
        if "bug" in text or "fix" in text or "修复" in text:
            return "bugfix"
        if "feat" in text or "add" in text or "功能" in text:
            return "feature"
        return "task"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
                if isinstance(meta, dict):
                    return meta, parts[2].strip()
            except yaml.YAMLError:
                pass
    return {}, text.strip()


def parse_task_file(path: Path) -> Task:
    """解析单个任务文件；解析失败时返回带 error 的 Task（保留文件定位信息）。"""
    task_id = path.stem
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return Task(id=task_id, path=path, error=f"无法读取文件：{e}")

    meta, body = _parse_frontmatter(text)
    title = str(meta.get("title") or "").strip()
    priority = str(meta.get("priority") or "P9").strip().upper()
    status = str(meta.get("status") or "open").strip().lower()
    labels_raw = meta.get("labels")
    labels = (
        [str(x).strip() for x in labels_raw if str(x).strip()]
        if isinstance(labels_raw, list) else []
    )
    # order 非法/缺失都当 None（未手动排序），不让脏值把任务判成解析失败
    order: int | None = None
    order_raw = meta.get("order")
    if isinstance(order_raw, int) and not isinstance(order_raw, bool):
        order = order_raw
    elif isinstance(order_raw, str) and order_raw.strip().lstrip("-").isdigit():
        order = int(order_raw.strip())
    # 受阻重入队轮次戳：缺失/非法按 0（从未受阻重入队）
    requeue_seq = 0
    seq_raw = meta.get("requeue_seq")
    if isinstance(seq_raw, int) and not isinstance(seq_raw, bool) and seq_raw > 0:
        requeue_seq = seq_raw
    elif isinstance(seq_raw, str) and seq_raw.strip().isdigit():
        requeue_seq = int(seq_raw.strip())

    # 待审查标记：truthy 即真（宽解析，不让脏值把任务判成解析失败）
    review_pending = bool(meta.get("review_pending", False))

    # 审查不通过轮数：缺失/非法按 0
    review_round = 0
    rr_raw = meta.get("review_round")
    if isinstance(rr_raw, int) and not isinstance(rr_raw, bool) and rr_raw > 0:
        review_round = rr_raw
    elif isinstance(rr_raw, str) and rr_raw.strip().isdigit():
        review_round = int(rr_raw.strip())

    # 前置依赖任务 ID 列表：支持列表或逗号分隔字符串，去重保序
    depends_on: list[str] = []
    deps_raw = meta.get("depends_on")
    if isinstance(deps_raw, list):
        raw_list = [str(x).strip() for x in deps_raw if str(x).strip()]
    elif isinstance(deps_raw, str) and deps_raw.strip():
        raw_list = [x.strip() for x in deps_raw.split(",") if x.strip()]
    else:
        raw_list = []
    seen_deps: set[str] = set()
    for d in raw_list:
        if d not in seen_deps:
            seen_deps.add(d)
            depends_on.append(d)

    parent_id = str(meta.get("parent_id") or "").strip()

    split_depth = 0
    sd_raw = meta.get("split_depth")
    if isinstance(sd_raw, int) and not isinstance(sd_raw, bool) and sd_raw > 0:
        split_depth = sd_raw
    elif isinstance(sd_raw, str) and sd_raw.strip().isdigit():
        split_depth = int(sd_raw.strip())

    qa_round = 0
    qa_raw = meta.get("qa_round")
    if isinstance(qa_raw, int) and not isinstance(qa_raw, bool) and qa_raw > 0:
        qa_round = qa_raw
    elif isinstance(qa_raw, str) and qa_raw.strip().isdigit():
        qa_round = int(qa_raw.strip())

    created_by = str(meta.get("created_by") or "").strip()

    if not title:
        return Task(id=task_id, path=path, error="缺少 frontmatter 中的 title 字段")
    if priority_rank(priority) > 4:
        return Task(id=task_id, path=path, title=title, error=f"priority 不合法：{priority}（应为 P0~P4）")
    if status not in ("open", "in_progress", "done", "blocked", "cancelled"):
        return Task(id=task_id, path=path, title=title, error=f"status 不合法：{status}")
    if not body.strip():
        return Task(id=task_id, path=path, title=title, error="任务正文为空，缺少描述")

    return Task(
        id=task_id,
        path=path,
        title=title,
        priority=priority,
        status=status,
        labels=labels,
        assignee=str(meta.get("assignee") or "").strip(),
        description=body,
        order=order,
        requeue_seq=requeue_seq,
        review_pending=review_pending,
        review_round=review_round,
        depends_on=depends_on,
        parent_id=parent_id,
        split_depth=split_depth,
        qa_round=qa_round,
        created_by=created_by,
    )


def scan_tasks(tasks_dir: Path) -> tuple[list[Task], list[Task]]:
    """扫描任务目录，返回 (可执行任务按优先级升序, 解析失败任务)。

    只有 status=open 且解析成功的任务才会进入可执行列表。
    """
    tasks: list[Task] = []
    errors: list[Task] = []
    if not tasks_dir.is_dir():
        return tasks, errors

    for path in sorted(tasks_dir.glob("*.md")):
        task = parse_task_file(path)
        if task.error:
            errors.append(task)
        elif task.is_actionable:
            tasks.append(task)

    # 领取顺序：**受阻后重新入队的任务最优先**（requeue_seq 越大 = 越近受阻，
    # 如后端失败重跑应接着上次的上下文继续，而不是排到队尾等所有新任务），
    # 其次看板拖拽的手动序（同优先级内），最后回退优先级 → id
    tasks.sort(key=lambda t: (
        0 if t.requeue_seq > 0 else 1,
        -t.requeue_seq,
        t.rank,
        0 if t.order is not None else 1,
        t.order if t.order is not None else 0,
        t.id,
    ))
    return tasks, errors


def _progress_now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def append_progress(path: Path, entry: str, keep_last: int = 40) -> None:
    """向任务文件正文追加一行执行进度（时间戳自动加在行首）。

    进度写在正文末尾的「## 执行进度」节，每轮工具调用都落盘一次——
    进程被强杀时最多丢失正在进行的这一轮，之前的进度都在文件里。
    只保留最近 keep_last 条，避免反复中断重跑把文件撑爆。
    任何 I/O 问题静默忽略：进度记录失败不能影响任务执行本身。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    m = _FRONTMATTER_SPLIT_RE.match(text)
    head, body = (m.group(0), text[m.end():]) if m else ("", text)

    heading = PROGRESS_HEADING
    marker = f"\n{heading}\n"
    idx = body.find(marker)
    line = f"- {_progress_now()} {entry}"
    if idx >= 0:
        start = idx + 1                              # 指向标题行首
        rest_at = start + len(heading) + 1
        nxt = re.search(r"\n## ", body[rest_at:])
        end = rest_at + nxt.start() if nxt else len(body)
        lines = [ln for ln in body[start:end].splitlines() if ln.strip()]
        lines = lines[:1] + lines[1:][-keep_last:] + [line]
        body = body[:start] + "\n".join(lines) + "\n" + body[end:]
    else:
        body = body.rstrip("\n") + f"\n\n{heading}\n\n{line}\n"
    try:
        path.write_text(head + body, encoding="utf-8")
    except OSError:
        pass


def _insert_frontmatter_field(text: str, head: str, rest_start: int,
                              line: str) -> str:
    """把一行字段插进已有 frontmatter 的收尾 --- 之前。

    _FRONTMATTER_SPLIT_RE 的 group(0) 含收尾的 "---\\n"——直接往后拼会把字段
    写到 frontmatter 外面（曾导致 order 行落在正文前、且每次重排叠加一段）。
    """
    assert head.endswith("---\n")
    return head[: -len("---\n")] + line + "---\n" + text[rest_start:]


def set_task_file_order(path: Path, order: int | None) -> bool:
    """改写/清除任务文件 frontmatter 的 order 字段（看板拖拽排序落盘用）。

    order=None 时删除该行（回到「未手动排序」）；写法与 set_task_file_status 同构。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    m = _FRONTMATTER_SPLIT_RE.match(text)
    if order is None:
        if not m or not re.search(r"^order:.*$", m.group(0), re.MULTILINE):
            return True  # 本就没有 order 行，无需写盘
        front = re.sub(r"^order:.*\n?", "", m.group(0), flags=re.MULTILINE)
        text = front + text[m.end():]
    elif not m:
        text = f"---\norder: {order}\n---\n\n{text}"
    elif re.search(r"^order:.*$", m.group(0), re.MULTILINE):
        front = re.sub(r"^order:.*$", f"order: {order}", m.group(0), flags=re.MULTILINE)
        text = front + text[m.end():]
    else:
        text = _insert_frontmatter_field(text, m.group(0), m.end(),
                                         f"order: {order}\n")
    try:
        path.write_text(text, encoding="utf-8")
        return True
    except OSError:
        return False


def set_task_file_review_pending(path: Path, pending: bool) -> bool:
    """改写/清除任务文件 frontmatter 的 review_pending 字段（审查门状态落盘）。

    WIP 提交后写 true（崩溃可无损恢复），出结论（pass/fail/返工/受阻）写
    false；审查 error 待重试路径保持 true。写法与 set_task_file_status 同构。
    """
    return _set_frontmatter_field(path, None, "review_pending",
                                  "true" if pending else None)


def increment_task_file_review_round(path: Path) -> int:
    """任务文件 frontmatter 的 review_round 加一并返回新值（审查 fail 落盘用）。

    读取-改写在单线程调度循环内执行，无并发竞争。任何 I/O 问题按 0 处理。
    """
    current = 0
    try:
        meta, _ = _parse_frontmatter(path.read_text(encoding="utf-8"))
        raw = meta.get("review_round")
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            current = raw
        elif isinstance(raw, str) and raw.strip().isdigit():
            current = int(raw.strip())
    except OSError:
        pass
    nxt = current + 1
    _set_frontmatter_field(path, None, "review_round", str(nxt))
    return nxt


def set_task_file_status(path: Path, status: str) -> bool:
    """改写任务文件 frontmatter 的 status 字段（无 frontmatter 时补一个）。

    重新入队（→ open）时同时写递增的 requeue_seq：受阻任务重入队后下一轮
    扫描优先领取（不排到队尾）；离开 open（标 done/blocked 等）时清除该戳。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if status == "open":
        seq = _next_requeue_seq(path.parent)
        return (_set_frontmatter_field(path, text, "status", str(status))
                and _set_frontmatter_field(path, None, "requeue_seq", str(seq)))
    changed = _set_frontmatter_field(path, text, "status", str(status))
    if changed:
        _set_frontmatter_field(path, None, "requeue_seq", None)  # 清除受阻戳
    return changed


def _next_requeue_seq(tasks_dir: Path) -> int:
    """下一个受阻重入队轮次戳：全项目现有最大值 +1（无则 1）。"""
    seq = 0
    try:
        for f in tasks_dir.glob("*.md"):
            t = parse_task_file(f)
            seq = max(seq, t.requeue_seq)
        for f in (tasks_dir / "done").glob("*.md"):
            t = parse_task_file(f)
            seq = max(seq, t.requeue_seq)
    except OSError:
        pass
    return seq + 1


def _set_frontmatter_field(path: Path, text: str | None, field: str,
                           value: str | None) -> bool:
    """写入/更新/删除 frontmatter 的单行字段（value=None 表示删除该行）。

    text=None 时自行读文件；frontmatter 不存在则新建。任何 I/O 失败返回 False。
    """
    if text is None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return False
    m = _FRONTMATTER_SPLIT_RE.match(text)
    if m:
        if value is None:
            if not re.search(rf"^{field}:.*$", m.group(0), re.MULTILINE):
                return True  # 本就没有该行，无需写盘
            front = re.sub(rf"^{field}:.*\n?", "", m.group(0), flags=re.MULTILINE)
            new_text = front + text[m.end():]
        elif re.search(rf"^{field}:.*$", m.group(0), re.MULTILINE):
            front = re.sub(rf"^{field}:.*$", f"{field}: {value}",
                           m.group(0), flags=re.MULTILINE)
            new_text = front + text[m.end():]
        else:
            new_text = _insert_frontmatter_field(text, m.group(0), m.end(),
                                                 f"{field}: {value}\n")
    elif value is not None:
        new_text = f"---\n{field}: {value}\n---\n\n{text}"
    else:
        return True  # 无 frontmatter 也没有可删的字段
    try:
        path.write_text(new_text, encoding="utf-8")
        return True
    except OSError:
        return False


def set_task_file_assignee(path: Path, assignee: str | None) -> bool:
    """改写或清除任务文件 frontmatter 的 assignee 字段。"""
    clean = str(assignee or "").strip()
    return _set_frontmatter_field(path, None, "assignee", clean if clean else None)


def set_task_file_dependencies(path: Path, deps: list[str]) -> bool:
    """改写或清除任务文件 frontmatter 的 depends_on 字段（兼容单行与多行列表）。"""
    clean: list[str] = []
    seen: set[str] = set()
    for d in deps:
        s = str(d).strip()
        if s and s != path.stem and s not in seen:
            seen.add(s)
            clean.append(s)

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False

    m = _FRONTMATTER_SPLIT_RE.match(text)
    if m:
        front = m.group(0)
        # 剥离可能存在的单行或缩进多行 depends_on 块（(?!-) 防止误匹配收尾 ---）
        stripped_front = re.sub(
            r"^depends_on:.*(?:\n[ \t]*-(?!-).*)*\n?",
            "",
            front,
            flags=re.MULTILINE,
        )
        if clean:
            formatted = "[" + ", ".join(clean) + "]"
            new_text = _insert_frontmatter_field(
                stripped_front + text[m.end():],
                stripped_front,
                len(stripped_front),
                f"depends_on: {formatted}\n",
            )
        else:
            if stripped_front == front:
                return True
            new_text = stripped_front + text[m.end():]
    elif clean:
        formatted = "[" + ", ".join(clean) + "]"
        new_text = f"---\ndepends_on: {formatted}\n---\n\n{text}"
    else:
        return True

    try:
        path.write_text(new_text, encoding="utf-8")
        return True
    except OSError:
        return False


def append_task_dependencies(path: Path, new_deps: list[str]) -> list[str]:
    """向任务文件追加新的前置依赖（去重保序）并返回更新后的列表。"""
    t = parse_task_file(path)
    merged = list(t.depends_on)
    seen = set(merged)
    for d in new_deps:
        s = str(d).strip()
        if s and s != path.stem and s not in seen:
            seen.add(s)
            merged.append(s)
    if merged != t.depends_on:
        set_task_file_dependencies(path, merged)
    return merged


def increment_task_file_qa_round(path: Path) -> int:
    """任务文件 frontmatter 的 qa_round 加一并返回新值（QA 回归计数）。"""
    current = 0
    try:
        meta, _ = _parse_frontmatter(path.read_text(encoding="utf-8"))
        raw = meta.get("qa_round")
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            current = raw
        elif isinstance(raw, str) and raw.strip().isdigit():
            current = int(raw.strip())
    except OSError:
        pass
    nxt = current + 1
    _set_frontmatter_field(path, None, "qa_round", str(nxt))
    return nxt


# ---- 任务 DAG 依赖引擎 ----

def collect_task_statuses(tasks_dir: Path) -> tuple[dict[str, str], set[str]]:
    """收集项目任务目录的状态映射与已完成集合（含 tasks/done/）。

    返回 (all_statuses, done_ids)：
    - done_ids: tasks/done/*.md 的文件名 stem，以及 tasks/*.md 中 status=="done" 的 ID；
    - all_statuses: 所有已知任务的 {task_id: status}（done/ 内固定为 "done"）。
    """
    all_statuses: dict[str, str] = {}
    done_ids: set[str] = set()
    if not tasks_dir.is_dir():
        return all_statuses, done_ids

    done_dir = tasks_dir / "done"
    if done_dir.is_dir():
        for f in done_dir.glob("*.md"):
            done_ids.add(f.stem)
            all_statuses[f.stem] = "done"

    for f in tasks_dir.glob("*.md"):
        t = parse_task_file(f)
        if t.error:
            all_statuses[f.stem] = "error"
        else:
            all_statuses[f.stem] = t.status
            if t.status == "done":
                done_ids.add(f.stem)
    return all_statuses, done_ids


def evaluate_task_dependencies(
    task: Task,
    all_statuses: dict[str, str],
    done_ids: set[str],
) -> dict:
    """评估单个任务的依赖就绪状态。

    返回字典：
    - dep_state: "ready"（可领取）| "waiting"（等待前置进行中）| "upstream_blocked"（前置受阻/取消/解析失败）
    - blocking_deps: 尚未满足的前置任务列表 [{"id": str, "status": str}]
    - done_count: 已完成前置数量
    - total_deps: 总前置数量
    """
    deps = task.depends_on
    if not deps:
        return {
            "dep_state": "ready",
            "blocking_deps": [],
            "done_count": 0,
            "total_deps": 0,
        }

    done_count = 0
    blocking_deps: list[dict[str, str]] = []
    has_upstream_blocked = False
    for dep_id in deps:
        if dep_id in done_ids:
            done_count += 1
        else:
            st = all_statuses.get(dep_id, "missing")
            blocking_deps.append({"id": dep_id, "status": st})
            if st in ("blocked", "cancelled", "error"):
                has_upstream_blocked = True

    if done_count == len(deps):
        dep_state = "ready"
    elif has_upstream_blocked:
        dep_state = "upstream_blocked"
    else:
        dep_state = "waiting"

    return {
        "dep_state": dep_state,
        "blocking_deps": blocking_deps,
        "done_count": done_count,
        "total_deps": len(deps),
    }


def detect_dependency_cycle(
    tasks_dir: Path,
    extra_edges: dict[str, list[str]] | None = None,
) -> list[str] | None:
    """检查任务依赖图是否存在环。无环返回 None，有环返回环路径列表 [a, b, ..., a]。"""
    adj: dict[str, list[str]] = {}
    if tasks_dir.is_dir():
        for f in tasks_dir.glob("*.md"):
            t = parse_task_file(f)
            if not t.error:
                adj[t.id] = list(t.depends_on)
    if extra_edges:
        for k, v in extra_edges.items():
            adj[k] = list(v)

    state: dict[str, int] = {}  # 0=未访问, 1=访问中, 2=已完成
    stack: list[str] = []

    def dfs(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for nxt in adj.get(node, ()):
            st = state.get(nxt, 0)
            if st == 1:
                idx = stack.index(nxt)
                return stack[idx:] + [nxt]
            if st == 0:
                cyc = dfs(nxt)
                if cyc:
                    return cyc
        stack.pop()
        state[node] = 2
        return None

    for nid in list(adj):
        if state.get(nid, 0) == 0:
            cyc = dfs(nid)
            if cyc:
                return cyc
    return None


def read_done_summaries(
    tasks_dir: Path,
    dep_ids: list[str],
    max_chars_per_dep: int = 1500,
) -> list[dict[str, str]]:
    """读取前置已完成任务的「## Agent 处理结果」摘要（供下游任务契约对齐）。"""
    out: list[dict[str, str]] = []
    if not dep_ids or not tasks_dir.is_dir():
        return out

    for dep_id in dep_ids:
        cand = tasks_dir / "done" / f"{dep_id}.md"
        if not cand.is_file():
            cand = tasks_dir / f"{dep_id}.md"
        if not cand.is_file():
            continue
        t = parse_task_file(cand)
        if t.error:
            continue
        body = t.description
        heading = "## Agent 处理结果"
        idx = body.find(heading)
        if idx >= 0:
            rest = body[idx:]
            nxt = re.search(r"\n## (?!Agent 处理结果)", rest)
            section = rest[:nxt.start()].strip() if nxt else rest.strip()
        else:
            section = body.strip()
        if len(section) > max_chars_per_dep:
            section = section[:max_chars_per_dep].rstrip() + "\n…（已截断）"
        out.append({
            "id": dep_id,
            "title": t.title,
            "assignee": t.assignee,
            "summary": section,
        })
    return out


__all__ = [
    "Task",
    "PRIORITY_ORDER",
    "priority_rank",
    "parse_task_file",
    "write_task_file",
    "list_tasks",
    "set_task_file_status",
    "set_task_file_assignee",
    "set_task_file_depends_on",
    "append_progress",
    "append_agent_result",
    "detect_dependency_cycle",
    "read_dependency_summaries",
]


