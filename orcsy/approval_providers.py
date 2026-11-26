"""外部审批 Provider 接口与 Phase 2A 假 Provider。

真实适配器（GitLab/GitHub/飞书/钉钉）在后续阶段实现；本模块先固定协议：
Provider 只负责「创建审批 → 轮询/回调 → 返回谁批准了」，Orcsy 负责 email
白名单、quorum、digest 与 fail-closed 校验。

回调统一使用 `X-Orcsy-Signature: sha256=<hex>` + `X-Orcsy-Timestamp: <epoch>`；
HMAC 覆盖 `timestamp + "." + raw_body`。真实 Provider 若有自己的签名格式，
适配器先验自己的签名，再归一化成 ProviderDecision。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import ApprovalProviderConfig

CALLBACK_SIGNATURE_HEADER = "X-Orcsy-Signature"
CALLBACK_TIMESTAMP_HEADER = "X-Orcsy-Timestamp"
CALLBACK_WINDOW_SECONDS = 300


class ProviderError(RuntimeError):
    """Provider 不可达、协议错误或签名无效；调用方必须 fail closed。"""


@dataclass
class ApproverDecision:
    provider: str
    id: str
    name: str = ""
    decision: str = "approved"       # approved / rejected
    at: str = ""


@dataclass
class ProviderDecision:
    status: str                      # pending / approved / rejected
    approvers: list[ApproverDecision] = field(default_factory=list)
    comment: str = ""
    digest: str = ""
    external_id: str = ""
    request_id: str = ""


@dataclass
class ProviderHandle:
    external_id: str
    url: str = ""
    state: dict[str, Any] = field(default_factory=dict)


class ApprovalProvider(Protocol):
    type: str

    def create(self, manifest: dict) -> ProviderHandle: ...
    def poll(self, handle: ProviderHandle) -> ProviderDecision | None: ...
    def verify_callback(self, headers: dict[str, str],
                        raw_body: bytes) -> ProviderDecision: ...
    def cancel(self, handle: ProviderHandle) -> None: ...


def _headers_lower(headers: dict[str, str]) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (headers or {}).items()}


def verify_callback_signature(secret: str, headers: dict[str, str],
                              raw_body: bytes,
                              window: int = CALLBACK_WINDOW_SECONDS) -> None:
    """校验统一回调签名；失败抛 ProviderError。"""
    if not secret:
        raise ProviderError("Provider 未配置 callback_secret，拒绝回调")
    h = _headers_lower(headers)
    sig = str(h.get(CALLBACK_SIGNATURE_HEADER.lower()) or "").strip()
    ts = str(h.get(CALLBACK_TIMESTAMP_HEADER.lower()) or "").strip()
    if not sig.startswith("sha256=") or not ts:
        raise ProviderError("回调缺少签名或时间戳")
    try:
        timestamp = int(ts)
    except ValueError as exc:
        raise ProviderError("回调时间戳不是整数") from exc
    if abs(time.time() - timestamp) > window:
        raise ProviderError("回调时间戳超出允许窗口")
    expected = hmac.new(
        secret.encode("utf-8"), ts.encode("ascii") + b"." + raw_body,
        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig[7:].lower(), expected):
        raise ProviderError("回调签名不匹配")


def sign_callback(secret: str, raw_body: bytes,
                  timestamp: int | None = None) -> tuple[dict[str, str], bytes]:
    """生成统一回调头（测试/本地自测用）。"""
    ts = str(int(time.time() if timestamp is None else timestamp))
    digest = hmac.new(
        secret.encode("utf-8"), ts.encode("ascii") + b"." + raw_body,
        hashlib.sha256).hexdigest()
    return {CALLBACK_SIGNATURE_HEADER: f"sha256={digest}",
            CALLBACK_TIMESTAMP_HEADER: ts}, raw_body


class FakeProvider:
    """本地假 Provider：create 后由测试/本地自测注入决定。"""

    type = "fake"

    def __init__(self, name: str, config: ApprovalProviderConfig | None = None):
        self.name = name
        self.config = config or ApprovalProviderConfig(type="fake")
        self._decisions: dict[str, ProviderDecision] = {}

    def create(self, manifest: dict) -> ProviderHandle:
        external_id = f"fake-{manifest.get('request_id') or int(time.time())}"
        return ProviderHandle(external_id=external_id,
                              url=f"fake://{self.name}/{external_id}")

    def set_decision(self, external_id: str, decision: ProviderDecision) -> None:
        self._decisions[external_id] = decision

    def poll(self, handle: ProviderHandle) -> ProviderDecision | None:
        return self._decisions.get(handle.external_id)

    def verify_callback(self, headers: dict[str, str],
                        raw_body: bytes) -> ProviderDecision:
        verify_callback_signature(self.config.callback_secret, headers, raw_body)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError("假 Provider 回调不是有效 JSON") from exc
        return decision_from_payload(payload)

    def cancel(self, handle: ProviderHandle) -> None:
        self._decisions.pop(handle.external_id, None)


class UnimplementedProvider:
    """真实适配器尚未接入时的占位：任何操作都 fail closed。"""

    def __init__(self, name: str, config: ApprovalProviderConfig,
                 ptype: str):
        self.name = name
        self.config = config
        self.type = ptype

    def create(self, manifest: dict) -> ProviderHandle:
        raise ProviderError(
            f"Provider {self.name}（{self.type}）适配器尚未实现；"
            "请在 2B/2C 接入后再启用 quorum")

    def poll(self, handle: ProviderHandle) -> ProviderDecision | None:
        raise ProviderError("Provider 适配器尚未实现")

    def verify_callback(self, headers: dict[str, str],
                        raw_body: bytes) -> ProviderDecision:
        raise ProviderError("Provider 适配器尚未实现")

    def cancel(self, handle: ProviderHandle) -> None:
        return


def decision_from_payload(payload: dict) -> ProviderDecision:
    """把统一回调 JSON 归一化为 ProviderDecision。"""
    if not isinstance(payload, dict):
        raise ProviderError("回调 payload 必须是 JSON 对象")
    status = str(payload.get("decision") or payload.get("status") or "").lower()
    if status not in ("approved", "rejected", "pending"):
        raise ProviderError(f"未知审批状态：{status or '空'}")
    approvers: list[ApproverDecision] = []
    raw_items = payload.get("approvers") or []
    if not isinstance(raw_items, list):
        raise ProviderError("approvers 必须是数组")
    for item in raw_items:
        if not isinstance(item, dict):
            raise ProviderError("approvers 条目必须是对象")
        decision = str(item.get("decision") or "approved").lower()
        if decision not in ("approved", "rejected"):
            raise ProviderError(f"approver decision 非法：{decision}")
        approvers.append(ApproverDecision(
            provider=str(item.get("provider") or ""),
            id=str(item.get("id") or ""),
            name=str(item.get("name") or ""),
            decision=decision,
            at=str(item.get("at") or ""),
        ))
    return ProviderDecision(
        status=status, approvers=approvers,
        comment=str(payload.get("comment") or ""),
        digest=str(payload.get("digest") or ""),
        external_id=str(payload.get("external_id") or ""),
        request_id=str(payload.get("request_id") or ""),
    )


def build_provider(name: str, config: ApprovalProviderConfig):
    if config.type == "fake":
        return FakeProvider(name, config)
    return UnimplementedProvider(name, config, config.type)


def resolve_email(approvers: dict[str, dict[str, str]], provider_type: str,
                  identity: str) -> str | None:
    """Provider 身份 → email；未登记返回 None。"""
    wanted = str(identity or "").casefold()
    if not wanted:
        return None
    for email, ids in approvers.items():
        if str(ids.get(provider_type) or "").casefold() == wanted:
            return email
    return None
