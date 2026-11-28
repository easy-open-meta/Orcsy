"""任务指纹与租约（V1-R2）：防止重复认领、支持断点续跑与提交前二次校验。

设计要点（见 docs/plan-v1-trusted-pipeline.md §4.4）：

- **指纹**只覆盖「用户意图」：frontmatter 里的非系统字段 + 正文（去掉执行进度 /
  处理结果 / 审查结果等系统追加节）+ 依赖任务指纹 + AGENT.md + 模型/角色。
  系统写状态、追加进度不会改变指纹；用户改任务正文/依赖/指引才会变。
- **租约**带 TTL 与心跳，存 `<state_dir>/<project>.json`；所有读改写经文件锁 +
  原子替换，进程内线程与多进程（CLI/看板）都按同一份状态互斥。
- **续跑标记**：WIP 提交/审查阶段落盘，进程被强杀后启动恢复据此把任务标回
  open + resume_run，保留 worktree 成果，避免重复执行或重复提交。
- 本模块只做状态判定；任何 I/O 失败都向上抛（由调用方决定是否降级），
  与 runlog/usage 的「旁路静默」不同——租约失效必须让调度器看见。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import yaml

from .logging_setup import get_logger

log = get_logger("claims")

SCHEMA_VERSION = 1
DEFAULT_TTL = 900.0          # 15 分钟无心跳视为过期
DEFAULT_HEARTBEAT = 30.0     # 心跳间隔

# frontmatter 中由系统维护、不参与指纹的字段
_SYSTEM_KEYS = frozenset({
    "status", "order", "requeue_seq", "review_pending", "review_round",
    "qa_round", "created_by", "resume_run", "review_no_tests", "budget",
    "error",
})

# 正文里由系统追加的节：指纹只取这些节之前的内容
_SYSTEM_HEADINGS = ("## 执行进度", "## Agent 处理结果", "## 审查结果")

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)


def claims_state_dir(config: object = None) -> Path:
    """租约/指纹状态目录：默认与 config.yaml 同级 `state/`。

    无配置（单元测试直构）时按进程隔离到临时目录，避免上一次测试进程留下的
    租约把下一次运行挡住（项目名相同、指纹不同）。
    """
    src = getattr(config, "source_path", None) if config is not None else None
    if src:
        return Path(src).parent / "state"
    return Path(tempfile.gettempdir()) / "orcsy-state" / f"pid-{os.getpid()}"


def _safe_name(name: str) -> str:
    raw = str(name or "unknown").strip() or "unknown"
    safe = _SAFE_NAME_RE.sub("_", raw)[:60].strip("._-") or "x"
    if safe != raw:
        safe += "-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]
    return safe


def _normalize(text: str) -> str:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]
    return "\n".join(lines).strip()


def _split_frontmatter(text: str) -> tuple[dict, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), text[m.end():]


def _strip_system_sections(body: str) -> str:
    cut = len(body)
    for heading in _SYSTEM_HEADINGS:
        idx = body.find(heading)
        if idx != -1:
            cut = min(cut, idx)
    return body[:cut]


@contextlib.contextmanager
def _file_lock(path: Path, timeout: float = 15.0) -> Iterator[None]:
    """跨进程文件锁：Windows msvcrt / POSIX fcntl；失败最多等 timeout。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    locked = False
    end = time.time() + timeout
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.time() >= end:
                    raise TimeoutError(f"等待租约文件锁超时：{path}")
                time.sleep(0.02)
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


