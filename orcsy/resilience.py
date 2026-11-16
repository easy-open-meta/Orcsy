"""熔断停机（V1-R4）：连续失败 / 窗口失败率触发项目级暂停。

设计（见 docs/design-circuit-breaker.md）：

- **失败口径**：模型/网络错误、普通 blocked、审查 fail/error 计为失败；
  用户主动停止（cancelled）、预算超限（budget_exceeded）、任务被改写
  （stale_lease）属于受控停止，不计入熔断；done / 重新入队视为成功并重置
  连续失败计数。
- **触发**：连续失败达到阈值，或统计窗口内失败率达到阈值且样本足够；
  触发后项目暂停领取新任务，正在跑的任务允许收尾。
- **恢复**：默认人工在看板点「恢复」；`auto_resume=true` 时窗口结束后自动
  恢复一次（过时失败记录清空）。暂停状态持久化，重启后仍有效。
- 状态文件 `<配置目录>/state/resilience.json`，与租约/预算同目录。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .claims import _file_lock, claims_state_dir
from .config import ResilienceConfig
from .logging_setup import get_logger

log = get_logger("resilience")

SCHEMA_VERSION = 1
STATE_FILENAME = "resilience.json"
_MAX_EVENTS = 200
_EVENT_TTL = 7 * 86400

# 受控停止：不视为系统性故障，不参与熔断
_CONTROLLED_REASONS = frozenset({"budget_exceeded", "stale_lease"})


def _now() -> float:
    return time.time()


def _is_failure(outcome: str, block_reason: str) -> bool:
    outcome = str(outcome or "")
    if outcome == "done":
        return False
    if outcome in ("cancelled", "open"):
        return False
    if outcome == "blocked":
        return str(block_reason or "") not in _CONTROLLED_REASONS
    # 其他未知结果按失败处理（宁可熔断也不静默跑一夜）
    return True


class ResilienceStore:
    """项目健康事件与暂停状态（单实例跨项目共享一个文件）。"""

    def __init__(self, state_dir: Path | str,
                 config: ResilienceConfig | None = None) -> None:
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / STATE_FILENAME
        self.lock_path = self.path.with_suffix(".lock")
        self.cfg = config or get_resilience_config()
        self._tlock = threading.RLock()

    # ---- 存储 ----

    def _load_unlocked(self) -> dict:
        if not self.path.is_file():
            return {"schema_version": SCHEMA_VERSION, "projects": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("schema_version", SCHEMA_VERSION)
        projects = data.get("projects")
        data["projects"] = projects if isinstance(projects, dict) else {}
        return data

    def _save_unlocked(self, data: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def _mutate(self):
        class _Ctx:
            def __enter__(inner):
                self._tlock.acquire()
                inner._file = _file_lock(self.lock_path)
                inner._file.__enter__()
                inner.data = self._load_unlocked()
                return inner.data

            def __exit__(inner, exc_type, exc, tb):
                try:
                    if exc_type is None:
                        self._save_unlocked(inner.data)
                finally:
                    inner._file.__exit__(exc_type, exc, tb)
                    self._tlock.release()
            # 保持 with 语法可用

        return _Ctx()

    def _read(self) -> dict:
        with self._tlock, _file_lock(self.lock_path):
            return self._load_unlocked()

    # ---- 事件与状态 ----

    def _project(self, data: dict, project: str) -> dict:
        rec = data["projects"].setdefault(str(project), {})
        rec.setdefault("events", [])
        rec.setdefault("paused", False)
        rec.setdefault("pause_reason", "")
        rec.setdefault("paused_at", 0.0)
        rec.setdefault("paused_by", "")
        rec.setdefault("consecutive_failures", 0)
        return rec

    def _stats(self, rec: dict, now: float | None = None) -> dict:
        now = now or _now()
        window_start = now - self.cfg.window_minutes * 60
        events = [e for e in rec.get("events") or [] if isinstance(e, dict)]
        relevant = [e for e in events if e.get("kind") in ("ok", "fail")]
        window = [e for e in relevant if float(e.get("ts") or 0) >= window_start]
        window_failures = sum(1 for e in window if e.get("kind") == "fail")
        rate = (window_failures / len(window)) if window else 0.0
        consec = 0
        for e in reversed(events):
            kind = e.get("kind")
            if kind == "fail":
                consec += 1
            elif kind == "ok":
                break
        return {
            "consecutive_failures": consec,
            "window_samples": len(window),
            "window_failures": window_failures,
            "failure_rate": round(rate, 4),
            "last_failure_at": max((float(e.get("ts") or 0)
                                    for e in events if e.get("kind") == "fail"),
                                   default=0.0),
        }

    @staticmethod
    def _trim(events: list[dict], now: float) -> list[dict]:
        events = [e for e in events
                  if isinstance(e, dict) and now - float(e.get("ts") or 0) <= _EVENT_TTL]
        return events[-_MAX_EVENTS:]

    def _auto_resume(self, rec: dict, now: float) -> bool:
        if not rec.get("paused") or not self.cfg.auto_resume:
            return False
        paused_at = float(rec.get("paused_at") or 0)
        if paused_at and now - paused_at >= self.cfg.window_minutes * 60:
            rec["paused"] = False
            rec["pause_reason"] = ""
            rec["paused_at"] = 0.0
            rec["paused_by"] = ""
            rec["events"] = []          # 重新开始计数，避免立即再次触发
            return True
        return False

    def record(self, project: str, task_id: str, *, outcome: str,
               block_reason: str = "", review_verdict: str = "",
               detail: str = "") -> dict:
        now = _now()
        failure = _is_failure(outcome, block_reason)
        neutral = str(outcome or "") in ("cancelled", "open")
        kind = "neutral" if neutral else ("fail" if failure else "ok")
        with self._mutate() as data:
            rec = self._project(data, project)
            self._auto_resume(rec, now)
            rec["events"].append({
                "ts": now, "kind": kind, "task": str(task_id),
                "outcome": str(outcome or ""),
                "reason": str(block_reason or ""),
                "review": str(review_verdict or ""),
                "detail": str(detail or "")[:200],
            })
            rec["events"] = self._trim(rec["events"], now)
            stats = self._stats(rec, now)
            rec["consecutive_failures"] = stats["consecutive_failures"]
            if self.cfg.enabled and not rec.get("paused"):
                reason = ""
                if (self.cfg.max_consecutive_failures > 0
                        and stats["consecutive_failures"]
                        >= self.cfg.max_consecutive_failures):
                    reason = (f"连续 {stats['consecutive_failures']} 次失败"
                              f"（阈值 {self.cfg.max_consecutive_failures}）")
                elif (self.cfg.max_failure_rate > 0
                      and stats["window_samples"] >= self.cfg.min_samples
                      and stats["failure_rate"] >= self.cfg.max_failure_rate):
                    reason = (f"近 {self.cfg.window_minutes} 分钟失败率 "
                              f"{stats['failure_rate'] * 100:.0f}%"
                              f"（{stats['window_failures']}/{stats['window_samples']}，"
                              f"阈值 {self.cfg.max_failure_rate * 100:.0f}%）")
                if reason:
                    rec.update({"paused": True, "pause_reason": reason,
                                "paused_at": now, "paused_by": "auto"})
                    log.warning("项目 %s 触发熔断：%s", project, reason)
            return self.status(project, _data=data, now=now)

    def status(self, project: str, *, _data: dict | None = None,
               now: float | None = None) -> dict:
        current = now or _now()
        if _data is not None:
            rec = self._project(_data, project)
            self._auto_resume(rec, current)
            return self._status_from(rec, current)
        if self.cfg.auto_resume:
            # 自动恢复需要写回暂停状态
            with self._mutate() as data:
                rec = self._project(data, project)
                self._auto_resume(rec, current)
                return self._status_from(rec, current)
        rec = self._project(self._read(), project)
        return self._status_from(rec, current)

    def _status_from(self, rec: dict, now: float) -> dict:
        stats = self._stats(rec, now)
        return {
            "paused": bool(rec.get("paused")),
            "pause_reason": str(rec.get("pause_reason") or ""),
            "paused_at": float(rec.get("paused_at") or 0),
            "paused_by": str(rec.get("paused_by") or ""),
            **stats,
            "limits": {
                "max_consecutive_failures": self.cfg.max_consecutive_failures,
                "window_minutes": self.cfg.window_minutes,
                "max_failure_rate": self.cfg.max_failure_rate,
                "min_samples": self.cfg.min_samples,
                "auto_resume": self.cfg.auto_resume,
                "enabled": self.cfg.enabled,
            },
        }

    def pause(self, project: str, reason: str, *, by: str = "manual") -> dict:
        now = _now()
        with self._mutate() as data:
            rec = self._project(data, project)
            rec.update({"paused": True, "pause_reason": str(reason),
                        "paused_at": now, "paused_by": by})
            return self._status_from(rec, now)

    def resume(self, project: str, *, by: str = "manual") -> dict:
        now = _now()
        with self._mutate() as data:
            rec = self._project(data, project)
            rec.update({"paused": False, "pause_reason": "",
                        "paused_at": 0.0, "paused_by": by,
                        "consecutive_failures": 0,
                        # 人工恢复视为重新开始：清空旧失败史，避免下一次立即再熔断
                        "events": []})
            return self._status_from(rec, now)

    def is_paused(self, project: str) -> tuple[bool, str]:
        st = self.status(project)
        return bool(st.get("paused")), str(st.get("pause_reason") or "")

    def snapshot(self) -> dict[str, dict]:
        now = _now()
        if self.cfg.auto_resume:
            with self._mutate() as data:
                out: dict[str, dict] = {}
                for name in list(data["projects"].keys()):
                    rec = self._project(data, name)
                    self._auto_resume(rec, now)
                    out[name] = self._status_from(rec, now)
                return out
        data = self._read()
        out = {}
        for name in list(data["projects"].keys()):
            out[name] = self._status_from(self._project(data, name), now)
        return out


# ---- 模块级配置与单例 ----

_CONFIG = ResilienceConfig()
_STORE: ResilienceStore | None = None
_STORE_LOCK = threading.Lock()


def configure(cfg: ResilienceConfig | None) -> None:
    global _CONFIG
    _CONFIG = cfg or ResilienceConfig()


def get_resilience_config() -> ResilienceConfig:
    return _CONFIG


def init_resilience_store(config: object = None, *,
                          state_dir: Path | str | None = None,
                          force: bool = False) -> ResilienceStore:
    global _STORE
    target = Path(state_dir) if state_dir else claims_state_dir(config)
    cfg = getattr(config, "resilience", None) or _CONFIG
    with _STORE_LOCK:
        if _STORE is not None and not force and _STORE.state_dir == target:
            _STORE.cfg = cfg
            return _STORE
        _STORE = ResilienceStore(target, cfg)
        return _STORE


def get_resilience_store() -> ResilienceStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = ResilienceStore(claims_state_dir(None), _CONFIG)
        return _STORE


def project_pause_status(project: str) -> dict:
    """扫描前判断项目是否处于熔断暂停（含 auto_resume 检查）。"""
    try:
        return get_resilience_store().status(project)
    except Exception:  # noqa: BLE001 — 状态读取失败不阻塞调度
        log.warning("读取项目熔断状态失败（忽略）", exc_info=True)
        return {"paused": False, "pause_reason": ""}


def record_task_result(project: str, task_id: str, result: object) -> dict:
    review = getattr(result, "review", None)
    return get_resilience_store().record(
        project, task_id,
        outcome=str(getattr(result, "outcome", "") or ""),
        block_reason=str(getattr(result, "block_reason", "") or ""),
        review_verdict=str(getattr(review, "verdict", "") or ""),
        detail=str(getattr(result, "notes", "") or ""),
    )


__all__ = [
    "ResilienceStore", "configure", "get_resilience_config",
    "init_resilience_store", "get_resilience_store",
    "project_pause_status", "record_task_result",
]
