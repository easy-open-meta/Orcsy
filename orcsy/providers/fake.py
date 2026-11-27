"""本地假 Provider：测试/本地自测注入审批决定。"""

from __future__ import annotations

import json
import time

from ..config import ApprovalProviderConfig
from .base import (ProviderDecision, ProviderError, ProviderHandle,
                   decision_from_payload, verify_callback_signature)


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


__all__ = ["FakeProvider"]
