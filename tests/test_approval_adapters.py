"""GitLab/GitHub issue-based approval adapters (no network)."""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.approval_providers import (DingTalkProvider, FeishuProvider,
                                      GitHubProvider, GitLabProvider,
                                      ProviderError, build_provider)
from orcsy.config import ApprovalProviderConfig

MANIFEST = {
    "request_id": "req-1", "project": "p", "task_id": "t", "title": "风险任务",
    "risk": {"level": "high", "reasons": ["自更新"], "files": ["a.py"],
             "additions": 1, "deletions": 0},
    "commit": "abc", "digest": "digest-1", "required_approvals": 2,
    "allowed_approvers": ["alice@example.com", "bob@example.com"],
}


def check_gitlab() -> None:
    cfg = ApprovalProviderConfig(
        type="gitlab", base_url="https://gitlab.example", project_id="group/proj",
        token="t", callback_secret="s")
    provider = GitLabProvider("gl", cfg)
    with mock.patch("orcsy.approval_providers._http_json") as http:
        http.return_value = {"iid": 7, "web_url": "https://gitlab.example/i/7"}
        handle = provider.create(MANIFEST)
        assert handle.external_id == "7"
        assert "/projects/group%2Fproj/issues" in http.call_args.args[1]
        assert http.call_args.kwargs["body"]["labels"] == "orcsy-approval"

        http.return_value = [
            {"system": True, "body": "/approve", "author": {"username": "bot"}},
            {"system": False, "body": "/approve\nlooks good",
             "author": {"username": "alice", "name": "Alice"},
             "created_at": "2026-01-01T00:00:00Z"},
            {"system": False, "body": "/reject not now",
             "author": {"username": "bob", "name": "Bob"},
             "created_at": "2026-01-01T00:01:00Z"},
        ]
        decision = provider.poll(handle)
        assert decision is not None
        assert decision.digest == "digest-1"
        assert [(a.id, a.decision) for a in decision.approvers] == [
            ("alice", "approved"), ("bob", "rejected")]

    payload = {
        "object_kind": "note",
        "user": {"username": "alice", "name": "Alice"},
        "issue": {"iid": 7},
        "object_attributes": {"note": "/approve", "created_at": "2026-01-01"},
    }
    raw = json.dumps(payload).encode()
    decision = provider.verify_callback({"X-Gitlab-Token": "s"}, raw)
    assert decision.external_id == "7"
    assert decision.approvers[0].id == "alice"
    try:
        provider.verify_callback({"X-Gitlab-Token": "bad"}, raw)
        raise AssertionError("错误 GitLab token 应拒绝")
    except ProviderError:
        pass


def check_github() -> None:
    cfg = ApprovalProviderConfig(
        type="github", repo="owner/name", token="t", callback_secret="s")
    provider = GitHubProvider("gh", cfg)
    with mock.patch("orcsy.approval_providers._http_json") as http:
        http.return_value = {"number": 12, "html_url": "https://github.com/o/n/issues/12"}
        handle = provider.create(MANIFEST)
        assert handle.external_id == "12"
        assert "/repos/owner/name/issues" in http.call_args.args[1]
        assert http.call_args.kwargs["body"]["labels"] == ["orcsy-approval"]

        http.return_value = [
            {"body": "/approve", "user": {"login": "alice", "name": "Alice"},
             "created_at": "2026-01-01T00:00:00Z"},
            {"body": "hello", "user": {"login": "bob"}},
        ]
        decision = provider.poll(handle)
        assert decision is not None and decision.digest == "digest-1"
        assert [(a.id, a.decision) for a in decision.approvers] == [
            ("alice", "approved")]

    payload = {
        "action": "created",
        "issue": {"number": 12},
        "comment": {"body": "/approve", "created_at": "2026-01-01",
                    "user": {"login": "alice"}},
    }
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"s", raw, hashlib.sha256).hexdigest()
    decision = provider.verify_callback({"X-Hub-Signature-256": sig}, raw)
    assert decision.external_id == "12"
    assert decision.approvers[0].id == "alice"
    try:
        provider.verify_callback({"X-Hub-Signature-256": "sha256=bad"}, raw)
        raise AssertionError("错误 GitHub 签名应拒绝")
    except ProviderError:
        pass


