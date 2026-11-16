"""并行波次回写时序：已完成任务立即回写，不等同波慢任务结束。"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.agent import ProjectAgent
from orcsy.config import (LLMConfig, ProjectConfig, ProjectTeamConfig,
                          TeamMemberConfig)
from orcsy.executor import TaskResult


def _write_task(tasks: Path, task_id: str, assignee: str) -> None:
    (tasks / f"{task_id}.md").write_text(
        "---\n"
        f"title: {task_id}\n"
        "priority: P1\n"
        "status: open\n"
        f"assignee: {assignee}\n"
        "---\n\n正文\n",
        encoding="utf-8")


def test_fast_sibling_written_back_before_slow_finishes() -> None:
    """A 睡 1.2s、B 立即完成：B 应在 A 仍执行时已归档，而不是等整波结束。"""
    tmp = Path(tempfile.mkdtemp(prefix="wave-writeback-"))
    try:
        proj = tmp / "proj"
        tasks = proj / "tasks"
        tasks.mkdir(parents=True)
        (proj / "AGENT.md").write_text("# guide\n", encoding="utf-8")
        _write_task(tasks, "A-slow", "backend")
        _write_task(tasks, "B-fast", "frontend")

        project = ProjectConfig(
            name="p", path=proj, max_tasks_per_cycle=2,
            team=ProjectTeamConfig(
                enabled=True, execution_mode="parallel_worktree",
                chain_within_cycle=False, max_chain_steps_per_cycle=2,
                members=[
                    TeamMemberConfig(id="backend", role="backend"),
                    TeamMemberConfig(id="frontend", role="frontend"),
                ],
            ))
        agent = ProjectAgent(
            project,
            LLMConfig(api_key="k", base_url="http://localhost:9", model="m"),
            state_dir=tmp / "state")
        # 避免测试环境没有 git 仓库：并行判定只看这个探针
        agent.executor.probe_git_repo = lambda: True  # type: ignore[method-assign]

        def fake_run(task, member_id, executor, guide, skills):
            if task.id == "A-slow":
                time.sleep(1.2)
            return task, member_id, TaskResult(
                task_id=task.id, outcome="done",
                role=member_id, agent_name=member_id)

        agent._run_member_task = fake_run  # type: ignore[method-assign]
        worker = threading.Thread(target=agent.scan_and_work, daemon=True)
        worker.start()

        fast_done_early = False
        alive_at_fast = False
        slow_running_at_fast = False
        for _ in range(60):
            time.sleep(0.05)
            if ((tasks / "done" / "B-fast.md").is_file()
                    and not (tasks / "B-fast.md").exists()):
                fast_done_early = True
                alive_at_fast = worker.is_alive()
                slow_running_at_fast = (tasks / "A-slow.md").exists()
                break
        assert fast_done_early, "快任务应在其完成后立即回写/归档"
        assert alive_at_fast and slow_running_at_fast, \
            "快任务归档时慢任务应仍在执行（证明未等整波结束）"

        worker.join(5)
        assert (tasks / "done" / "A-slow.md").is_file()
        assert not (tasks / "A-slow.md").exists()
        print("ok: 并行波次完成即回写（快任务不等待慢任务）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    test_fast_sibling_written_back_before_slow_finishes()
    print("\n并行波次回写时序自检通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
