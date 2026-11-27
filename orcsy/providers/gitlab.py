"""GitLab 审批 Provider：issue + 评论（不推送代码分支）。"""

from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import quote

from ..config import ApprovalProviderConfig
from .base import (ApproverDecision, ProviderDecision, ProviderError,
                   ProviderHandle, _headers_lower, _http_json)
from .issue import _issue_body, _issue_title, _parse_approval_command


class GitLabProvider:
    """GitLab 审批工单（issue + 评论），不推送代码分支。"""

    type = "gitlab"

    def __init__(self, name: str, config: ApprovalProviderConfig):
        self.name = name
        self.config = config
        self.base = (config.base_url or "https://gitlab.com").rstrip("/")
        if not config.project_id:
            raise ProviderError(f"GitLab Provider {name} 缺少 project_id")
        self.project = quote(str(config.project_id), safe="")

    def _headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self.config.token,
                "Content-Type": "application/json"}

    def create(self, manifest: dict) -> ProviderHandle:
        url = f"{self.base}/api/v4/projects/{self.project}/issues"
        data = _http_json(
            "POST", url, headers=self._headers(), body={
                "title": _issue_title(manifest),
                "description": _issue_body(manifest),
            }, verify=self.config.verify_tls,
            timeout=self.config.timeout_seconds)
        iid = data.get("iid")
        if not iid:
            raise ProviderError("GitLab 创建 issue 未返回 iid")
        return ProviderHandle(
            external_id=str(iid), url=str(data.get("web_url") or ""),
            state={"digest": manifest.get("digest", ""),
                   "request_id": manifest.get("request_id", "")})

    def poll(self, handle: ProviderHandle) -> ProviderDecision | None:
        url = (f"{self.base}/api/v4/projects/{self.project}/issues/"
               f"{handle.external_id}/notes?per_page=100&sort=asc&order_by=created_at")
        notes = _http_json("GET", url, headers=self._headers(),
                           verify=self.config.verify_tls,
                           timeout=self.config.timeout_seconds)
        if not isinstance(notes, list):
            raise ProviderError("GitLab notes 返回格式异常")
        approvers: list[ApproverDecision] = []
        for note in notes:
            if note.get("system"):
                continue
            decision, _ = _parse_approval_command(note.get("body") or "")
            if not decision:
                continue
            author = note.get("author") or {}
            username = str(author.get("username") or "")
            if not username:
                continue
            approvers.append(ApproverDecision(
                provider="gitlab", id=username,
                name=str(author.get("name") or username),
                decision=decision, at=str(note.get("created_at") or "")))
        return ProviderDecision(
            status="pending", approvers=approvers,
            digest=str(handle.state.get("digest") or ""),
            external_id=handle.external_id)

    def verify_callback(self, headers: dict[str, str],
                        raw_body: bytes) -> ProviderDecision:
        h = _headers_lower(headers)
        token = h.get("x-gitlab-token", "")
        if not self.config.callback_secret or not hmac.compare_digest(
                token, self.config.callback_secret):
            raise ProviderError("GitLab Webhook token 不匹配")
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError("GitLab Webhook 不是有效 JSON") from exc
        if payload.get("object_kind") != "note":
            raise ProviderError("仅支持 GitLab note Webhook")
        attrs = payload.get("object_attributes") or {}
        issue = payload.get("issue") or {}
        decision, _ = _parse_approval_command(attrs.get("note") or "")
        user = payload.get("user") or {}
        approvers = []
        if decision and user.get("username"):
            approvers.append(ApproverDecision(
                provider="gitlab", id=str(user.get("username")),
                name=str(user.get("name") or user.get("username")),
                decision=decision, at=str(attrs.get("created_at") or "")))
        return ProviderDecision(
            status="pending", approvers=approvers,
            external_id=str(issue.get("iid") or ""))

    def cancel(self, handle: ProviderHandle) -> None:
        try:
            _http_json(
                "PUT",
                f"{self.base}/api/v4/projects/{self.project}/issues/"
                f"{handle.external_id}",
                headers=self._headers(), body={"state_event": "close"},
                verify=self.config.verify_tls,
                timeout=self.config.timeout_seconds)
        except ProviderError:
            return


__all__ = ["GitLabProvider"]
