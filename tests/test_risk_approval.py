"""Risk approval engine, executor gate and task writeback."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.agent import ProjectAgent
from orcsy.config import LLMConfig, ProjectConfig, RiskApprovalConfig
from orcsy.executor import TaskExecutor, TaskResult
from orcsy.risk import RiskAssessment
from orcsy.risk_approval import (RiskApprovalManager, RiskApprovalOutcome,
                                 RiskApprovalRequest)
from orcsy.task_model import parse_task_file


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "orcsy").mkdir(parents=True)
    (repo / "orcsy" / "executor.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "orcsy" / "updater.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "risk@example.com")
    _git(repo, "config", "user.name", "Risk Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "-M", "main")
    _git(repo, "checkout", "-b", "task")
    (repo / "orcsy" / "updater.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "high risk change")
    _git(repo, "checkout", "main")
    (repo / "tasks").mkdir()
    (repo / "tasks" / "P1-risk.md").write_text(
        "---\ntitle: 风险任务\npriority: P1\nstatus: in_progress\n---\n\n"
        "## 需求\n\n保留原文 ABC\n", encoding="utf-8")
    return repo


def make_executor(repo: Path) -> TaskExecutor:
    proj = ProjectConfig(name="p", path=repo)
    cfg = RiskApprovalConfig(
        enabled=True, policy={"low": "off", "medium": "off", "high": "single"},
        timeout_minutes=1)
    return TaskExecutor(
        proj, LLMConfig(api_key="k", base_url="http://localhost:9", model="m"),
        commit_and_merge=True, worktree_isolation=True,
        risk_approval_cfg=cfg)


def fake_request(*, status: str, digest: str = "digest-a") -> RiskApprovalRequest:
    req = RiskApprovalRequest(
        id="req-1", project="p", task_id="P1-risk", title="风险任务",
        risk_level="high", digest=digest, timeout_seconds=60,
        status=status, decided_by="user" if status == "approved" else "",
        comment="looks good" if status == "approved" else "",
        decided_at=time.time() if status in ("approved", "rejected") else 0.0)
    return req


def check_manager() -> None:
    mgr = RiskApprovalManager()
    box: dict = {}

    def worker():
        box["outcome"] = mgr.request(
            project="p", task_id="t", title="title", risk_level="high",
            reasons=["r"], files=["a.py"], additions=1, deletions=2,
            branch="task", commit="abc", digest="d", timeout_seconds=10,
            requested_by="agent:backend")

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
    for key in ("id", "project", "task_id", "title", "risk_level", "reasons",
                "files", "additions", "deletions", "branch", "commit",
                "digest", "requested_by", "created_at", "timeout_seconds",
                "waited_seconds", "status"):
        assert key in pending[0], f"pending 缺字段 {key}"
    assert pending[0]["risk_level"] == "high"
    assert mgr.respond(pending[0]["id"], True, comment="ok", actor="user")
    assert not mgr.respond(pending[0]["id"], True, actor="user"), "重复响应应失败"
    t.join(5)
    assert not t.is_alive()
    outcome = box["outcome"]
    assert outcome.approved and not outcome.timed_out
    assert outcome.request.decision == "approved"
    assert outcome.request.decided_by == "user"

    # 拒绝
    box.clear()
    t = threading.Thread(target=worker)
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline and not mgr.pending():
        time.sleep(0.02)
    rid = mgr.pending()[0]["id"]
    assert mgr.respond(rid, False, comment="no", actor="user")
    t.join(5)
    assert not box["outcome"].approved and not box["outcome"].timed_out
    assert box["outcome"].request.status == "rejected"

    # 超时
    box.clear()

    def slow():
        box["outcome"] = mgr.request(
            project="p", task_id="t", title="title", risk_level="high",
            reasons=[], files=[], additions=0, deletions=0, branch="task",
            commit="abc", digest="d", timeout_seconds=1, requested_by="agent")

    t = threading.Thread(target=slow)
    t.start()
    t.join(5)
    assert box["outcome"].timed_out, box["outcome"]

    # 停止信号取消
    stop = threading.Event()
    stop.set()
    box.clear()

    def cancelled():
        box["outcome"] = mgr.request(
            project="p", task_id="t2", title="title", risk_level="high",
            reasons=[], files=[], additions=0, deletions=0, branch="task",
            commit="abc", digest="d", timeout_seconds=10, requested_by="agent",
            stop_event=stop)

    t = threading.Thread(target=cancelled)
    t.start()
    t.join(5)
    assert box["outcome"].cancelled, box["outcome"]
    mgr.clear()


def check_executor_approve_and_reject() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        ex = make_executor(repo)
        task = parse_task_file(repo / "tasks" / "P1-risk.md")

        mgr = RiskApprovalManager()
        result = TaskResult(task_id=task.id, outcome="blocked")
        with mock.patch("orcsy.risk_approval.get_manager", return_value=mgr):
            box: dict = {}
            t = threading.Thread(target=lambda: box.setdefault(
                "ok", ex._approve_risk_merge(task, result, "task", repo)))
            t.start()
            deadline = time.time() + 5
            pending = []
            while time.time() < deadline:
                pending = mgr.pending()
                if pending:
                    break
                time.sleep(0.02)
            assert pending, "executor 未发起风险审批"
            assert pending[0]["risk_level"] == "high"
            assert mgr.respond(pending[0]["id"], True, actor="user")
            t.join(10)
            assert box["ok"] is True, box
            assert result.risk is not None and result.risk.level == "high"
            assert result.merge_approval["decision"] == "approved"

        mgr = RiskApprovalManager()
        result2 = TaskResult(task_id=task.id, outcome="blocked")
        with mock.patch("orcsy.risk_approval.get_manager", return_value=mgr):
            box.clear()
            t = threading.Thread(target=lambda: box.setdefault(
                "ok", ex._approve_risk_merge(task, result2, "task", repo)))
            t.start()
            deadline = time.time() + 5
            while time.time() < deadline and not mgr.pending():
                time.sleep(0.02)
            assert mgr.pending()
            assert mgr.respond(mgr.pending()[0]["id"], False,
                               comment="no", actor="user")
            t.join(10)
            assert box["ok"] is False
            assert result2.block_reason == "risk_approval_rejected"
            assert "风险审批被拒绝" in result2.notes
            assert result2.merge_approval["decision"] == "rejected"


def check_timeout_and_stale() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        ex = make_executor(repo)
        task = parse_task_file(repo / "tasks" / "P1-risk.md")

        # 超时：stub manager 直接返回 timed_out
        req = fake_request(status="timeout")
        stub = mock.Mock()
        stub.request.return_value = RiskApprovalOutcome(False, True, req)
        result = TaskResult(task_id=task.id, outcome="blocked")
        with mock.patch("orcsy.risk_approval.get_manager", return_value=stub):
            assert ex._approve_risk_merge(task, result, "task", repo) is False
        assert result.block_reason == "risk_approval_timeout"
        assert "风险审批超时" in result.notes

        # 批准后内容变化 → stale
        req = fake_request(status="approved", digest="digest-a")
        stub = mock.Mock()
        stub.request.return_value = RiskApprovalOutcome(True, False, req)
        first = RiskAssessment(level="high", reasons=["r"], files=["a.py"],
                               digest="digest-a", branch_sha="abc")
        second = RiskAssessment(level="high", reasons=["r"], files=["a.py"],
                                digest="digest-b", branch_sha="abc")
        result2 = TaskResult(task_id=task.id, outcome="blocked")
        with mock.patch("orcsy.risk_approval.get_manager", return_value=stub), \
                mock.patch.object(ex, "_assess_merge_risk",
                                  side_effect=[first, second]):
            assert ex._approve_risk_merge(task, result2, "task", repo) is False
        assert result2.block_reason == "risk_approval_stale"
        assert result2.merge_approval["decision"] == "stale"


def check_full_merge_after_approval() -> None:
    """批准后真实走完 _merge_with_premerge 的合入路径。"""
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        ex = make_executor(repo)
        task = parse_task_file(repo / "tasks" / "P1-risk.md")
        result = TaskResult(task_id=task.id, outcome="blocked")
        mgr = RiskApprovalManager()
        with mock.patch("orcsy.risk_approval.get_manager", return_value=mgr):
            box: dict = {}
            t = threading.Thread(target=lambda: box.setdefault(
                "merged", ex._merge_with_premerge(task, result, "task", repo)))
            t.start()
            deadline = time.time() + 5
            while time.time() < deadline and not mgr.pending():
                time.sleep(0.02)
            assert mgr.pending(), "合入路径未发起风险审批"
            assert mgr.respond(mgr.pending()[0]["id"], True, actor="user")
            t.join(15)
            assert not t.is_alive()
            assert box["merged"] is True, box
        assert (repo / "orcsy" / "updater.py").read_text(
            encoding="utf-8") == "x = 2\n", "批准后未合入 task 分支内容"


def check_merge_gate_blocks_before_merge() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        ex = make_executor(repo)
        task = parse_task_file(repo / "tasks" / "P1-risk.md")
        result = TaskResult(task_id=task.id, outcome="blocked")
        with mock.patch.object(ex, "_approve_risk_merge", return_value=False), \
                mock.patch.object(ex, "_merge_and_push") as merge:
            assert ex._merge_with_premerge(task, result, "task", repo) is None
            assert not merge.called, "审批未通过时不得调用合并"


def check_writeback_section() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        proj = ProjectConfig(name="p", path=repo)
        agent = ProjectAgent(proj, LLMConfig(api_key="k",
                                             base_url="http://localhost:9",
                                             model="m"),
                             state_dir=repo / "state")
        task_file = repo / "tasks" / "P1-risk.md"
        task = parse_task_file(task_file)
        risk = RiskAssessment(level="high", reasons=["自更新逻辑：orcsy/updater.py"],
                              files=["orcsy/updater.py"], additions=1,
                              deletions=1, digest="abcdef0123456789")
        result = TaskResult(
            task_id=task.id, outcome="blocked",
            block_reason="risk_approval_rejected", notes="风险审批被拒绝",
            risk=risk,
            merge_approval={"decision": "rejected", "by": "user",
                            "comment": "不要改自更新", "at": "2026-01-01 00:00:00",
                            "digest": "abcdef0123456789", "commit": "deadbeef"})
        agent._write_back_result(task, result)
        text = task_file.read_text(encoding="utf-8")
        assert "status: blocked" in text
        assert "block_reason: risk_approval_rejected" in text
        assert "## 风险审批" in text
        assert "已拒绝" in text and "不要改自更新" in text
        assert "保留原文 ABC" in text, "风险节不应覆盖任务原文"
        assert "## Agent 处理结果" in text


def main() -> None:
    check_manager()
    check_executor_approve_and_reject()
    check_timeout_and_stale()
    check_full_merge_after_approval()
    check_merge_gate_blocks_before_merge()
    check_writeback_section()
    print("test_risk_approval: all checks passed")


if __name__ == "__main__":
    main()
