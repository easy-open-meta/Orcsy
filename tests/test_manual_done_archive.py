"""手工标记完成的任务归档一致性回归。

背景：看板「标记完成」此前只改 frontmatter 的 status，文件留在 tasks/ 根目录，
导致它出现在任务列表（已完成）而不出现在「已归档」列表（该列表只读
tasks/done/ 目录）。修复后：

- 手工标 done → 文件归档进 tasks/done/，与 Agent 完成走同一落点；
- 已归档任务允许「重新入队」→ 文件回迁 tasks/ 根目录、状态 open、
  带 requeue_seq 插队戳；
- 归档列表（full_state.archived）立即包含手工完成的任务。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import load_config
from orcsy.scheduler import Scheduler
from orcsy.web import AppState


def _make_env() -> tuple[Path, AppState]:
    tmp = Path(tempfile.mkdtemp(prefix="manual-done-"))
    proj = tmp / "proj"
    proj.mkdir(parents=True)
    (proj / "AGENT.md").write_text("# Guide\n", encoding="utf-8")
    cfg_file = tmp / "config.yaml"
    cfg_file.write_text(
        f"""
llm:
  provider: openai
  api_key: sk-test
  model: test
projects:
  - name: proj
    path: {proj.as_posix()}
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file, require_api_key=False)
    state = AppState(cfg, Scheduler(cfg))
    return tmp, state


def check_manual_done_archives() -> None:
    tmp, state = _make_env()
    try:
        agent = state.agents["proj"]
        tasks_dir = agent.tasks_dir
        tasks_dir.mkdir(parents=True, exist_ok=True)
        (tasks_dir / "a-task.md").write_text(
            "---\ntitle: 手工完成测试\npriority: P2\nstatus: open\n---\n\n正文\n",
            encoding="utf-8")

        # 前置：任务列表可见、归档为空
        fs = state.full_state()["projects"][0]
        assert any(t["id"] == "a-task" for t in fs["tasks"]), fs["tasks"]
        assert fs["archived"] == [], fs["archived"]

        # 手工标记完成
        res = state.set_task_status({"project": "proj", "id": "a-task", "status": "done"})
        assert res["ok"], res

        # 文件已归档到 tasks/done/，根目录不再保留
        done_file = tasks_dir / "done" / "a-task.md"
        assert done_file.is_file(), "任务文件应归档到 tasks/done/"
        assert not (tasks_dir / "a-task.md").exists(), "根目录不应残留原文件"

        # 归档列表立即可见，任务列表不再包含
        fs2 = state.full_state()["projects"][0]
        assert any(t["id"] == "a-task" for t in fs2["archived"]), fs2["archived"]
        assert not any(t["id"] == "a-task" and not t.get("archived")
                       for t in fs2["tasks"]), fs2["tasks"]

        # 依赖状态映射仍认它为 done（下游依赖判断不受影响）
        from orcsy.task_model import collect_task_statuses
        statuses, done_ids = collect_task_statuses(tasks_dir)
        assert statuses.get("a-task") == "done" and "a-task" in done_ids

        # 重新入队：文件回迁根目录、状态 open、带插队戳
        res2 = state.set_task_status({"project": "proj", "id": "a-task", "status": "open"})
        assert res2["ok"], res2
        assert (tasks_dir / "a-task.md").is_file(), "重开任务应回迁到 tasks/ 根目录"
        assert not done_file.exists(), "done/ 不应残留副本"
        reopened = (tasks_dir / "a-task.md").read_text(encoding="utf-8")
        assert "status: open" in reopened and "requeue_seq: 1" in reopened, reopened[:200]
        print("ok: 手工标 done 归档进 tasks/done/ / 归档列表可见 / 重开回迁")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_cancelled_stays_and_archive_conflict() -> None:
    tmp, state = _make_env()
    try:
        tasks_dir = state.agents["proj"].tasks_dir
        tasks_dir.mkdir(parents=True, exist_ok=True)
        (tasks_dir / "b-task.md").write_text(
            "---\ntitle: 取消测试\npriority: P2\nstatus: open\n---\n\nx\n",
            encoding="utf-8")
        # cancelled 留在原地（便于重新入队），不归档
        state.set_task_status({"project": "proj", "id": "b-task", "status": "cancelled"})
        assert (tasks_dir / "b-task.md").is_file(), "cancelled 应留在 tasks/ 根目录"
        assert not (tasks_dir / "done" / "b-task.md").exists()

        # done/ 同名冲突时归档跳过、不覆盖已归档内容
        (tasks_dir / "done").mkdir(exist_ok=True)
        (tasks_dir / "done" / "b-task.md").write_text(
            "---\ntitle: 已有归档\npriority: P2\nstatus: done\n---\n\n旧档\n",
            encoding="utf-8")
        state.set_task_status({"project": "proj", "id": "b-task", "status": "done"})
        archived = (tasks_dir / "done" / "b-task.md").read_text(encoding="utf-8")
        assert "旧档" in archived, "同名归档不应被覆盖"
        print("ok: cancelled 留在原地 / done/ 同名冲突不覆盖")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_manual_done_archives()
    check_cancelled_stays_and_archive_conflict()
    print("\n手工完成归档一致性全部测试通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
