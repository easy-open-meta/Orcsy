"""强制停止端到端测试：真实工具循环执行长命令，看板置停止信号，
验证子进程被杀、任务收尾为 cancelled、进度留痕、非运行任务报 400。

手法与 e2e_project_import 相同：patch LLMClient._post 用脚本化响应驱动真实
run_task_loop（第一轮返回 run_command 长命令的工具调用）。
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

from orcsy.config import LLMConfig, ProjectConfig
from orcsy.executor import TaskExecutor
from orcsy.task_control import get_task_controls
from orcsy.tools import _CHILD_PROCS
from orcsy.web import AppState
from orcsy.scheduler import Scheduler

COMMAND = ["python", "-c", "import time; time.sleep(30)"]


def make_project(root: Path, name: str) -> ProjectConfig:
    proj = root / name
    proj.mkdir(parents=True)
    (proj / "app.py").write_text("value = 1\n", encoding="utf-8")
    (proj / "AGENT.md").write_text("# 项目指引\n- 小步修改\n", encoding="utf-8")
    (proj / "tasks").mkdir()
    (proj / "tasks" / "P1-stop-me.md").write_text(
        "---\ntitle: 停止测试任务\npriority: P1\nstatus: open\n---\n\n随便干点啥。\n",
        encoding="utf-8")
    return ProjectConfig(name=name, path=proj)


def fake_post_factory(state: dict):
    """第 1 轮返回 run_command 长命令；之后返回 finish（正常不应被走到）。"""
    def fake_post(self, url, payload, headers):
        state["calls"] += 1
        if state["calls"] == 1:
            return {"choices": [{
                "message": {"role": "assistant", "content": None,
                            "tool_calls": [{"id": "c1", "type": "function",
                                            "function": {"name": "run_command",
                                                         "arguments": json.dumps({"command": COMMAND})}}],
                            }, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        return {"choices": [{
            "message": {"role": "assistant", "content": None,
                        "tool_calls": [{"id": "c2", "type": "function",
                                        "function": {"name": "finish",
                                                     "arguments": json.dumps(
                                                         {"action": "completed", "summary": "不该走到"})}}],
                        }, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    return fake_post


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="stop-e2e-"))
    try:
        proj = make_project(tmp, "stopproj")
        from orcsy.task_model import parse_task_file
        task = parse_task_file(proj.path / "tasks" / "P1-stop-me.md")
        llm_cfg = LLMConfig(api_key="fake", base_url="http://localhost:9", model="fake")
        executor = TaskExecutor(proj, llm_cfg)

        state = {"calls": 0}
        from orcsy.llm import LLMClient
        original_post = LLMClient._post
        LLMClient._post = fake_post_factory(state)
        try:
            box: dict = {}

            def worker():
                box["result"] = executor.run_task(task, guide_content="# 项目指引\n- 小步修改\n")
            th = threading.Thread(target=worker)
            th.start()

            # 等子进程真的跑起来（tools._CHILD_PROCS 出现该命令）
            deadline = time.time() + 10
            while time.time() < deadline and not _CHILD_PROCS:
                time.sleep(0.05)
            assert _CHILD_PROCS, "子进程应已启动"

            controls = get_task_controls()
            # 停止信号置位：子进程被杀、TaskStopped 冒到执行器
            assert controls.stop("stopproj", "P1-stop-me") is True
            th.join(20)
            assert not th.is_alive(), "停止后任务线程应在数秒内结束"
            result = box["result"]
            assert result.outcome == "cancelled", (result.outcome, result.notes)
            assert "强制停止" in result.notes, result.notes
            assert "finish" not in str(state), "停止后不应再调用模型"
            # 子进程已被终止（sleep 30 不可能自然结束）
            time.sleep(0.3)
            assert all(p.poll() is not None for p in list(_CHILD_PROCS)), "子进程应已被杀"
            assert "⏹ 已被用户在看板强制停止" in task.path.read_text(encoding="utf-8")
            assert not (proj.path / ".worktree-arranger").exists(), "worktree 应已清理"
            print("ok: 停止信号 → 杀子进程 → cancelled 收尾 + 进度留痕")
        finally:
            LLMClient._post = original_post
            get_task_controls().clear()

        # Web 层：非运行任务报错、未知项目报错
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text("llm: {}\n", encoding="utf-8")
        from orcsy.config import AppConfig, LLMConfig as _LLM
        cfg = AppConfig(llm=_LLM(api_key="fake", base_url="http://localhost:9",
                                 model="fake"),
                        source_path=cfg_file)
        app = AppState(cfg, Scheduler(cfg))
        for bad in ({"project": "stopproj", "id": "P1-stop-me"},
                    {"project": "nope", "id": "x"},
                    {"project": "stopproj", "id": ""}):
            try:
                app.stop_task(bad)
                raise AssertionError(f"应拒绝：{bad}")
            except ValueError:
                pass
        print("ok: 非运行任务/未知项目/缺 id 均 400")

        print("\n强制停止端到端全部通过 ✅")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
