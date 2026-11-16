"""写操作审计（V1-R5）：谁、从哪个 IP、在什么时间、对什么对象做了什么。

范围（只记「改变状态」的动作，不记录读请求）：
- 登录成功/失败、退出；
- 任务增删改、状态变更、排序、批量删除/清空、强制停止；
- 设置保存、项目增删改、提交确认、改动丢弃、Issues 拉取；
- 审批响应、技能/镜像/更新等运维动作；
- 静态 API Key 的写调用（只记密钥哈希前缀，绝不落明文）。

落盘：`<配置目录>/logs/audit.jsonl`，append-only；单文件超过 5MB 轮转，
保留最近 5 份。参数只记摘要/白名单键，不记任务正文、密码、api_key。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .logging_setup import get_logger
from .runlog import redact_text

log = get_logger("audit")

MAX_BYTES = 5 * 1024 * 1024
MAX_BACKUPS = 5
_SAFE_ACTOR_RE = re.compile(r"[^A-Za-z0-9._:@-]+")


def audit_path(config: object = None) -> Path:
    """审计文件与 orcsy.log/usage.jsonl 同目录（配置目录/logs）。"""
    src = getattr(config, "source_path", None) if config is not None else None
    if src:
        return Path(src).parent / "logs" / "audit.jsonl"
    import tempfile
    return Path(tempfile.gettempdir()) / "orcsy-audit.jsonl"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_actor(actor: str) -> str:
    raw = str(actor or "unknown").strip() or "unknown"
    return _SAFE_ACTOR_RE.sub("_", raw)[:120]


class AuditLog:
    """append-only JSONL 审计日志；写失败只告警，不影响业务。"""

    def __init__(self, path: Path | str, *, max_bytes: int = MAX_BYTES,
                 backups: int = MAX_BACKUPS) -> None:
        self.path = Path(path)
        self.max_bytes = max(64 * 1024, int(max_bytes))
        self.backups = max(1, int(backups))
        self._lock = threading.Lock()

    def record(self, action: str, *, actor: str = "unknown", ip: str = "",
               target: str = "", result: str = "ok",
               detail: Any = None) -> dict:
        entry = {
            "ts": _now_iso(),
            "action": str(action or "unknown")[:120],
            "actor": _safe_actor(actor),
            "ip": str(ip or "")[:80],
            "target": redact_text(str(target or ""), 240),
            "result": str(result or "ok")[:40],
            "detail": redact_text(
                json.dumps(detail, ensure_ascii=False, default=str)
                if detail is not None else "", 600),
        }
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed()
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("审计日志写入失败（忽略）：%s", e)
        return entry

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.stat().st_size < self.max_bytes:
                return
        except OSError:
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = self.path.with_name(f"audit-{stamp}.jsonl")
        try:
            os.replace(self.path, dest)
        except OSError:
            return
        olds = sorted(self.path.parent.glob("audit-*.jsonl"))
        for old in olds[:-self.backups]:
            try:
                old.unlink()
            except OSError:
                pass

    def tail(self, *, limit: int = 100, action: str = "",
             actor: str = "") -> list[dict]:
        limit = max(1, min(int(limit or 100), 1000))
        want_action = str(action or "").strip().lower()
        want_actor = str(actor or "").strip().lower()
        out: list[dict] = []
        files = [self.path] + sorted(
            self.path.parent.glob("audit-*.jsonl"), reverse=True)
        for path in files:
            if len(out) >= limit:
                break
            lines: list[str] = []
            try:
                with open(path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    # 只读尾部约 512KB，避免大文件全量载入
                    span = min(size, 512 * 1024)
                    f.seek(size - span)
                    lines = f.read().decode("utf-8", "replace").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(e, dict):
                    continue
                if want_action and want_action not in str(e.get("action") or "").lower():
                    continue
                if want_actor and want_actor not in str(e.get("actor") or "").lower():
                    continue
                out.append(e)
                if len(out) >= limit:
                    break
        return out


_STORE: AuditLog | None = None
_STORE_LOCK = threading.Lock()


def init_audit_log(config: object = None, *, force: bool = False) -> AuditLog:
    global _STORE
    target = audit_path(config)
    with _STORE_LOCK:
        if _STORE is not None and not force and _STORE.path == target:
            return _STORE
        _STORE = AuditLog(target)
        return _STORE


def get_audit_log() -> AuditLog:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = AuditLog(audit_path(None))
        return _STORE


def record(action: str, **kwargs: Any) -> dict:
    return get_audit_log().record(action, **kwargs)


__all__ = ["AuditLog", "audit_path", "init_audit_log", "get_audit_log", "record"]