def check_feishu() -> None:
    cfg = ApprovalProviderConfig(
        type="feishu", app_id="app", app_secret="sec", approval_code="code",
        originator_user_id="ou_origin", form_widget_id="widget")
    provider = FeishuProvider("fs", cfg)
    calls: list[str] = []

    def fake(method: str, url: str, **kwargs):
        calls.append(url)
        if "tenant_access_token" in url:
            return {"tenant_access_token": "tok", "expire": 7200}
        if url.endswith("/instances"):
            assert kwargs["headers"]["Authorization"] == "Bearer tok"
            assert kwargs["body"]["uuid"] == "req-1"
            return {"data": {"instance_code": "inst-1"}}
        return {"data": {"task_list": [
            {"user_id": "ou_a", "status": "APPROVED"},
            {"user_id": "ou_b", "status": "REJECTED"},
            {"user_id": "ou_c", "status": "PENDING"},
        ]}}

    with mock.patch("orcsy.approval_providers._http_json", side_effect=fake):
        handle = provider.create(MANIFEST)
        assert handle.external_id == "inst-1"
        decision = provider.poll(handle)
    assert decision is not None and decision.digest == "digest-1"
    assert [(a.id, a.decision) for a in decision.approvers] == [
        ("ou_a", "approved"), ("ou_b", "rejected")]
    assert len(calls) == 3
    try:
        provider.verify_callback({}, b"{}")
        raise AssertionError("飞书回调应提示使用轮询")
    except ProviderError as exc:
        assert "轮询" in str(exc)


def check_dingtalk() -> None:
    cfg = ApprovalProviderConfig(
        type="dingtalk", app_key="key", app_secret="sec", process_code="proc",
        originator_user_id="user-1", dept_id=42)
    provider = DingTalkProvider("dt", cfg)

    def fake(method: str, url: str, **kwargs):
        if "/gettoken" in url:
            return {"access_token": "tok"}
        if "/topapi/processinstance/create" in url:
            assert kwargs["body"]["originator_user_id"] == "user-1"
            assert kwargs["body"]["dept_id"] == 42
            return {"process_instance_id": "pi-1"}
        return {"process_instance": {"operation_records": [
            {"userid": "u1", "result": "AGREE", "date": "2026-01-01"},
            {"userid": "u2", "result": "REFUSE", "date": "2026-01-02"},
        ]}}

    with mock.patch("orcsy.approval_providers._http_json", side_effect=fake):
        handle = provider.create(MANIFEST)
        assert handle.external_id == "pi-1"
        decision = provider.poll(handle)
    assert decision is not None and decision.digest == "digest-1"
    assert [(a.id, a.decision) for a in decision.approvers] == [
        ("u1", "approved"), ("u2", "rejected")]
    try:
        provider.verify_callback({}, b"{}")
        raise AssertionError("钉钉回调应提示使用轮询")
    except ProviderError as exc:
        assert "轮询" in str(exc)


def check_build_provider() -> None:
    assert isinstance(build_provider("x", ApprovalProviderConfig(type="gitlab",
                               project_id="1", token="t")), GitLabProvider)
    assert isinstance(build_provider("x", ApprovalProviderConfig(type="github",
                               repo="o/n", token="t")), GitHubProvider)
    fake = build_provider("x", ApprovalProviderConfig(type="fake"))
    assert fake.type == "fake"


def main() -> None:
    check_gitlab()
    check_github()
    check_feishu()
    check_dingtalk()
    check_build_provider()
    print("test_approval_adapters: all checks passed")


if __name__ == "__main__":
    main()
