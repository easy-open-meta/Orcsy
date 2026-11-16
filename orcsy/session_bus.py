"""需求批次临时共享剪贴板（Phase 1）：跨角色传递运行态信息。

**批次定义**：沿任务 `parent_id` 向上追到根任务；同一根需求下的 PM 子任务、
开发任务、QA 回归与 Bug 修复任务共享同一个批次。没有 `parent_id` 的单兵任务
以自身 ID 为批次（只在该任务生命周期内可见）。

**典型用途**：后端开发把临时启动端口、测试账号、动态 JWT、临时 DSN 写入，
QA 联调时直接读取，不必用日志正则猜。**只放非代码运行态**，不要放长期密钥。

**生命周期**：需求验收归档或人工终止、且批次内已无 open/in_progress/blocked
任务时，文件立即物理删除；进程异常残留由 7 天 TTL 兜底清理。数据落在
`<配置目录>/state/session_bus/`，不写任务文件、不进 Git。

并发：单进程内按文件加线程锁 + 临时文件原子替换；Orcsy 设计为单实例，
跨进程并发不在本模块承诺范围。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from .logging_setup import get_logger
from .task_model import parse_task_file

log = get_logger("session_bus")

SCHEMA_VERSION = 1
MAX_KEY_CHARS = 64
MAX_VALUE_CHARS = 8000
MAX_ENTRIES = 50
EXPIRE_SECONDS = 7 * 24 * 3600

_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SAFE_RE = re.compile(r"[^\w.-]+")
_BUS_DIR = "session_bus"

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _safe_name(name: str) -> str:
    raw = str(name or "unknown").strip() or "unknown"
    safe = _SAFE_RE.sub("_", raw)[:60].strip("._-") or "x"
    if safe != raw:
        import hashlib
        safe += "-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]
    return safe


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve()).lower()
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def _find_task_file(tasks_dir: Path, task_id: str) -> Path | None:
    for cand in (tasks_dir / f"{task_id}.md", tasks_dir / "done" / f"{task_id}.md"):
        if cand.is_file():
            return cand
    return None


def batch_id_for(task: object, tasks_dir: Path) -> str:
    """沿 parent_id 追到根任务 ID；父任务缺失时用父 ID 作为稳定批次键。"""
    current_id = str(getattr(task, "id", "") or "task")
    current = task
    seen: set[str] = set()
    while True:
        parent_id = str(getattr(current, "parent_id", "") or "").strip()
        if not parent_id or parent_id in seen:
            return current_id
        seen.add(parent_id)
        parent_path = _find_task_file(tasks_dir, parent_id)
        if parent_path is None:
            return parent_id
        try:
            parent = parse_task_file(parent_path)
        except Exception:  # noqa: BLE001 — 解析失败按父 ID 作为批次键
            return parent_id
        if parent.error:
            return parent_id
        current_id = parent_id
        current = parent


def bus_dir(state_dir: Path | str) -> Path:
    return Path(state_dir) / _BUS_DIR


def bus_path(state_dir: Path | str, project_name: str, batch_id: str) -> Path:
    return bus_dir(state_dir) / f"{_safe_name(project_name)}__{_safe_name(batch_id)}.json"


class SessionBus:
    """一个任务所属批次的可读写信箱。"""

    def __init__(self, project: object, state_dir: Path | str, task: object,
                 *, tasks_dir: Path | str | None = None,
                 actor: str = "") -> None:
        self.project_name = str(getattr(project, "name", "project"))
        self.state_dir = Path(state_dir)
        self.tasks_dir = Path(
            tasks_dir
            or (Path(getattr(project, "path", "."))
                / str(getattr(project, "tasks_dir", "tasks") or "tasks")))
        self.batch_id = batch_id_for(task, self.tasks_dir)
        self.task_id = str(getattr(task, "id", "") or "")
        self.actor = str(actor or "agent")[:80]
        self.path = bus_path(self.state_dir, self.project_name, self.batch_id)
        self._lock = _lock_for(self.path)

    # ---- 读写 ----

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        entries = data.get("entries")
        if not isinstance(entries, dict):
            entries = {}
        return {
            "schema_version": SCHEMA_VERSION,
            "project": self.project_name,
            "batch": self.batch_id,
            "created_at": str(data.get("created_at") or _now()),
            "updated_at": str(data.get("updated_at") or _now()),
            "entries": entries,
        }

    def _save(self, data: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:  # 仅 POSIX 生效：状态目录只给当前用户
            os.chmod(self.state_dir, 0o700)
        except OSError:
            pass
        data["updated_at"] = _now()
        tmp = self.path.with_name(
            self.path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def share(self, key: str, value: str) -> dict:
        """写入/覆盖一个键；返回 {key, count}（不回显 value）。"""
        key = str(key or "").strip()
        if not _KEY_RE.fullmatch(key):
            raise ValueError(
                f"key 不合法：{key!r}（1~{MAX_KEY_CHARS} 位字母/数字/下划线/点/连字符）")
        text = str(value if value is not None else "")
        if not text.strip():
            raise ValueError("value 不能为空（临时状态请写有意义的字符串）")
        if len(text) > MAX_VALUE_CHARS:
            raise ValueError(f"value 过长（{len(text)} > {MAX_VALUE_CHARS} 字符）")
        with self._lock:
            data = self._load()
            entries = data["entries"]
            is_new = key not in entries
            if is_new and len(entries) >= MAX_ENTRIES:
                raise ValueError(
                    f"批次共享条目已达上限 {MAX_ENTRIES} 个；请复用已有 key 或等待批次结束清理")
            entries[key] = {
                "value": text,
                "updated_at": _now(),
                "updated_by": self.actor,
                "task_id": self.task_id,
            }
            self._save(data)
            return {"key": key, "count": len(entries)}

    def read(self, key: str) -> str | None:
        key = str(key or "").strip()
        if not _KEY_RE.fullmatch(key):
            raise ValueError(
                f"key 不合法：{key!r}（1~{MAX_KEY_CHARS} 位字母/数字/下划线/点/连字符）")
        with self._lock:
            entry = self._load()["entries"].get(key)
        if not isinstance(entry, dict):
            return None
        return str(entry.get("value") or "")

    def keys(self) -> list[dict]:
        """列出可读键（不含 value，避免把敏感值灌回上下文）。"""
        with self._lock:
            entries = self._load()["entries"]
        return [
            {
                "key": str(k),
                "updated_at": str(v.get("updated_at") or ""),
                "updated_by": str(v.get("updated_by") or ""),
                "task_id": str(v.get("task_id") or ""),
                "chars": len(str(v.get("value") or "")),
            }
            for k, v in sorted(entries.items())
            if isinstance(v, dict)
        ]

    def cleanup(self) -> bool:
        with self._lock:
            try:
                self.path.unlink()
                return True
            except FileNotFoundError:
                return False
            except OSError as e:
                log.warning("清理批次共享剪贴板失败（忽略）：%s", e)
                return False


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def cleanup_if_finished(project: object, state_dir: Path | str, task: object,
                        *, tasks_dir: Path | str | None = None) -> bool:
    """批次内已无 open/in_progress/blocked 任务时删除剪贴板。

    在任务 done/cancelled 归档或人工改状态后调用；blocked 保留（可能重新入队）。
    返回是否实际删除。任何异常都吞掉——清理是旁路，绝不能影响任务回写。
    """
    try:
        bus = SessionBus(project, state_dir, task, tasks_dir=tasks_dir)
        tdir = bus.tasks_dir
        if not tdir.is_dir():
            return bus.cleanup()
        paths = list(tdir.glob("*.md")) + list((tdir / "done").glob("*.md"))
        for path in paths:
            try:
                t = parse_task_file(path)
            except Exception:  # noqa: BLE001
                continue
            if t.error:
                continue
            if batch_id_for(t, tdir) != bus.batch_id:
                continue
            if t.status in ("open", "in_progress", "blocked"):
                return False
        return bus.cleanup()
    except Exception:  # noqa: BLE001 — 清理旁路
        log.warning("按批次清理共享剪贴板失败（忽略）", exc_info=True)
        return False


def prune_expired(state_dir: Path | str, *, max_age_seconds: float = EXPIRE_SECONDS) -> int:
    """清理超过 TTL 的批次文件（进程被杀导致未能按任务生命周期回收的兜底）。"""
    removed = 0
    now = time.time()
    directory = bus_dir(state_dir)
    if not directory.is_dir():
        return 0
    for path in directory.glob("*.json"):
        try:
            if now - path.stat().st_mtime <= max_age_seconds:
                continue
            path.unlink()
            removed += 1
        except OSError:
            continue
    if removed:
        log.info("清理过期批次共享剪贴板 %d 个", removed)
    return removed


__all__ = [
    "MAX_ENTRIES",
    "MAX_KEY_CHARS",
    "MAX_VALUE_CHARS",
    "SessionBus",
    "batch_id_for",
    "bus_dir",
    "bus_path",
    "cleanup_if_finished",
    "prune_expired",
]
