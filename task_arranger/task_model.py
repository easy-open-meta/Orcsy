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

    tasks.sort(key=lambda t: (t.rank, t.id))
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


def set_task_file_status(path: Path, status: str) -> bool:
    """改写任务文件 frontmatter 的 status 字段（无 frontmatter 时补一个）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    m = _FRONTMATTER_SPLIT_RE.match(text)
    if not m:
        text = f"---\nstatus: {status}\n---\n\n{text}"
    elif re.search(r"^status:.*$", m.group(0), re.MULTILINE):
        front = re.sub(r"^status:.*$", f"status: {status}", m.group(0), flags=re.MULTILINE)
        text = front + text[m.end():]
    else:
        text = m.group(0).rstrip("\n") + f"\nstatus: {status}\n---\n" + text[m.end():]
    try:
        path.write_text(text, encoding="utf-8")
        return True
    except OSError:
        return False
