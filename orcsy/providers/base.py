"""审批 Provider 的公共契约：数据类、异常、统一回调签名与出站 HTTP。

各厂商适配器（gitlab/github/feishu/dingtalk/fake）实现 :class:`ApprovalProvider`
协议，由 :func:`orcsy.providers.build_provider` 按类型实例化。

本模块不依赖任何厂商实现，也不在导入期依赖 ``config``（仅类型标注），
避免与 ``config`` 形成循环导入。
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

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


def _is_loopback_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _http_json(method: str, url: str, *, headers: dict[str, str],
               body: dict | None = None, verify: bool = True,
               timeout: int = 10) -> Any:
    """Provider 出站请求统一过 egress；任何网络/HTTP 错误转 ProviderError。"""
    from .. import egress
    try:
        egress.check(url, purpose="approval_provider")
    except Exception as exc:  # noqa: BLE001 — EgressDenied 等
        raise ProviderError(f"出网白名单拒绝：{exc}") from exc
    # 回环 Provider（如本机自托管 GitLab）不能走系统代理：代理常驻时会把
    # 127.0.0.1 的请求转给上游，表现为 502/RST/超时。公网地址仍遵循代理设置。
    loopback = _is_loopback_url(url)
    try:
        with httpx.Client(verify=verify, timeout=timeout,
                          follow_redirects=True,
                          trust_env=not loopback) as client:
            resp = client.request(method, url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise ProviderError(f"Provider 请求失败：{exc}") from exc
    if resp.status_code >= 400:
        text = resp.text[:300].replace("\n", " ")
        raise ProviderError(f"Provider HTTP {resp.status_code}：{text}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ProviderError("Provider 返回不是有效 JSON") from exc


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


__all__ = [
    "CALLBACK_SIGNATURE_HEADER",
    "CALLBACK_TIMESTAMP_HEADER",
    "CALLBACK_WINDOW_SECONDS",
    "ApprovalProvider",
    "ApproverDecision",
    "ProviderDecision",
    "ProviderError",
    "ProviderHandle",
    "decision_from_payload",
    "sign_callback",
    "verify_callback_signature",
]
