"""真实 HTTP 契约测试：四个 Provider 适配器 + 会签引擎，对接本地模拟厂商服务。

覆盖：
- GitLab / GitHub：建 issue、拉评论、`/approve` `/reject` 解析；
- 飞书 / 钉钉：取 token、建审批实例、拉 task_list / operation_records；
- QuorumApprovalManager 走真实 GitLab 适配器轮询计数（email 白名单映射）；
- GitLab Webhook 回调经 manager 验签并推进会签；
- 持久化落盘后可被新 manager 实例读回。

不等同于真实厂商沙箱：仅验证请求/响应契约与 Orcsy 侧状态机。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_vendor_server import (DINGTALK_APP, FEISHU_APP, GITHUB_TOKEN,
                                GITLAB_TOKEN, start_server)
from orcsy.providers import (DingTalkProvider, FeishuProvider,
                                      GitHubProvider, GitLabProvider,
                                      ProviderError)
from orcsy.config import ApprovalProviderConfig
from orcsy.quorum_approval import QuorumApprovalManager

MANIFEST = {
    "request_id": "req-http-1", "project": "p", "task_id": "t",
    "title": "风险任务", "risk": {"level": "high", "reasons": ["r"],
                                  "files": ["a.py"], "additions": 1,
                                  "deletions": 0},
    "commit": "abc", "digest": "digest-http", "required_approvals": 2,
    "allowed_approvers": ["alice@example.com", "bob@example.com"],
}


# ---- helpers ----

def _retry(fn, attempts: int = 8):
    """本机回环偶发 RST（安全软件/代理劫持回环），重试即恢复。"""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — 仅回环抖动重试
            last = exc
            text = str(exc)
            if "10054" not in text and "Connect" not in text:
                raise
            time.sleep(0.2 * (i + 1))
    raise last  # type: ignore[misc]


def _run_case(fn, attempts: int = 4):
    """整段用例重试：create 若在 RST 中失败，换新实例重跑。"""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except (AssertionError, ProviderError, OSError) as exc:
            last = exc
            time.sleep(0.4 * (i + 1))
    raise last  # type: ignore[misc]


def _control(base: str, path: str, payload: dict) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call():
        req = urllib.request.Request(
            base + path, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=10) as r:
            assert r.status == 200
    _retry(call)


# ---- 适配器直连 ----

def check_gitlab(base: str) -> None:
    cfg = ApprovalProviderConfig(type="gitlab", base_url=base,
                                 project_id="group/proj", token=GITLAB_TOKEN,
                                 callback_secret="s")
    p = GitLabProvider("gl", cfg)
    handle = _retry(lambda: p.create(MANIFEST))
    assert handle.external_id and handle.url
    _control(base, "/__mock/gitlab/comment", {
        "iid": int(handle.external_id), "username": "alice",
        "name": "Alice", "body": "/approve looks good"})
    _control(base, "/__mock/gitlab/comment", {
        "iid": int(handle.external_id), "username": "bob",
        "name": "Bob", "body": "/reject no"})
    d = _retry(lambda: p.poll(handle))
    assert d is not None and d.digest == "digest-http"
    assert [(a.id, a.decision) for a in d.approvers] == [
        ("alice", "approved"), ("bob", "rejected")]


def check_github(base: str) -> None:
    cfg = ApprovalProviderConfig(type="github", base_url=base,
                                 repo="owner/name", token=GITHUB_TOKEN,
                                 callback_secret="s")
    p = GitHubProvider("gh", cfg)
    handle = _retry(lambda: p.create(MANIFEST))
    assert handle.external_id and handle.url
    _control(base, "/__mock/github/comment", {
        "number": int(handle.external_id), "login": "alice", "body": "/approve"})
    d = _retry(lambda: p.poll(handle))
    assert d is not None and d.digest == "digest-http"
    assert [(a.id, a.decision) for a in d.approvers] == [("alice", "approved")], (
        f"handle={handle.external_id} approvers={d.approvers}")


def check_feishu(base: str) -> None:
    cfg = ApprovalProviderConfig(
        type="feishu", base_url=base, app_id=FEISHU_APP[0],
        app_secret=FEISHU_APP[1], approval_code="code",
        originator_user_id="ou_origin")
    p = FeishuProvider("fs", cfg)
    handle = _retry(lambda: p.create(MANIFEST))
    _control(base, "/__mock/feishu/approve", {
        "instance_code": handle.external_id, "user_id": "ou_a",
        "status": "APPROVED"})
    _control(base, "/__mock/feishu/approve", {
        "instance_code": handle.external_id, "user_id": "ou_b",
        "status": "REJECTED"})
    d = _retry(lambda: p.poll(handle))
    assert d is not None and d.digest == "digest-http"
    assert [(a.id, a.decision) for a in d.approvers] == [
        ("ou_a", "approved"), ("ou_b", "rejected")]


def check_dingtalk(base: str) -> None:
    cfg = ApprovalProviderConfig(
        type="dingtalk", base_url=base, app_key=DINGTALK_APP[0],
        app_secret=DINGTALK_APP[1], process_code="proc",
        originator_user_id="user-1", dept_id=42)
    p = DingTalkProvider("dt", cfg)
    handle = _retry(lambda: p.create(MANIFEST))
    _control(base, "/__mock/dingtalk/approve", {
        "process_instance_id": handle.external_id, "userid": "u1",
        "result": "AGREE"})
    _control(base, "/__mock/dingtalk/approve", {
        "process_instance_id": handle.external_id, "userid": "u2",
        "result": "REFUSE"})
    d = _retry(lambda: p.poll(handle))
    assert d is not None and d.digest == "digest-http"
    assert [(a.id, a.decision) for a in d.approvers] == [
        ("u1", "approved"), ("u2", "rejected")]


# ---- 会签引擎 + 真实适配器 ----

APPROVER_MAP = {
    "alice@example.com": {"gitlab": "alice", "github": "alice"},
    "bob@example.com": {"gitlab": "bob", "github": "bob"},
}


def _start_request(mgr: QuorumApprovalManager, cfg: ApprovalProviderConfig,
                   digest: str, poll_interval: int = 5):
    box: dict = {}

    def run() -> None:
        box["outcome"] = mgr.request(
            project="p", task_id="t", title="风险任务", risk_level="high",
            reasons=["r"], files=["a.py"], additions=1, deletions=0,
            branch="b", commit="abc", digest=digest,
            provider_name=cfg.type + "-name", provider_cfg=cfg,
            approver_map=APPROVER_MAP,
            allowed_approvers=["alice@example.com", "bob@example.com"],
            required=2, requested_by="agent:backend", timeout_seconds=60,
            poll_interval_seconds=poll_interval)

    t = threading.Thread(target=run)
    t.start()
    deadline = time.time() + 8
    external_id = ""
    while time.time() < deadline:
        pending = mgr.pending()
        if pending:
            external_id = pending[0]["external_id"]
            if external_id:
                break
        time.sleep(0.05)
    if not external_id:
        t.join(timeout=5)
        outcome = box.get("outcome")
        raise ProviderError(
            "会签请求未创建成功："
            + (outcome.error if outcome is not None else "create 超时"))
    return t, box, external_id


def check_manager_gitlab_approve(base: str) -> None:
    cfg = ApprovalProviderConfig(type="gitlab", base_url=base,
                                 project_id="group/proj", token=GITLAB_TOKEN,
                                 callback_secret="s")
    with tempfile.TemporaryDirectory() as tmp:
        mgr = QuorumApprovalManager(Path(tmp))
        t, box, iid = _start_request(mgr, cfg, "digest-mgr-gl")
        _control(base, "/__mock/gitlab/comment", {
            "iid": int(iid), "username": "alice", "name": "Alice",
            "body": "/approve"})
        _control(base, "/__mock/gitlab/comment", {
            "iid": int(iid), "username": "bob", "name": "Bob",
            "body": "/approve"})
        t.join(timeout=30)
        outcome = box.get("outcome")
        assert outcome is not None and outcome.approved, (
            f"expected approved, got {outcome}")
        assert sorted(outcome.request.approvers) == [
            "alice@example.com", "bob@example.com"]
        assert outcome.request.approved_count == 2
        # 持久化：新实例读回终态，不再出现在 pending
        again = QuorumApprovalManager(Path(tmp))
        loaded = again.get(outcome.request.id)
        assert loaded is not None and loaded.status == "approved"
        assert again.pending() == []


def check_manager_github_veto(base: str) -> None:
    cfg = ApprovalProviderConfig(type="github", base_url=base,
                                 repo="owner/name", token=GITHUB_TOKEN,
                                 callback_secret="s")
    with tempfile.TemporaryDirectory() as tmp:
        mgr = QuorumApprovalManager(Path(tmp))
        t, box, number = _start_request(mgr, cfg, "digest-mgr-gh")
        _control(base, "/__mock/github/comment", {
            "number": int(number), "login": "alice", "body": "/approve"})
        _control(base, "/__mock/github/comment", {
            "number": int(number), "login": "bob", "body": "/reject 有风险"})
        t.join(timeout=30)
        outcome = box.get("outcome")
        assert outcome is not None and not outcome.approved
        assert outcome.request.status == "rejected"


def check_manager_gitlab_callback(base: str) -> None:
    """Webhook 回调路径：验签 + request 关联 + 计数（不等轮询）。"""
    cfg = ApprovalProviderConfig(type="gitlab", base_url=base,
                                 project_id="group/proj", token=GITLAB_TOKEN,
                                 callback_secret="hook-secret")
    with tempfile.TemporaryDirectory() as tmp:
        mgr = QuorumApprovalManager(Path(tmp))
        t, box, iid = _start_request(mgr, cfg, "digest-mgr-cb",
                                     poll_interval=60)
        req = mgr.pending()[0]

        def hook(user: str, note: str) -> str:
            payload = json.dumps({
                "object_kind": "note",
                "user": {"username": user, "name": user.title()},
                "object_attributes": {"note": note,
                                      "created_at": "2026-01-01T00:00:00Z"},
                "issue": {"iid": int(iid)},
            }).encode()
            return mgr.apply_callback(
                "gitlab-name", cfg, {"X-Gitlab-Token": "hook-secret"}, payload)

        assert hook("alice", "/approve") == "pending"
        assert hook("bob", "/approve") == "approved"
        t.join(timeout=10)
        outcome = box.get("outcome")
        assert outcome is not None and outcome.approved
        assert outcome.request.id == req["id"]
        # 重复回调幂等
        assert hook("alice", "/approve") == "duplicate"


def main() -> None:
    server, base = start_server()
    try:
        for check in (check_gitlab, check_github, check_feishu, check_dingtalk):
            _run_case(lambda c=check: c(base))
        for check in (check_manager_gitlab_approve,
                      check_manager_github_veto,
                      check_manager_gitlab_callback):
            _run_case(lambda c=check: c(base))
    finally:
        server.shutdown()
        server.server_close()
    print("test_approval_adapters_http: all checks passed")


if __name__ == "__main__":
    main()
