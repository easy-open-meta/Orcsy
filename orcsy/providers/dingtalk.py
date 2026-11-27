"""钉钉审批 Provider：审批实例轮询（Webhook 暂未启用）。"""

from __future__ import annotations

import time
from urllib.parse import quote

from ..config import ApprovalProviderConfig
from .base import (ApproverDecision, ProviderDecision, ProviderError,
                   ProviderHandle, _http_json)


class DingTalkProvider:
    """钉钉审批实例（轮询）；Webhook 暂不启用。"""

    type = "dingtalk"

    def __init__(self, name: str, config: ApprovalProviderConfig):
        self.name = name
        self.config = config
        self.base = (config.base_url or "https://oapi.dingtalk.com").rstrip("/")
        self._token = ""
        self._token_expires = 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires:
            return self._token
        url = (f"{self.base}/gettoken?appkey={quote(self.config.app_key)}"
               f"&appsecret={quote(self.config.app_secret)}")
        data = _http_json("GET", url, headers={},
                          verify=self.config.verify_tls,
                          timeout=self.config.timeout_seconds)
        token = str(data.get("access_token") or "")
        if not token:
            raise ProviderError("钉钉未返回 access_token")
        self._token = token
        self._token_expires = time.time() + 3600
        return token

    def create(self, manifest: dict) -> ProviderHandle:
        data = _http_json(
            "POST",
            f"{self.base}/topapi/processinstance/create"
            f"?access_token={quote(self._access_token())}",
            headers={"Content-Type": "application/json"}, body={
                "process_code": self.config.process_code,
                "originator_user_id": self.config.originator_user_id,
                "dept_id": int(self.config.dept_id),
                "form_component_values": [dict(v) for v in
                                         self.config.form_component_values],
            }, verify=self.config.verify_tls,
            timeout=self.config.timeout_seconds)
        code = str(data.get("process_instance_id") or "")
        if not code:
            raise ProviderError("钉钉创建审批未返回 process_instance_id")
        return ProviderHandle(
            external_id=code, url="",
            state={"digest": manifest.get("digest", ""),
                   "request_id": manifest.get("request_id", "")})

    def poll(self, handle: ProviderHandle) -> ProviderDecision | None:
        data = _http_json(
            "POST",
            f"{self.base}/topapi/processinstance/get"
            f"?access_token={quote(self._access_token())}",
            headers={"Content-Type": "application/json"},
            body={"process_instance_id": handle.external_id},
            verify=self.config.verify_tls,
            timeout=self.config.timeout_seconds)
        inst = data.get("process_instance") or {}
        records = inst.get("operation_records") or []
        approvers: list[ApproverDecision] = []
        for rec in records:
            # 真实钉钉返回 operation_result；mock/旧数据用 result
            result = str(rec.get("result") or rec.get("operation_result") or "").upper()
            if result not in ("AGREE", "REFUSE"):
                continue
            uid = str(rec.get("userid") or rec.get("user_id") or "")
            if not uid:
                continue
            approvers.append(ApproverDecision(
                provider="dingtalk", id=uid,
                name=str(rec.get("user_name") or uid),
                decision="approved" if result == "AGREE" else "rejected",
                at=str(rec.get("date") or rec.get("create_time") or "")))
        return ProviderDecision(
            status="pending", approvers=approvers,
            digest=str(handle.state.get("digest") or ""),
            external_id=handle.external_id)

    def verify_callback(self, headers: dict[str, str],
                        raw_body: bytes) -> ProviderDecision:
        raise ProviderError("钉钉审批回调暂未启用，请使用轮询")

    def cancel(self, handle: ProviderHandle) -> None:
        return


__all__ = ["DingTalkProvider"]
