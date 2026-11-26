"""Quorum approval: email allowlist, callbacks, persistence and executor wiring."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.approval_providers import (ApproverDecision, FakeProvider,
                                      ProviderDecision, sign_callback)
from orcsy.config import ApprovalProviderConfig
from orcsy.quorum_approval import QuorumApprovalManager

SECRET = "test-secret"
MAP = {
    "alice@example.com": {"fake": "alice"},
    "bob@example.com": {"fake": "bob"},
    "carol@example.com": {"fake": "carol"},
}
ALLOWED = ["alice@example.com", "bob@example.com"]
CFG = ApprovalProviderConfig(type="fake", callback_secret=SECRET)


def make_manager(root: Path) -> tuple[QuorumApprovalManager, FakeProvider]:
    fake = FakeProvider("fake1", CFG)
    return QuorumApprovalManager(root, provider_factory=lambda n, c: fake), fake


def start_request(mgr: QuorumApprovalManager, box: dict, **overrides):
    kwargs = dict(
        project="p", task_id="t", title="风险任务", risk_level="high",
        reasons=["自更新逻辑"], files=["orcsy/updater.py"], additions=3,
        deletions=1, branch="task", commit="abc123", digest="digest-1",
        provider_name="fake1", provider_cfg=CFG, approver_map=MAP,
        allowed_approvers=ALLOWED, required=2, requested_by="agent:backend",
        timeout_seconds=10, poll_interval_seconds=5)
    kwargs.update(overrides)

    def worker():
        box["outcome"] = mgr.request(**kwargs)

    t = threading.Thread(target=worker)
    t.start()
    deadline = time.time() + 5
    pending = []
    while time.time() < deadline:
        pending = mgr.pending()
        if pending:
            break
        time.sleep(0.02)
    assert pending, "pending 未出现"
    return t, pending[0]


def approve(mgr: QuorumApprovalManager, rid: str, *ids: str,
            digest: str = "digest-1") -> str:
    return mgr.apply_decision(rid, ProviderDecision(
        status="approved",
        approvers=[ApproverDecision(provider="fake", id=i, decision="approved")
                   for i in ids],
        digest=digest, request_id=rid))


def check_quorum_and_allowlist() -> None:
    with tempfile.TemporaryDirectory() as td:
        mgr, _ = make_manager(Path(td))
        box: dict = {}
        t, pending = start_request(mgr, box)
        assert pending["required"] == 2
        assert pending["approved_count"] == 0
        rid = pending["id"]
        assert approve(mgr, rid, "alice") == "pending"
        # 重复同一 email 不重复计数
        assert approve(mgr, rid, "alice") == "pending"
        # 未登记用户忽略
        assert approve(mgr, rid, "mallory") == "pending"
        assert approve(mgr, rid, "bob") == "approved"
        t.join(5)
        assert not t.is_alive()
        assert box["outcome"].approved
        assert set(box["outcome"].request.approvers) == set(ALLOWED)

        # 拒绝：白名单内一票否决
        box.clear()
        t, pending = start_request(mgr, box, task_id="t2", digest="digest-2")
        rid = pending["id"]
        assert mgr.apply_decision(rid, ProviderDecision(
            status="rejected",
            approvers=[ApproverDecision(provider="fake", id="carol",
                                        decision="rejected")],
            digest="digest-2", request_id=rid)) == "pending"  # carol 不在白名单
        assert mgr.apply_decision(rid, ProviderDecision(
            status="rejected",
            approvers=[ApproverDecision(provider="fake", id="alice",
                                        decision="rejected")],
            digest="digest-2", request_id=rid)) == "rejected"
        t.join(5)
        assert box["outcome"].request.status == "rejected"


def check_callback_hmac() -> None:
    with tempfile.TemporaryDirectory() as td:
        mgr, _ = make_manager(Path(td))
        box: dict = {}
        t, pending = start_request(mgr, box, task_id="t-callback",
                                   digest="digest-cb")
        rid = pending["id"]
        ext = pending["external_id"]
        payload = {
            "decision": "approved", "request_id": rid, "external_id": ext,
            "digest": "digest-cb",
            "approvers": [
                {"provider": "fake", "id": "alice", "name": "Alice",
                 "decision": "approved"},
                {"provider": "fake", "id": "bob", "name": "Bob",
                 "decision": "approved"},
            ],
        }
        raw = json.dumps(payload).encode()
        headers, _ = sign_callback(SECRET, raw)
        assert mgr.apply_callback("fake1", CFG, headers, raw) == "approved"
        t.join(5)
        assert box["outcome"].approved
        assert box["outcome"].request.approved_count == 2

        # 错误签名必须拒绝
        headers_bad, _ = sign_callback("wrong", raw)
        try:
            mgr.apply_callback("fake1", CFG, headers_bad, raw)
            raise AssertionError("错误签名不应通过")
        except Exception as exc:  # ProviderError
            assert "签名" in str(exc)


def check_persistence_and_restart() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        mgr, _ = make_manager(root)
        box: dict = {}
        t, pending = start_request(mgr, box, task_id="t-persist",
                                   digest="digest-p")
        rid = pending["id"]
        assert approve(mgr, rid, "alice", digest="digest-p") == "pending"
        assert approve(mgr, rid, "bob", digest="digest-p") == "approved"
        t.join(5)
        assert box["outcome"].approved
        # 模拟进程重启：从同一 state 目录加载，已批准请求仍在
        mgr2, _ = make_manager(root)
        loaded = mgr2.get(rid)
        assert loaded is not None and loaded.status == "approved"
        assert loaded.approved_count == 2
        # 已批准请求在重启后可直接复用
        mgr3, _ = make_manager(root)
        again = mgr3.request(
            project="p", task_id="t-persist", title="风险任务",
            risk_level="high", reasons=[], files=[], additions=0,
            deletions=0, branch="task", commit="abc123", digest="digest-p",
            provider_name="fake1", provider_cfg=CFG, approver_map=MAP,
            allowed_approvers=ALLOWED, required=2,
            requested_by="agent:backend", timeout_seconds=10)
        assert again.approved


def check_timeout_and_stop() -> None:
    with tempfile.TemporaryDirectory() as td:
        mgr, _ = make_manager(Path(td))
        box: dict = {}
        t, pending = start_request(mgr, box, task_id="t-timeout",
                                   digest="digest-to", timeout_seconds=1)
        t.join(5)
        assert not t.is_alive()
        assert box["outcome"].timed_out
        assert box["outcome"].request.status == "timeout"

        box.clear()
        stop = threading.Event()
        stop.set()
        t, pending = start_request(mgr, box, task_id="t-stop",
                                   digest="digest-stop", stop_event=stop)
        t.join(5)
        assert box["outcome"].cancelled
        assert box["outcome"].request.status == "cancelled"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "orcsy").mkdir(parents=True)
    (repo / "orcsy" / "updater.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "q@example.com")
    _git(repo, "config", "user.name", "Quorum Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "-M", "main")
    _git(repo, "checkout", "-b", "task")
    (repo / "orcsy" / "updater.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "high risk")
    _git(repo, "checkout", "main")
    (repo / "tasks").mkdir()
    (repo / "tasks" / "P1-q.md").write_text(
        "---\ntitle: 会签任务\npriority: P1\nstatus: in_progress\n---\n\n正文\n",
        encoding="utf-8")
    return repo


def check_executor_quorum() -> None:
    from orcsy.config import LLMConfig, ProjectConfig, RiskApprovalConfig
    from orcsy.executor import TaskExecutor, TaskResult
    from orcsy.task_model import parse_task_file
    import orcsy.quorum_approval as qa
    from unittest import mock

    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        cfg = RiskApprovalConfig(
            enabled=True, policy={"low": "off", "medium": "off", "high": "quorum"},
            provider="fake1", quorum={"high": 2},
            allowed_approvers={"high": ALLOWED}, timeout_minutes=1,
            poll_interval_seconds=5)
        proj = ProjectConfig(name="p", path=repo)
        ex = TaskExecutor(
            proj, LLMConfig(api_key="k", base_url="http://localhost:9",
                            model="m"),
            commit_and_merge=True, worktree_isolation=True,
            risk_approval_cfg=cfg,
            approval_providers={"fake1": CFG}, approvers=MAP,
            risk_approval_state_dir=repo / "state")
        task = parse_task_file(repo / "tasks" / "P1-q.md")
        result = TaskResult(task_id=task.id, outcome="blocked")
        mgr, _ = make_manager(repo / "state")
        with mock.patch("orcsy.quorum_approval.get_manager", return_value=mgr):
            box: dict = {}
            t = threading.Thread(target=lambda: box.setdefault(
                "ok", ex._approve_risk_merge(task, result, "task", repo)))
            t.start()
            deadline = time.time() + 5
            while time.time() < deadline and not mgr.pending():
                time.sleep(0.02)
            assert mgr.pending(), "执行器未发起会签"
            pending = mgr.pending()[0]
            rid = pending["id"]
            digest = pending["digest"]
            assert approve(mgr, rid, "alice", digest=digest) == "pending"
            assert approve(mgr, rid, "bob", digest=digest) == "approved"
            t.join(10)
            assert not t.is_alive()
            assert box["ok"] is True, box
            assert result.merge_approval["decision"] == "approved"
            assert result.merge_approval["by"]


def main() -> None:
    check_quorum_and_allowlist()
    check_callback_hmac()
    check_persistence_and_restart()
    check_timeout_and_stop()
    check_executor_quorum()
    print("test_quorum_approval: all checks passed")


if __name__ == "__main__":
    main()