class ProjectClaims:
    """一个项目一份状态文件：指纹缓存、租约与续跑标记。"""

    def __init__(self, project: object, state_dir: Path | str,
                 *, ttl: float = DEFAULT_TTL,
                 heartbeat: float = DEFAULT_HEARTBEAT) -> None:
        self.project_name = str(getattr(project, "name", "project"))
        self.project_path = Path(getattr(project, "path", "."))
        self.tasks_rel = str(getattr(project, "tasks_dir", "tasks") or "tasks")
        self.guide_rel = str(getattr(project, "guide_file", "AGENT.md") or "AGENT.md")
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / f"{_safe_name(self.project_name)}.json"
        self.lock_path = self.path.with_suffix(".lock")
        self.ttl = max(0.05, float(ttl))
        self.heartbeat_interval = max(0.1, float(heartbeat))
        self.owner = f"pid:{os.getpid()}@{socket.gethostname()}"
        self._tlock = threading.RLock()
        self._stop: dict[str, threading.Event] = {}

    # ---- 存储 ----

    def _load_unlocked(self) -> dict:
        if not self.path.is_file():
            return {"schema_version": SCHEMA_VERSION,
                    "project": self.project_name,
                    "leases": {}, "resumes": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("schema_version", SCHEMA_VERSION)
        data.setdefault("project", self.project_name)
        data.setdefault("leases", {})
        data.setdefault("resumes", {})
        return data

    def _save_unlocked(self, data: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    @contextlib.contextmanager
    def _mutate(self) -> Iterator[dict]:
        with self._tlock, _file_lock(self.lock_path):
            data = self._load_unlocked()
            yield data
            self._save_unlocked(data)

    def _read(self) -> dict:
        with self._tlock, _file_lock(self.lock_path):
            return self._load_unlocked()

    # ---- 指纹 ----

    def fingerprint(self, task: object, *, model: str = "",
                    agent_key: str = "") -> str:
        return self._fingerprint_task(task, model=model, agent_key=agent_key,
                                      depth=0, seen=set())

    def _fingerprint_task(self, task: object, *, model: str, agent_key: str,
                          depth: int, seen: set[str]) -> str:
        task_id = str(getattr(task, "id", "") or "")
        if depth > 8 or task_id in seen:
            return "cycle"
        seen.add(task_id)
        path = Path(getattr(task, "path", "") or "")
        meta: dict = {}
        body = str(getattr(task, "description", "") or "")
        if path.is_file():
            try:
                meta, raw_body = _split_frontmatter(path.read_text(encoding="utf-8"))
                body = raw_body or body
            except OSError:
                pass
        user_meta = {str(k): v for k, v in meta.items() if str(k) not in _SYSTEM_KEYS}
        body = _normalize(_strip_system_sections(body))
        deps = []
        for dep_id in sorted(str(x) for x in (getattr(task, "depends_on", None) or [])):
            deps.append([dep_id, self._dependency_fingerprint(
                dep_id, model=model, agent_key=agent_key,
                depth=depth + 1, seen=set(seen))])
        guide_hash = self._file_hash(self.project_path / self.guide_rel)
        payload = json.dumps({
            "meta": user_meta,
            "body": body,
            "deps": deps,
            "guide": guide_hash,
            "model": str(model or ""),
            "agent": str(agent_key or ""),
        }, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _dependency_fingerprint(self, dep_id: str, *, model: str, agent_key: str,
                                depth: int, seen: set[str]) -> str:
        tasks_dir = self.project_path / self.tasks_rel
        for candidate in (tasks_dir / f"{dep_id}.md",
                          tasks_dir / "done" / f"{dep_id}.md"):
            if candidate.is_file():
                class _T:
                    id = dep_id
                    path = candidate
                    description = ""
                    depends_on: list[str] = []
                return self._fingerprint_task(
                    _T(), model=model, agent_key=agent_key,
                    depth=depth, seen=seen)
        return "missing"

    @staticmethod
    def _file_hash(path: Path) -> str:
        try:
            return hashlib.sha256(_normalize(path.read_text(encoding="utf-8"))
                                  .encode("utf-8")).hexdigest()
        except OSError:
            return "missing"

    # ---- 租约 ----

    @staticmethod
    def _expired(lease: dict, now: float) -> bool:
        try:
            hb = float(lease.get("heartbeat_at") or 0)
            ttl = float(lease.get("ttl") or DEFAULT_TTL)
        except (TypeError, ValueError):
            return True
        return now - hb > ttl

    def acquire(self, task_id: str, fingerprint: str) -> str | None:
        """成功返回 claim_id；已有活跃租约（含同进程的其他 claim）返回 None。"""
        now = time.time()
        with self._mutate() as data:
            lease = data["leases"].get(str(task_id))
            if lease and not self._expired(lease, now):
                return None
            claim_id = secrets.token_hex(8)
            data["leases"][str(task_id)] = {
                "claim_id": claim_id,
                "fingerprint": str(fingerprint),
                "owner": self.owner,
                "acquired_at": now,
                "heartbeat_at": now,
                "ttl": self.ttl,
            }
            return claim_id

    def heartbeat(self, task_id: str, claim_id: str) -> bool:
        now = time.time()
        with self._mutate() as data:
            lease = data["leases"].get(str(task_id))
            if not lease or lease.get("claim_id") != claim_id:
                return False
            lease["heartbeat_at"] = now
            return True

    def release(self, task_id: str, claim_id: str | None = None) -> None:
        with self._mutate() as data:
            lease = data["leases"].get(str(task_id))
            if not lease:
                return
            if claim_id and lease.get("claim_id") != claim_id:
                return
            data["leases"].pop(str(task_id), None)
            return

    def verify(self, task_id: str, fingerprint: str,
               claim_id: str | None) -> tuple[bool, str]:
        lease = self._read()["leases"].get(str(task_id))
        if not lease:
            return False, "lease_missing"
        if claim_id and lease.get("claim_id") != claim_id:
            return False, "lease_owner_changed"
        if self._expired(lease, time.time()):
            return False, "lease_expired"
        if str(lease.get("fingerprint") or "") != str(fingerprint):
            return False, "fingerprint_changed"
        return True, ""

    def start_heartbeat(self, task_id: str, claim_id: str) -> threading.Event:
        stop = threading.Event()

        def _beat() -> None:
            while not stop.wait(self.heartbeat_interval):
                try:
                    if not self.heartbeat(task_id, claim_id):
                        return
                except Exception:  # noqa: BLE001 — 心跳失败让 TTL 自然过期
                    log.warning("租约心跳失败（任务 %s）", task_id, exc_info=True)
                    return

        self._stop[str(task_id)] = stop
        threading.Thread(target=_beat, name=f"claim-hb-{task_id}", daemon=True).start()
        return stop

    @staticmethod
    def stop_heartbeat(stop: threading.Event | None) -> None:
        if stop is not None:
            stop.set()

    def cleanup_expired(self) -> int:
        now = time.time()
        removed = 0
        with self._mutate() as data:
            for tid, lease in list(data["leases"].items()):
                if self._expired(lease, now):
                    data["leases"].pop(tid, None)
                    removed += 1
        return removed

    # ---- 续跑标记 ----

    def mark_resume(self, task_id: str, stage: str, *, wip_commit: str = "",
                    fingerprint: str = "") -> None:
        with self._mutate() as data:
            data["resumes"][str(task_id)] = {
                "stage": str(stage or ""),
                "wip_commit": str(wip_commit or ""),
                "fingerprint": str(fingerprint or ""),
                "updated_at": time.time(),
            }

    def has_resume(self, task_id: str) -> bool:
        return str(task_id) in self._read()["resumes"]

    def resume_info(self, task_id: str) -> dict:
        return dict(self._read()["resumes"].get(str(task_id)) or {})

    def clear_resume(self, task_id: str) -> None:
        with self._mutate() as data:
            data["resumes"].pop(str(task_id), None)

    def snapshot(self) -> dict:
        """诊断/测试用：当前租约与续跑标记副本。"""
        data = self._read()
        return {"leases": dict(data["leases"]), "resumes": dict(data["resumes"])}


__all__ = ["ProjectClaims", "claims_state_dir", "DEFAULT_TTL", "DEFAULT_HEARTBEAT"]
