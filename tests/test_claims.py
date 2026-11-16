"""任务指纹与租约（V1-R2）自检：指纹稳定性、并发认领、TTL/心跳、续跑与提交前校验。

风格与 test_runlog.py 一致：main() + assert，直跑无 pytest。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orcsy.agent import ProjectAgent, recover_interrupted_tasks
from orcsy.claims import ProjectClaims
from orcsy.config import LLMConfig, ProjectConfig
from orcsy.executor import TaskExecutor
from orcsy.task_model import (append_progress, parse_task_file,
                              set_task_file_review_pending, set_task_file_status)


def make_project(root: Path, *, body: str = "实现 demo。\n") -> ProjectConfig:
    proj = root / "proj"
    (proj / "tasks").mkdir(parents=True)
    (proj / "AGENT.md").write_text("# 项目指引\n\n指纹测试。\n", encoding="utf-8")
    (proj / "tasks" / "T-1.md").write_text(
        "---\ntitle: 演示任务\npriority: P2\nstatus: open\n---\n\n" + body,
        encoding="utf-8")
    return ProjectConfig(name="proj", path=proj)


def check_fingerprint_stability() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="claims-fp-"))
    try:
        cfg = make_project(tmp)
        claims = ProjectClaims(cfg, tmp / "state")
        task = parse_task_file(cfg.path / "tasks" / "T-1.md")
        fp = claims.fingerprint(task, model="m1", agent_key="backend")

        # 系统写入（状态/进度/审查标记）不得改变指纹
        set_task_file_status(task.path, "in_progress")
        append_progress(task.path, "第1轮 · read_file")
        set_task_file_review_pending(task.path, True)
        task2 = parse_task_file(task.path)
        assert claims.fingerprint(task2, model="m1", agent_key="backend") == fp

        # 用户改正文 → 变化
        text = task.path.read_text(encoding="utf-8")
        task.path.write_text(text.replace("实现 demo。", "实现 demo（新需求）。"),
                             encoding="utf-8")
        task3 = parse_task_file(task.path)
        assert claims.fingerprint(task3, model="m1", agent_key="backend") != fp

        # 模型 / 角色 / AGENT.md 变化 → 变化
        assert claims.fingerprint(task3, model="m2", agent_key="backend") != \
            claims.fingerprint(task3, model="m1", agent_key="backend")
        assert claims.fingerprint(task3, model="m1", agent_key="frontend") != \
            claims.fingerprint(task3, model="m1", agent_key="backend")
        (cfg.path / "AGENT.md").write_text("# 项目指引\n\n新规则。\n", encoding="utf-8")
        assert claims.fingerprint(task3, model="m1", agent_key="backend") != \
            claims.fingerprint(task3, model="m2", agent_key="backend")

        # 依赖指纹：依赖任务内容变化 → 下游指纹变化
        (cfg.path / "tasks" / "T-0.md").write_text(
            "---\ntitle: 前置\npriority: P2\nstatus: open\n---\n\n先做 A。\n",
            encoding="utf-8")
        (cfg.path / "tasks" / "T-2.md").write_text(
            "---\ntitle: 下游\npriority: P2\nstatus: open\ndepends_on: [T-0]\n---\n\n做 B。\n",
            encoding="utf-8")
        downstream = parse_task_file(cfg.path / "tasks" / "T-2.md")
        before = claims.fingerprint(downstream, model="m", agent_key="")
        (cfg.path / "tasks" / "T-0.md").write_text(
            "---\ntitle: 前置\npriority: P2\nstatus: open\n---\n\n先做 A（改）。\n",
            encoding="utf-8")
        after = claims.fingerprint(downstream, model="m", agent_key="")
        assert before != after, "依赖内容变化应改变下游指纹"
        print("ok: 指纹稳定（系统写入不敏感，正文/依赖/指引/模型敏感）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_lease_exclusive() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="claims-lease-"))
    try:
        cfg = make_project(tmp)
        claims = ProjectClaims(cfg, tmp / "state", ttl=5)
        claim = claims.acquire("T-1", "fp1")
        assert claim, "首次认领应成功"
        assert claims.acquire("T-1", "fp1") is None, "同一任务不能被二次认领"
        ok, reason = claims.verify("T-1", "fp1", claim)
        assert ok and reason == ""
        assert claims.verify("T-1", "fp2", claim)[1] == "fingerprint_changed"
        assert claims.verify("T-1", "fp1", "other")[1] == "lease_owner_changed"
        assert claims.heartbeat("T-1", claim)
        claims.release("T-1", claim)
        assert claims.acquire("T-1", "fp1"), "释放后可重新认领"
        print("ok: 租约独占/指纹绑定/释放重领")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_concurrent_acquire() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="claims-threads-"))
    try:
        cfg = make_project(tmp)
        claims = ProjectClaims(cfg, tmp / "state")
        results: list[str | None] = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def worker() -> None:
            barrier.wait()
            got = claims.acquire("T-1", "fp")
            with lock:
                results.append(got)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [r for r in results if r]
        assert len(winners) == 1, f"并发认领应只有 1 个成功：{results}"
        print("ok: 10 线程并发认领只有 1 个成功")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_ttl_and_heartbeat() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="claims-ttl-"))
    try:
        cfg = make_project(tmp)
        claims = ProjectClaims(cfg, tmp / "state", ttl=0.25, heartbeat=0.05)
        first = claims.acquire("T-1", "fp")
        assert first
        time.sleep(0.35)
        assert claims.acquire("T-1", "fp"), "TTL 过期后应可回收"

        second = claims.acquire("T-9", "fp")
        hb = claims.start_heartbeat("T-9", second)
        time.sleep(0.5)
        assert claims.acquire("T-9", "fp") is None, "心跳应维持租约不过期"
        claims.stop_heartbeat(hb)
        print("ok: TTL 过期回收 + 心跳续租")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_resume_marker_and_recovery() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="claims-resume-"))
    try:
        cfg = make_project(tmp)
        claims = ProjectClaims(cfg, tmp / "state")
        claims.mark_resume("T-1", "review", wip_commit="abc1234")
        assert claims.has_resume("T-1")
        assert claims.resume_info("T-1")["wip_commit"] == "abc1234"
        claims.clear_resume("T-1")
        assert not claims.has_resume("T-1")

        # 恢复：有续跑标记 → open + resume_run=true；无标记 → 普通重跑
        path = cfg.path / "tasks" / "T-1.md"
        set_task_file_status(path, "in_progress")
        claims.acquire("T-1", "fp")
        claims.mark_resume("T-1", "review", wip_commit="abc")
        reset = recover_interrupted_tasks(cfg, claims)
        assert reset == ["T-1"], reset
        task = parse_task_file(path)
        assert task.status == "open" and task.resume_run is True, task

        (cfg.path / "tasks" / "T-2.md").write_text(
            "---\ntitle: 无续跑\npriority: P2\nstatus: in_progress\n---\n\n正文。\n",
            encoding="utf-8")
        recover_interrupted_tasks(cfg, claims)
        task2 = parse_task_file(cfg.path / "tasks" / "T-2.md")
        assert task2.status == "open" and task2.resume_run is False
        print("ok: 续跑标记 + 启动恢复（保留/普通两条路径）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_commit_guard() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="claims-guard-"))
    try:
        cfg = make_project(tmp)
        claims = ProjectClaims(cfg, tmp / "state")
        llm = LLMConfig(api_key="k", base_url="http://localhost:9", model="m")
        ex = TaskExecutor(cfg, llm, claims=claims)
        task = parse_task_file(cfg.path / "tasks" / "T-1.md")
        fp = claims.fingerprint(task, model="m", agent_key="")
        task.claim_id = claims.acquire(task.id, fp)
        assert ex._verify_claim(task)[0]

        # 运行中用户改了任务正文 → 提交前校验失败
        text = task.path.read_text(encoding="utf-8")
        task.path.write_text(text.replace("实现 demo。", "改成别的。"), encoding="utf-8")
        changed = parse_task_file(task.path)
        changed.claim_id = task.claim_id
        ok, reason = ex._verify_claim(changed)
        assert not ok and reason == "fingerprint_changed", (ok, reason)

        # 租约被释放/易主 → 也失败
        claims.release(task.id, task.claim_id)
        assert ex._verify_claim(task)[1] == "lease_missing"
        print("ok: 提交前二次校验（正文变化/租约丢失）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_scan_twice_does_not_rerun() -> None:
    """真实 ProjectAgent 扫描：首轮执行并归档，后续 100 轮不再触发模型调用。"""
    from e2e_fake_llm import FakeLLMClient, make_project as e2e_make_project, make_task

    tmp = Path(tempfile.mkdtemp(prefix="claims-scan-"))
    try:
        cfg = e2e_make_project(tmp)
        make_task(cfg.path)
        task_path = cfg.path / "tasks" / "P1-fix-greet-crash.md"
        FakeLLMClient.script = [
            ("list_dir", {"path": "."}),
            ("read_file", {"path": "app.py"}),
            ("edit_file", {
                "path": "app.py",
                "old_string": 'def greet(name):\n    # TODO: 空名字会崩溃\n'
                              '    return "Hello, " + name.upper()',
                "new_string": 'def greet(name):\n'
                              '    if not name:\n'
                              '        return "Hello, stranger"\n'
                              '    return "Hello, " + name.upper()',
            }),
            ("finish", {"action": "completed", "summary": "修复完成",
                        "files_changed": ["app.py"],
                        "commit_message": "fix: greet"}),
        ]
        FakeLLMClient.calls = []
        llm = LLMConfig(api_key="k", base_url="http://localhost:9", model="m")
        agent = ProjectAgent(cfg, llm, state_dir=tmp / "state")
        agent.executor.run_task.__globals__["LLMClient"] = FakeLLMClient
        results = agent.scan_and_work()
        assert results and results[0].outcome == "done", results
        assert (cfg.path / "tasks" / "done" / task_path.name).is_file()
        first_calls = len(FakeLLMClient.calls)
        assert first_calls > 0

        for _ in range(100):
            more = agent.scan_and_work()
            assert not more, "任务已归档，后续扫描不应再触发执行"
        assert len(FakeLLMClient.calls) == first_calls, "模型调用不应重复"
        snap = agent.claims.snapshot()
        assert not snap["leases"], snap
        assert not snap["resumes"], snap
        print("ok: 真实扫描 1 次执行 + 100 次空扫描（无重复调用/租约已释放）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_scan_stale_claim_blocks() -> None:
    """执行中任务正文被改写 → 提交前二次校验失败，结果 blocked 且不落地 done。"""
    from e2e_fake_llm import FakeLLMClient, make_project as e2e_make_project, make_task

    tmp = Path(tempfile.mkdtemp(prefix="claims-stale-"))
    try:
        cfg = e2e_make_project(tmp)
        make_task(cfg.path)
        task_path = cfg.path / "tasks" / "P1-fix-greet-crash.md"
        original = task_path.read_text(encoding="utf-8")
        rewritten = original.replace("空名字调用 greet", "模型改写了任务正文：空名字调用 greet")
        FakeLLMClient.script = [
            ("write_file", {"path": "tasks/P1-fix-greet-crash.md",
                            "content": rewritten}),
            ("finish", {"action": "completed", "summary": "完成",
                        "files_changed": ["tasks/P1-fix-greet-crash.md"],
                        "commit_message": "chore: tweak"}),
        ]
        FakeLLMClient.calls = []
        llm = LLMConfig(api_key="k", base_url="http://localhost:9", model="m")
        agent = ProjectAgent(cfg, llm, state_dir=tmp / "state")
        agent.executor.run_task.__globals__["LLMClient"] = FakeLLMClient
        results = agent.scan_and_work()
        assert results, "应产生一条执行结果"
        res = results[0]
        assert res.outcome == "blocked", res.outcome
        assert "stale_lease" in (res.notes or ""), res.notes
        task = parse_task_file(task_path)
        assert task.status == "blocked", task.status
        print("ok: 执行中任务被改写 → stale_lease 阻断提交")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_pending_review_stale() -> None:
    """待审任务在等待期间被改：补审不得直接合并旧 WIP。"""
    tmp = Path(tempfile.mkdtemp(prefix="claims-pending-"))
    try:
        cfg = make_project(tmp)
        llm = LLMConfig(api_key="k", base_url="http://localhost:9", model="m")
        agent = ProjectAgent(cfg, llm, state_dir=tmp / "state")
        path = cfg.path / "tasks" / "T-1.md"
        task = parse_task_file(path)
        fp = agent.claims.fingerprint(task, model="m", agent_key="")
        agent.claims.mark_resume(task.id, "review", wip_commit="abc",
                                 fingerprint=fp)
        set_task_file_review_pending(path, True)
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("实现 demo。", "等待审查期间改成新需求。"),
                        encoding="utf-8")
        changed = parse_task_file(path)
        bad = agent._review_pending_task(changed)
        assert bad is not None and bad.outcome == "blocked", bad
        assert "stale_lease" in (bad.notes or ""), bad.notes
        print("ok: 待审任务被改写 → 补审阻断 stale_lease")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_merged_resume_archives() -> None:
    """合并后崩溃：恢复时直接归档，不再调用模型/不重复提交。"""
    from e2e_fake_llm import FakeLLMClient, make_project as e2e_make_project, make_task
    from orcsy.task_model import set_task_file_resume

    tmp = Path(tempfile.mkdtemp(prefix="claims-merged-"))
    try:
        cfg = e2e_make_project(tmp)
        make_task(cfg.path)
        path = cfg.path / "tasks" / "P1-fix-greet-crash.md"
        task = parse_task_file(path)
        llm = LLMConfig(api_key="k", base_url="http://localhost:9", model="m")
        agent = ProjectAgent(cfg, llm, state_dir=tmp / "state")
        fp = agent.claims.fingerprint(task, model="m", agent_key="")
        agent.claims.mark_resume(task.id, "merged", wip_commit="abc",
                                 fingerprint=fp)
        set_task_file_resume(path, True)
        FakeLLMClient.script = []
        FakeLLMClient.calls = []
        results = agent.scan_and_work()
        assert results and results[0].outcome == "done", results
        assert not FakeLLMClient.calls, "合并恢复不应再调用模型"
        assert (cfg.path / "tasks" / "done" / path.name).is_file()
        assert not agent.claims.snapshot()["resumes"]
        print("ok: 合并后崩溃恢复 → 直接归档（零模型调用）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_fingerprint_stability()
    check_lease_exclusive()
    check_concurrent_acquire()
    check_ttl_and_heartbeat()
    check_resume_marker_and_recovery()
    check_commit_guard()
    check_scan_twice_does_not_rerun()
    check_scan_stale_claim_blocks()
    check_pending_review_stale()
    check_merged_resume_archives()
    print("\n任务指纹与租约自检全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
