"""合入前风险审批：阻塞等待看板人工决定，并把请求/决定写入审计。

与 `approvals.py`（工具调用审批）分开：
- 本模块审批的是「一次合入」：绑定 task commit 与 diff digest，不是某个工具参数；
- 只有看板登录会话可响应（由 web 层按 actor 前缀拒绝 API Key）；
- 请求/决定/超时会写 `logs/audit.jsonl`，供事后追溯；pending 状态只在内存，
  进程重启后任务由现有恢复逻辑重置并经下一轮重新发起。

Phase 1 只支持单人审批（single）；多人会签需要外部身份源，见 roadmap Phase 2。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from .audit import record as audit_record
from .logging_setup import get_logger

log = get_logger("risk_approval")


@dataclass
class RiskApprovalRequest:
    id: str
    project: str
    task_id: str
    title: str
    risk_level: str
    reasons: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    branch: str = ""
    commit: str = ""
    digest: str = ""
    requested_by: str = ""
    created_at: float = field(default_factory=time.time)
    timeout_seconds: int = 3600
    status: str = "pending"          # pending / approved / rejected / timeout
    decision: str = ""               # approved / rejected
    comment: str = ""
    decided_by: str = ""
    decided_at: float = 0.0
    event: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self, now: float | None = None) -> dict:
        current = time.time() if now is None else now
        return {
            "id": self.id,
            "project": self.project,
            "task_id": self.task_id,
            "title": self.title,
            "risk_level": self.risk_level,
            "reasons": list(self.reasons),
            "files": list(self.files),
            "additions": int(self.additions),
            "deletions": int(self.deletions),
            "branch": self.branch,
            "commit": self.commit,
            "digest": self.digest,
            "requested_by": self.requested_by,
            "created_at": self.created_at,
            "timeout_seconds": int(self.timeout_seconds),
            "waited_seconds": max(0, int(current - self.created_at)),
            "status": self.status,
        }


@dataclass
class RiskApprovalOutcome:
    approved: bool
    timed_out: bool
    request: RiskApprovalRequest
    cancelled: bool = False

    @property
    def rejected(self) -> bool:
        return not self.approved and not self.timed_out and not self.cancelled


class RiskApprovalManager:
    """进程级风险审批注册表：任务线程阻塞等待，看板线程响应唤醒。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, RiskApprovalRequest] = {}

    def request(self, *, project: str, task_id: str, title: str,
                risk_level: str, reasons: list[str], files: list[str],
                additions: int, deletions: int, branch: str, commit: str,
                digest: str, timeout_seconds: int, requested_by: str,
                stop_event: threading.Event | None = None,
                ) -> RiskApprovalOutcome:
        req = RiskApprovalRequest(
            id=uuid.uuid4().hex, project=str(project or ""),
            task_id=str(task_id or ""), title=str(title or ""),
            risk_level=str(risk_level or ""), reasons=list(reasons or []),
            files=list(files or [])[:200], additions=max(0, int(additions)),
            deletions=max(0, int(deletions)), branch=str(branch or ""),
            commit=str(commit or ""), digest=str(digest or ""),
            requested_by=str(requested_by or ""),
            timeout_seconds=max(1, int(timeout_seconds)),
        )
        with self._lock:
            self._pending[req.id] = req
        self._audit("risk_approval.request", req, actor=req.requested_by,
                    result="pending")
        log.info("等待风险审批 [%s] %s · %s · %s",
                 req.project, req.task_id, req.risk_level, req.digest[:12])
        deadline = time.monotonic() + req.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with self._lock:
                    self._pending.pop(req.id, None)
                req.status = "timeout"
                self._audit("risk_approval.timeout", req, actor="system",
                            result="timeout")
                log.warning("风险审批超时 [%s] %s", req.project, req.task_id)
                return RiskApprovalOutcome(approved=False, timed_out=True, request=req)
            if req.event.wait(timeout=min(0.5, remaining)):
                break
            if stop_event is not None and stop_event.is_set():
                with self._lock:
                    self._pending.pop(req.id, None)
                req.status = "cancelled"
                self._audit("risk_approval.cancel", req, actor="system",
                            result="cancelled")
                log.info("风险审批随任务停止取消 [%s] %s", req.project, req.task_id)
                return RiskApprovalOutcome(approved=False, timed_out=False,
                                           request=req, cancelled=True)
        with self._lock:
            self._pending.pop(req.id, None)
        if req.status == "approved":
            return RiskApprovalOutcome(approved=True, timed_out=False, request=req)
        return RiskApprovalOutcome(approved=False, timed_out=False, request=req)

    def respond(self, request_id: str, approved: bool, *,
                comment: str = "", actor: str = "", ip: str = "") -> bool:
        """响应一条 pending 请求；未知/已处理返回 False。"""
        with self._lock:
            req = self._pending.get(request_id)
            if req is None or req.status != "pending":
                return False
            req.status = "approved" if approved else "rejected"
            req.decision = req.status
            req.comment = str(comment or "")[:500]
            req.decided_by = str(actor or "")
            req.decided_at = time.time()
            self._pending.pop(request_id, None)
        self._audit(f"risk_approval.{'approve' if approved else 'reject'}",
                    req, actor=actor, ip=ip,
                    result=req.status, comment=req.comment)
        log.info("风险审批%s [%s] %s（%s）",
                 "通过" if approved else "拒绝", req.project, req.task_id, actor)
        req.event.set()
        return True

    def pending(self) -> list[dict]:
        now = time.time()
        with self._lock:
            items = list(self._pending.values())
        return [r.public(now) for r in items]

    def clear(self) -> None:
        """丢弃全部挂起请求（测试/停机用）。"""
        with self._lock:
            items = list(self._pending.values())
            self._pending.clear()
        for req in items:
            req.status = "timeout"
            req.event.set()

    @staticmethod
    def _audit(action: str, req: RiskApprovalRequest, *, actor: str = "",
               ip: str = "", result: str = "ok", comment: str = "") -> None:
        try:
            audit_record(
                action,
                actor=actor or req.requested_by or "system",
                ip=ip,
                target=f"{req.project}/{req.task_id}",
                result=result,
                detail={
                    "risk_level": req.risk_level,
                    "reasons": req.reasons[:20],
                    "files": req.files[:50],
                    "additions": req.additions,
                    "deletions": req.deletions,
                    "branch": req.branch,
                    "commit": req.commit,
                    "digest": req.digest,
                    "comment": comment,
                },
            )
        except Exception:  # noqa: BLE001 — 审计是旁路，绝不能阻断审批
            log.warning("风险审批审计写入失败（可忽略）", exc_info=True)


_MANAGER: RiskApprovalManager | None = None
_MANAGER_LOCK = threading.Lock()


def get_manager() -> RiskApprovalManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = RiskApprovalManager()
        return _MANAGER
