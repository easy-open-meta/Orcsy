"""GitHub 审批 Provider：issue + 评论（不推送代码分支）。"""

from __future__ import annotations

import hashlib
import hmac
import json

from ..config import ApprovalProviderConfig
from .base import (ApproverDecision, ProviderDecision, ProviderError,
                   ProviderHandle, _headers_lower, _http_json)
from .issue import _issue_body, _issue_title, _parse_approval_command


class GitHubProvider:
    """GitHub 审批工单（issue + 评论），不推送代码分支。"""

    type = "github"

    def __init__(self, name: str, config: ApprovalProviderConfig):
        self.name = name
        self.config = config
        self.base = (config.base_url or "https://api.github.com").rstrip("/")
        if not config.repo or "/" not in config.repo:
            raise ProviderError(f"GitHub Provider {name} 的 repo 必须是 owner/name")
        self.repo = config.repo

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }

    def create(self, manifest: dict) -> ProviderHandle:
        url = f"{self.base}/repos/{self.repo}/issues"
        data = _http_json(
            "POST", url, headers=self._headers(), body={
                "title": _issue_title(manifest),
                "body": _issue_body(manifest),
            }, verify=self.config.verify_tls,
            timeout=self.config.timeout_seconds)
        number = data.get("number")
        if not number:
            raise ProviderError("GitHub 创建 issue 未返回 number")
        return ProviderHandle(
            external_id=str(number), url=str(data.get("html_url") or ""),
            state={"digest": manifest.get("digest", ""),
                   "request_id": manifest.get("request_id", "")})

    def poll(self, handle: ProviderHandle) -> ProviderDecision | None:
        url = (f"{self.base}/repos/{self.repo}/issues/"
               f"{handle.external_id}/comments?per_page=100")
        notes = _http_json("GET", url, headers=self._headers(),
                           verify=self.config.verify_tls,
                           timeout=self.config.timeout_seconds)
        if not isinstance(notes, list):
            raise ProviderError("GitHub comments 返回格式异常")
        approvers: list[ApproverDecision] = []
        for note in notes:
            decision, _ = _parse_approval_command(note.get("body") or "")
            if not decision:
                continue
            user = note.get("user") or {}
            login = str(user.get("login") or "")
            if not login:
                continue
            approvers.append(ApproverDecision(
                provider="github", id=login,
                name=str(user.get("name") or login),
                decision=decision, at=str(note.get("created_at") or "")))
        return ProviderDecision(
            status="pending", approvers=approvers,
            digest=str(handle.state.get("digest") or ""),
            external_id=handle.external_id)

    def verify_callback(self, headers: dict[str, str],
                        raw_body: bytes) -> ProviderDecision:
        h = _headers_lower(headers)
        sig = h.get("x-hub-signature-256", "")
        if not self.config.callback_secret or not sig.startswith("sha256="):
            raise ProviderError("GitHub Webhook 缺少签名")
        expected = hmac.new(
            self.config.callback_secret.encode("utf-8"), raw_body,
            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig[7:].lower(), expected):
            raise ProviderError("GitHub Webhook 签名不匹配")
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError("GitHub Webhook 不是有效 JSON") from exc
        if payload.get("action") != "created":
            raise ProviderError("仅支持 GitHub issue_comment created")
        comment = payload.get("comment") or {}
        issue = payload.get("issue") or {}
        decision, _ = _parse_approval_command(comment.get("body") or "")
        user = comment.get("user") or {}
        approvers = []
        if decision and user.get("login"):
            approvers.append(ApproverDecision(
                provider="github", id=str(user.get("login")),
                name=str(user.get("login")), decision=decision,
                at=str(comment.get("created_at") or "")))
        return ProviderDecision(
            status="pending", approvers=approvers,
            external_id=str(issue.get("number") or ""))

    def cancel(self, handle: ProviderHandle) -> None:
        try:
            _http_json(
                "PATCH",
                f"{self.base}/repos/{self.repo}/issues/{handle.external_id}",
                headers=self._headers(), body={"state": "closed"},
                verify=self.config.verify_tls,
                timeout=self.config.timeout_seconds)
        except ProviderError:
            return


__all__ = ["GitHubProvider"]
