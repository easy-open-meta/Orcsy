"""工具循环轮数上限自检：-1 = 不设上限（真实循环跑到 finish 为止）、
有限轮数耗尽强制收尾、配置解析与 Web 校验。

手法与 e2e_task_stop 相同：patch LLMClient._post 驱动真实 run_task_loop。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import LLMConfig, ProjectConfig, load_config
from orcsy.executor import TaskExecutor
from orcsy.task_model import parse_task_file
from orcsy.web import AppState
from orcsy.scheduler import Scheduler


def make_project(root: Path, name: str) -> ProjectConfig:
    proj = root / name
    proj.mkdir(parents=True)
    (proj / "app.py").write_text("value = 1\n", encoding="utf-8")
    (proj / "AGENT.md").write_text("# 项目指引\n- 小步修改\n", encoding="utf-8")
    (proj / "tasks").mkdir()
    (proj / "tasks" / "P1-t.md").write_text(
        "---\ntitle: 轮数测试\npriority: P1\nstatus: open\n---\n\n随便看看。\n",
        encoding="utf-8")
    return ProjectConfig(name=name, path=proj)


def tool_call_response(cid: str, name: str, args: dict) -> dict:
    return {"choices": [{
        "message": {"role": "assistant", "content": None,
                    "tool_calls": [{"id": cid, "type": "function",
                                    "function": {"name": name,
                                                 "arguments": json.dumps(args)}}],
                    }, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="turns-"))
    try:
        proj = make_project(tmp, "turnsproj")
        task = parse_task_file(proj.path / "tasks" / "P1-t.md")
        llm_cfg = LLMConfig(api_key="fake", base_url="http://localhost:9", model="fake")

        from orcsy.llm import LLMClient
        original_post = LLMClient._post
        try:
            # ---- 场景 1：max_turns=-1 不设上限——连跑 5 轮 list_dir 后 finish 正常收尾 ----
            state = {"calls": 0}

            def fake_unlimited(self, url, payload, headers):
                state["calls"] += 1
                if state["calls"] <= 5:
                    return tool_call_response(f"c{state['calls']}", "list_dir", {"path": "."})
                return tool_call_response("cf", "finish",
                                          {"action": "completed", "summary": "看完结构了",
                                           "files_changed": [], "commit_message": ""})
            LLMClient._post = fake_unlimited
            proj.max_agent_turns = -1
            executor = TaskExecutor(proj, llm_cfg)
            result = executor.run_task(task, guide_content="# 项目指引\n- 小步修改\n")
            assert result.outcome == "done", (result.outcome, result.notes)
            assert result.turns == 6, result.turns  # 5 轮工具 + finish 轮
            assert state["calls"] == 6
            print("ok: max_turns=-1 不设上限——循环跑到 finish 为止（6 轮）")

            # ---- 场景 2：max_turns=2 且模型永不 finish → 耗尽强制收尾 ----
            state2 = {"calls": 0}

            def fake_never_finish(self, url, payload, headers):
                state2["calls"] += 1
                return tool_call_response(f"c{state2['calls']}", "list_dir", {"path": "."})
            LLMClient._post = fake_never_finish
            proj.max_agent_turns = 2
            executor2 = TaskExecutor(proj, llm_cfg)
            result2 = executor2.run_task(task, guide_content="# 项目指引\n- 小步修改\n")
            assert result2.outcome == "blocked", result2.outcome
            # 文案随「强制收尾」重构为「已达到最大工作轮数 N，模型未给出结论」
            assert "最大工作轮数 2" in (result2.notes or ""), result2.notes
            assert state2["calls"] == 3, state2["calls"]  # 2 轮 + 1 次强制收尾请求
            print("ok: max_turns=2 有限上限——耗尽后强制收尾为 blocked")
        finally:
            LLMClient._post = original_post

        # ---- 场景 3：配置解析 -1；Web 校验接受 -1、拒绝 0/4 ----
        bare = tmp / "config.yaml"
        bare.write_text(
            "llm:\n  api_key: k\n  model: m\n"
            "projects:\n"
            f"  - name: p1\n    path: {(tmp / 'turnsproj').as_posix()}\n"
            "    max_agent_turns: -1\n", encoding="utf-8")
        cfg = load_config(bare, require_api_key=False)
        assert cfg.projects[0].max_agent_turns == -1

        cfg_file = tmp / "web.yaml"
        cfg_file.write_text("llm: {}\n", encoding="utf-8")
        proj_dir = tmp / "turnsproj"
        from orcsy.config import AppConfig as _AppConfig
        app_cfg = _AppConfig(
            llm=LLMConfig(api_key="fake", base_url="http://localhost:9", model="fake"),
            source_path=cfg_file)
        state_app = AppState(app_cfg, Scheduler(app_cfg))
        r = state_app.save_project({"name": "w1", "path": str(proj_dir),
                                    "max_agent_turns": -1})
        assert r["ok"] is True
        assert state_app.config.projects[0].max_agent_turns == -1
        for bad in (0, 4, -2):
            try:
                state_app.save_project({"name": "w1", "path": str(proj_dir),
                                        "original_name": "w1", "max_agent_turns": bad})
                raise AssertionError(f"应拒绝：{bad}")
            except ValueError:
                pass
        print("ok: 配置解析 -1 + Web 校验（-1 放行，0/4/-2 拒绝）")

        print("\n轮数上限自检全部通过 ✅")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
