"""飞书审批 Provider：审批实例轮询（Webhook 暂未启用）。"""

from __future__ import annotations

import json
import time

from ..config import ApprovalProviderConfig
from .base import (ApproverDecision, ProviderDecision, ProviderError,
                   ProviderHandle, _http_json)


class FeishuProvider:
    """飞书审批实例（轮询）；Webhook 暂不启用，避免依赖事件订阅配置。"""

    type = "feishu"

    def __init__(self, name: str, config: ApprovalProviderConfig):
        self.name = name
        self.config = config
        self.base = (config.base_url or "https://open.feishu.cn").rstrip("/")
        self._token = ""
        self._token_expires = 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires:
            return self._token
        data = _http_json(
            "POST",
            f"{self.base}/open-apis/auth/v3/tenant_access_token/internal",
            headers={"Content-Type": "application/json"},
            body={"app_id": self.config.app_id,
                  "app_secret": self.config.app_secret},
            verify=self.config.verify_tls,
            timeout=self.config.timeout_seconds)
        token = str(data.get("tenant_access_token") or "")
        if not token:
            raise ProviderError("飞书未返回 tenant_access_token")
        self._token = token
        self._token_expires = time.time() + max(
            60, int(data.get("expire") or 7200) - 120)
        return token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/json"}

    def create(self, manifest: dict) -> ProviderHandle:
        summary = (f"{manifest.get('project')}/{manifest.get('task_id')} · "
                   f"{manifest.get('risk', {}).get('level')} · "
                   f"digest {manifest.get('digest', '')[:16]}")
        # 飞书新版自定义审批要求 form 为“控件数组”的 JSON 字符串：
        #   [{"id": widget_id, "type": "input", "value": "..."}]
        # 旧实现的 map / "{}" 在真实免费版租户会返回 1390001 表单格式不正确。
        form = "[]"
        if self.config.form_widget_id:
            form = json.dumps([{"id": self.config.form_widget_id,
                                "type": "input", "value": summary}],
                              ensure_ascii=False)
        data = _http_json(
            "POST", f"{self.base}/open-apis/approval/v4/instances",
            headers=self._headers(), body={
                "approval_code": self.config.approval_code,
                "user_id": self.config.originator_user_id,
                "form": form,
                "uuid": manifest.get("request_id", ""),
            }, verify=self.config.verify_tls,
            timeout=self.config.timeout_seconds)
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        code = str(payload.get("instance_code") or "")
        if not code:
            raise ProviderError("飞书创建审批未返回 instance_code")
        return ProviderHandle(
            external_id=code, url=str(payload.get("instance_url") or ""),
            state={"digest": manifest.get("digest", ""),
                   "request_id": manifest.get("request_id", "")})

    def poll(self, handle: ProviderHandle) -> ProviderDecision | None:
        data = _http_json(
            "GET",
            f"{self.base}/open-apis/approval/v4/instances/{handle.external_id}",
            headers=self._headers(), verify=self.config.verify_tls,
            timeout=self.config.timeout_seconds)
        inst = data.get("data") if isinstance(data.get("data"), dict) else data
        tasks = inst.get("task_list") or inst.get("tasks") or []
        approvers: list[ApproverDecision] = []
        for task in tasks:
            status = str(task.get("status") or "").upper()
            if status not in ("APPROVED", "REJECTED"):
                continue
            uid = str(task.get("user_id") or task.get("open_id") or "")
            if not uid:
                continue
            approvers.append(ApproverDecision(
                provider="feishu", id=uid,
                name=str(task.get("user_name") or uid),
                decision="approved" if status == "APPROVED" else "rejected",
                at=str(task.get("operate_time") or task.get("update_time") or "")))
        return ProviderDecision(
            status="pending", approvers=approvers,
            digest=str(handle.state.get("digest") or ""),
            external_id=handle.external_id)

    def verify_callback(self, headers: dict[str, str],
                        raw_body: bytes) -> ProviderDecision:
        raise ProviderError("飞书审批回调暂未启用，请使用轮询")

    def cancel(self, handle: ProviderHandle) -> None:
        return


__all__ = ["FeishuProvider"]
