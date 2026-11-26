"""Phase 2A：外部 Provider 的持久化 quorum 审批引擎。

只负责编排与校验：
- 创建/复用请求，持久化到 `<state_dir>/risk_approvals.json`；
- 轮询 Provider 或接收统一 HMAC 回调；
- 用 email 白名单 + 不同 email 去重计数，任何白名单内拒绝一票否决；
- digest 变化、超时、Provider 不可达、回调非法 → fail closed。

真实 Provider 适配器在 `approval_providers.py` 注册；测试用 `fake`。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .approval_providers import (ApprovalProvider, ApproverDecision,
                                 ProviderDecision, ProviderError,
                                 ProviderHandle, build_provider,
                                 resolve_email)
from .audit import record as audit_record
from .config import ApprovalProviderConfig
from .logging_setup import get_logger

log = get_logger("quorum_approval")

TERMINAL_STATUS = frozenset({
    "approved", "rejected", "timeout", "cancelled", "provider_error", "expired",
})
PROVIDER_RETRY_LIMIT = 3
STORE_VERSION = 1
_SAVE_LOCK = threading.Lock()


@dataclass
class QuorumApprovalRequest:
    id: str
    project: str
    task_id: str
    title: str
    risk_level: str
    reasons: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    provider_name: str = ""
    provider_type: str = ""
    required: int = 2
    allowed_approvers: list[str] = field(default_factory=list)
    approver_map: dict[str, dict[str, str]] = field(default_factory=dict)
    digest: str = ""
    branch: str = ""
    commit: str = ""
    requested_by: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    status: str = "pending"
    approvers: dict[str, dict] = field(default_factory=dict)
    external_id: str = ""
    external_url: str = ""
    comment: str = ""

    @property
    def approved_count(self) -> int:
        return sum(1 for a in self.approvers.values()
                   if str(a.get("decision")) == "approved")

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
            "provider": self.provider_name,
            "provider_type": self.provider_type,
            "required": int(self.required),
            "approved_count": self.approved_count,
            "allowed_approvers": list(self.allowed_approvers),
            "approvers": [
                {"email": email, **dict(info)}
                for email, info in self.approvers.items()
            ],
            "digest": self.digest,
            "branch": self.branch,
            "commit": self.commit,
            "requested_by": self.requested_by,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "waited_seconds": max(0, int(current - self.created_at)),
            "status": self.status,
            "external_id": self.external_id,
            "external_url": self.external_url,
            "comment": self.comment,
        }

    def to_store(self) -> dict:
        data = self.public()
        # public() 给 UI 的是 list；落盘用 email→决定 的 dict，供 from_store 还原
        data["approvers"] = {
            email: dict(info) for email, info in self.approvers.items()
        }
        data["approver_map"] = {
            email: dict(ids) for email, ids in self.approver_map.items()
        }
        return data

    @classmethod
    def from_store(cls, data: dict) -> "QuorumApprovalRequest":
        return cls(
            id=str(data.get("id") or ""),
            project=str(data.get("project") or ""),
            task_id=str(data.get("task_id") or ""),
            title=str(data.get("title") or ""),
            risk_level=str(data.get("risk_level") or ""),
            reasons=list(data.get("reasons") or []),
            files=list(data.get("files") or []),
            additions=int(data.get("additions") or 0),
            deletions=int(data.get("deletions") or 0),
            provider_name=str(data.get("provider") or ""),
            provider_type=str(data.get("provider_type") or ""),
            required=max(1, int(data.get("required") or 1)),
            allowed_approvers=list(data.get("allowed_approvers") or []),
            approver_map=dict(data.get("approver_map") or {}),
            digest=str(data.get("digest") or ""),
            branch=str(data.get("branch") or ""),
            commit=str(data.get("commit") or ""),
            requested_by=str(data.get("requested_by") or ""),
            created_at=float(data.get("created_at") or 0.0),
            expires_at=float(data.get("expires_at") or 0.0),
            status=str(data.get("status") or "pending"),
            approvers=dict(data.get("approvers") or {}),
            external_id=str(data.get("external_id") or ""),
            external_url=str(data.get("external_url") or ""),
            comment=str(data.get("comment") or ""),
        )


@dataclass
class QuorumApprovalOutcome:
    approved: bool
    timed_out: bool
    cancelled: bool
    request: QuorumApprovalRequest
    error: str = ""


class QuorumApprovalManager:
    """单实例、文件持久化的 quorum 审批引擎。"""

    def __init__(self, state_dir: Path,
                 provider_factory=None):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.state_dir / "risk_approvals.json"
        self._lock = threading.RLock()
        self._requests: dict[str, QuorumApprovalRequest] = {}
        self._events: dict[str, threading.Event] = {}
        self._provider_factory = provider_factory or build_provider
        self._load()

    # ---- 持久化 ----

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        now = time.time()
        for item in raw.get("requests") or []:
            try:
                req = QuorumApprovalRequest.from_store(item)
            except (TypeError, ValueError):
                continue
            if not req.id:
                continue
            if req.status == "pending" and req.expires_at and req.expires_at <= now:
                req.status = "expired"
            self._requests[req.id] = req

    def _save(self) -> None:
        with self._lock:
            now = time.time()
            # 只保留最近 500 条；终态保留 7 天供审计
            items = sorted(self._requests.values(),
                           key=lambda r: r.created_at, reverse=True)
            keep: list[dict] = []
            for req in items:
                if req.status in TERMINAL_STATUS and now - req.created_at > 7 * 86400:
                    continue
                keep.append(req.to_store())
                if len(keep) >= 500:
                    break
        tmp = ""
        try:
            with _SAVE_LOCK:
                fd, tmp = tempfile.mkstemp(
                    prefix="risk-approvals-", suffix=".json",
                    dir=str(self.state_dir))
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"version": STORE_VERSION, "requests": keep},
                              f, ensure_ascii=False, indent=2)
                os.replace(tmp, self._path)
                tmp = ""
        except OSError as exc:
            log.warning("风险审批状态写入失败（可忽略）：%s", exc)
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # ---- 事件 ----

    def _event(self, request_id: str) -> threading.Event:
        with self._lock:
            ev = self._events.get(request_id)
            if ev is None:
                ev = threading.Event()
                self._events[request_id] = ev
            return ev

    def _audit(self, action: str, req: QuorumApprovalRequest,
               *, actor: str = "", result: str = "ok", detail: dict | None = None):
        try:
            audit_record(
                action, actor=actor or req.requested_by or "system",
                target=f"{req.project}/{req.task_id}", result=result,
                detail={
                    "provider": req.provider_name,
                    "risk_level": req.risk_level,
                    "required": req.required,
                    "approved_count": req.approved_count,
                    "digest": req.digest,
                    **(detail or {}),
                })
        except Exception:  # noqa: BLE001 — 审计旁路
            log.warning("风险审批审计写入失败（可忽略）", exc_info=True)

    # ---- 请求 ----

    def request(self, *, project: str, task_id: str, title: str,
                risk_level: str, reasons: list[str], files: list[str],
                additions: int, deletions: int, branch: str, commit: str,
                digest: str, provider_name: str,
                provider_cfg: ApprovalProviderConfig,
                approver_map: dict[str, dict[str, str]],
                allowed_approvers: list[str], required: int,
                requested_by: str, timeout_seconds: int,
                poll_interval_seconds: int = 20,
                callback_url: str = "",
                stop_event: threading.Event | None = None,
                ) -> QuorumApprovalOutcome:
        now = time.time()
        allowed = [str(e).strip().lower() for e in allowed_approvers]
        with self._lock:
            existing: QuorumApprovalRequest | None = None
            for req in self._requests.values():
                if (req.project == project and req.task_id == task_id
                        and req.digest == digest and req.status == "pending"
                        and (req.expires_at == 0 or req.expires_at > now)):
                    existing = req
                    break
                if (req.project == project and req.task_id == task_id
                        and req.digest == digest and req.status == "approved"
                        and (req.expires_at == 0 or req.expires_at > now)):
                    return QuorumApprovalOutcome(
                        approved=True, timed_out=False, cancelled=False,
                        request=req)
            if existing is None:
                req = QuorumApprovalRequest(
                    id=uuid.uuid4().hex, project=project, task_id=task_id,
                    title=title, risk_level=risk_level, reasons=list(reasons),
                    files=list(files)[:200], additions=max(0, int(additions)),
                    deletions=max(0, int(deletions)),
                    provider_name=provider_name,
                    provider_type=provider_cfg.type, required=max(2, int(required)),
                    allowed_approvers=allowed,
                    approver_map={e: dict(v) for e, v in approver_map.items()},
                    digest=digest, branch=branch, commit=commit,
                    requested_by=requested_by, created_at=now,
                    expires_at=now + max(1, int(timeout_seconds)),
                    status="pending")
                self._requests[req.id] = req
            else:
                req = existing
        event = self._event(req.id)
        self._audit("risk_approval.request", req, result="pending",
                    detail={"external_id": req.external_id})

        provider: ApprovalProvider | None = None
        handle: ProviderHandle | None = None
        if not req.external_id:
            try:
                provider = self._provider_factory(provider_name, provider_cfg)
                handle = provider.create({
                    "schema": "orcsy.approval.request.v1",
                    "request_id": req.id,
                    "project": req.project,
                    "task_id": req.task_id,
                    "title": req.title,
                    "risk": {"level": req.risk_level,
                             "reasons": req.reasons,
                             "files": req.files,
                             "additions": req.additions,
                             "deletions": req.deletions},
                    "commit": req.commit,
                    "digest": req.digest,
                    "required_approvals": req.required,
                    "allowed_approvers": req.allowed_approvers,
                    "requested_by": req.requested_by,
                    "callback_url": (callback_url.replace(
                        "{provider}", req.provider_type)
                        if callback_url else ""),
                })
                req.external_id = handle.external_id
                req.external_url = handle.url
                self._save()
            except Exception as exc:  # noqa: BLE001 — Provider 故障 fail closed
                self._finish(req, "provider_error", str(exc))
                return QuorumApprovalOutcome(
                    approved=False, timed_out=False, cancelled=False,
                    request=req, error=str(exc))
        else:
            try:
                provider = self._provider_factory(provider_name, provider_cfg)
                handle = ProviderHandle(external_id=req.external_id,
                                        url=req.external_url)
            except Exception as exc:  # noqa: BLE001 — Provider 故障 fail closed
                self._finish(req, "provider_error", str(exc))
                return QuorumApprovalOutcome(
                    approved=False, timed_out=False, cancelled=False,
                    request=req, error=str(exc))

        poll_interval = max(5, int(poll_interval_seconds))
        errors = 0
        last_poll = 0.0
        while True:
            with self._lock:
                if req.status in TERMINAL_STATUS:
                    return QuorumApprovalOutcome(
                        approved=req.status == "approved",
                        timed_out=req.status == "timeout",
                        cancelled=req.status == "cancelled",
                        request=req,
                        error="" if req.status in ("approved", "rejected") else req.comment)
            remaining = req.expires_at - time.time()
            if remaining <= 0:
                self._finish(req, "timeout", "审批等待超时")
                return QuorumApprovalOutcome(
                    approved=False, timed_out=True, cancelled=False, request=req,
                    error="审批等待超时")
            if stop_event is not None and stop_event.is_set():
                self._finish(req, "cancelled", "任务已停止")
                return QuorumApprovalOutcome(
                    approved=False, timed_out=False, cancelled=True, request=req)
            event.wait(timeout=min(poll_interval, max(0.2, remaining)))
            with self._lock:
                if req.status in TERMINAL_STATUS:
                    continue
            now = time.time()
            if now - last_poll < poll_interval:
                continue
            last_poll = now
            try:
                decision = provider.poll(handle) if provider and handle else None
                if decision is not None:
                    # 轮询结果天然属于当前 handle；补上 digest/request_id 供校验
                    if not decision.digest:
                        decision.digest = req.digest
                    if not decision.request_id:
                        decision.request_id = req.id
                    self.apply_decision(req.id, decision)
                    errors = 0
            except Exception as exc:  # noqa: BLE001 — Provider 故障 fail closed
                errors += 1
                self._audit("risk_approval.provider_error", req,
                            result="error", detail={"error": str(exc)[:300]})
                if errors >= PROVIDER_RETRY_LIMIT:
                    self._finish(req, "provider_error", str(exc))
                    return QuorumApprovalOutcome(
                        approved=False, timed_out=False, cancelled=False,
                        request=req, error=str(exc))

    def _finish(self, req: QuorumApprovalRequest, status: str,
                comment: str = "") -> None:
        with self._lock:
            req.status = status
            req.comment = str(comment or "")[:500]
            self._requests[req.id] = req
            event = self._events.get(req.id)
        self._save()
        self._audit(
            "risk_approval.timeout" if status == "timeout"
            else "risk_approval.cancel" if status == "cancelled"
            else "risk_approval.provider_error",
            req, result=status, detail={"comment": req.comment})
        if event is not None:
            event.set()

    # ---- 回调/决定 ----

    def apply_callback(self, provider_name: str,
                       provider_cfg: ApprovalProviderConfig,
                       headers: dict[str, str], raw_body: bytes) -> str:
        provider = self._provider_factory(provider_name, provider_cfg)
        decision = provider.verify_callback(headers, raw_body)
        target = decision.request_id
        if not target and decision.external_id:
            with self._lock:
                for req in self._requests.values():
                    if req.external_id == decision.external_id:
                        target = req.id
                        break
        if not target:
            raise ProviderError("回调缺少 request_id/external_id")
        with self._lock:
            req = self._requests.get(target)
        if req is not None:
            # 平台 Webhook 天然关联该 issue/MR；digest 以 Orcsy 存档为准
            if not decision.digest:
                decision.digest = req.digest
            if not decision.request_id:
                decision.request_id = req.id
        return self.apply_decision(target, decision)

    def apply_decision(self, request_id: str,
                       decision: ProviderDecision) -> str:
        with self._lock:
            req = self._requests.get(request_id)
            if req is None:
                raise ProviderError("审批请求不存在")
            if req.status != "pending":
                if decision.digest and decision.digest != req.digest:
                    raise ProviderError("审批请求已终结且 digest 不一致")
                return "duplicate"
            if not decision.digest:
                raise ProviderError("回调缺少 digest")
            if decision.digest != req.digest:
                raise ProviderError("回调 digest 与当前请求不一致")
            if decision.external_id and req.external_id \
                    and decision.external_id != req.external_id:
                raise ProviderError("回调 external_id 不匹配")
            allowed = set(req.allowed_approvers)
            rejected = False
            for item in decision.approvers:
                if not isinstance(item, ApproverDecision):
                    continue
                ptype = item.provider or req.provider_type
                if ptype != req.provider_type:
                    self._audit("risk_approval.invalid", req, result="ignored",
                                detail={"reason": "provider 不匹配",
                                        "approver": item.id})
                    continue
                email = resolve_email(req.approver_map, ptype, item.id)
                if not email or email not in allowed:
                    self._audit("risk_approval.invalid", req, result="ignored",
                                detail={"reason": "email 未在白名单",
                                        "approver": item.id})
                    continue
                if item.id.startswith("agent:") or item.id.startswith("service:"):
                    self._audit("risk_approval.invalid", req, result="ignored",
                                detail={"reason": "agent/service 不可审批",
                                        "approver": item.id})
                    continue
                if item.id == req.requested_by:
                    self._audit("risk_approval.invalid", req, result="ignored",
                                detail={"reason": "requested_by 不可自批",
                                        "approver": item.id})
                    continue
                if item.decision == "rejected":
                    rejected = True
                    req.comment = f"{email}: {decision.comment or 'rejected'}"
                    break
                existing = req.approvers.get(email)
                if existing and existing.get("decision") == "approved":
                    continue
                req.approvers[email] = {
                    "provider": ptype,
                    "id": item.id,
                    "name": item.name,
                    "decision": "approved",
                    "at": item.at or time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            if decision.status == "rejected" and not decision.approvers:
                rejected = True
                req.comment = decision.comment or "Provider 拒绝"
            if rejected:
                req.status = "rejected"
                status = "rejected"
            elif len(req.approvers) >= req.required:
                req.status = "approved"
                status = "approved"
            else:
                status = "pending"
            self._requests[req.id] = req
            event = self._events.get(req.id)
        self._save()
        self._audit(
            "risk_approval.reject" if status == "rejected"
            else "risk_approval.approve" if status == "approved"
            else "risk_approval.progress",
            req, result=status,
            detail={"approvers": list(req.approvers.keys()),
                    "comment": req.comment})
        if status in TERMINAL_STATUS and event is not None:
            event.set()
        return status

    # ---- 查询 ----

    def pending(self) -> list[dict]:
        now = time.time()
        with self._lock:
            items = [r for r in self._requests.values() if r.status == "pending"]
        return [r.public(now) for r in items]

    def get(self, request_id: str) -> QuorumApprovalRequest | None:
        with self._lock:
            return self._requests.get(request_id)

    def clear(self) -> None:
        with self._lock:
            items = list(self._requests.values())
            self._requests.clear()
        for req in items:
            req.status = "cancelled"
            ev = self._events.get(req.id)
            if ev is not None:
                ev.set()
        self._save()


_MANAGERS: dict[str, QuorumApprovalManager] = {}
_MANAGER_LOCK = threading.Lock()


def get_manager(state_dir: Path | str) -> QuorumApprovalManager:
    key = str(Path(state_dir).resolve())
    with _MANAGER_LOCK:
        mgr = _MANAGERS.get(key)
        if mgr is None:
            mgr = QuorumApprovalManager(Path(key))
            _MANAGERS[key] = mgr
        return mgr
