"""人工审批端到端测试：模拟 LLM 驱动真实执行链路，任务线程挂起等待审批，
主线程扮演看板响应（批准 / 拒绝 / 始终允许），验证结果与进度留痕。

注入手法与 e2e_fake_llm.py 相同：替换 executor 模块全局的 LLMClient。
区别：替身不断言「工具不得报错」——审批被拒时 dispatch 返回错误文本正是
模型将看到的反馈，脚本按预排继续走到 finish。
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

from orcsy.approvals import get_manager
from orcsy.config import LLMConfig, ProjectConfig
from orcsy.executor import TaskExecutor
from orcsy.git_ops import Git
from orcsy.task_model import parse_task_file


class ApprovalFakeLLM:
    """脚本化替身：容忍工具错误文本（审批被拒的场景），记录每次输出。"""

    script: list[tuple[str, dict]] = []
    outputs: list[tuple[str, str]] = []

    def __init__(self, cfg) -> None:
        pass

    def reset(self, system_prompt: str) -> None:
        self.system_prompt = system_prompt

    def run_task_loop(self, task_prompt, ctx, max_turns=40, on_progress=None):
        from orcsy.tools import FinishSignal, dispatch_tool
        for name, args in self.script:
            if name == "finish":
                raise FinishSignal(args)
            out = dispatch_tool(ctx, name, json.dumps(args, ensure_ascii=False))
            ApprovalFakeLLM.outputs.append((name, out))
        raise AssertionError("脚本执行完仍未 finish")


def make_project(root: Path, name: str) -> ProjectConfig:
    proj = root / name
    proj.mkdir(parents=True)
    (proj / "app.py").write_text("value = 1\n", encoding="utf-8")
    (proj / "AGENT.md").write_text("# 项目指引\n- 小步修改\n", encoding="utf-8")
    (proj / "tasks").mkdir()
    g = Git(proj)
    g.run("init", "-q")
    g.run("config", "user.email", "t@t")
    g.run("config", "user.name", "t")
    g.run("add", "-A")
    g.run("commit", "-qm", "init")
    return ProjectConfig(name=name, path=proj)


def make_task(proj: Path, task_id: str) -> None:
    (proj / "tasks" / f"{task_id}.md").write_text(
        f"---\ntitle: 审批测试任务\npriority: P1\nstatus: open\n---\n\n改点什么。\n",
        encoding="utf-8")
    g = Git(proj)
    g.run("add", "-A")
    g.run("commit", "-qm", "chore: add task")


def run_with_approval(executor: TaskExecutor, task, respond, mode_kwargs) -> tuple:
    """后台线程跑 run_task；主线程轮询 pending，命中后调 respond(entry) 响应。

    respond 返回 None 表示不响应（等超时）。返回 (result, 挂起期间看到的条目)。
    """
    box: dict = {}

    def worker():
        box["result"] = executor.run_task(task, guide_content="# 项目指引\n- 小步修改\n")
    t = threading.Thread(target=worker)
    t.start()
    mgr = get_manager()
    entry = None
    deadline = time.time() + 15
    while time.time() < deadline:
        pend = mgr.pending()
        if pend:
            entry = pend[0]
            break
        if not t.is_alive():
            break  # 任务已结束（可能根本没挂起——由调用方断言）
        time.sleep(0.05)
    if entry is not None and respond is not None:
        respond(entry)
    t.join(30)
    assert not t.is_alive(), "任务线程未在时限内结束（审批等待泄漏？）"
    return box.get("result"), entry


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="appr-e2e-"))
    try:
        llm_cfg = LLMConfig(api_key="fake", base_url="http://localhost:9", model="fake")
        mgr = get_manager()

        # ---- 场景 1：变更前询问，批准 write_file → 任务 done，进度留痕 ----
        proj = make_project(tmp, "p-approve")
        make_task(proj.path, "P1-t1")
        task = parse_task_file(proj.path / "tasks" / "P1-t1.md")
        ex = TaskExecutor(proj, llm_cfg, permission_mode="ask_on_change",
                          permission_timeout_minutes=1)
        ex.run_task.__globals__["LLMClient"] = ApprovalFakeLLM
        ApprovalFakeLLM.script = [
            ("list_dir", {"path": "."}),                       # 只读：不询问
            ("write_file", {"path": "new.txt", "content": "hi"}),  # 变更：挂起等审批
            ("finish", {"action": "completed", "summary": "已创建 new.txt",
                        "files_changed": ["new.txt"],
                        "commit_message": "feat: add new.txt"}),
        ]
        ApprovalFakeLLM.outputs.clear()
        seen: dict = {}

        def approve(entry):
            seen.update(entry)
            assert mgr.respond(entry["id"], True) is True
        result, entry = run_with_approval(ex, task, approve, None)
        assert entry is not None, "write_file 应挂起等待审批（list_dir 不应挂起）"
        assert seen["tool"] == "write_file" and seen["project"] == "p-approve"
        assert seen["task_id"] == "P1-t1" and seen["mode"] == "ask_on_change"
        assert "new.txt" in seen["args_preview"]
        assert result.outcome == "done", f"批准后任务应完成：{result.notes}"
        assert "hi" in (proj.path / "new.txt").read_text(encoding="utf-8"), \
            "批准的写入应已合并到主分支"
        # 只读的 list_dir 没触发审批：整个任务只挂起过一次
        # （归档由 ProjectAgent._write_back_result 负责，本测试直接跑 executor，
        # 进度行留在 tasks/ 原位）
        progress = (proj.path / "tasks" / "P1-t1.md").read_text(encoding="utf-8")
        assert "⏸ 等待审批：write_file" in progress, progress
        assert "✅ 审批通过：write_file" in progress
        assert progress.count("等待审批") == 1, "只读操作不应触发审批"
        print("ok: 场景1 变更前询问——只读放行、write_file 挂起、批准后任务完成并留痕")

        # ---- 场景 2：拒绝 → 零副作用，模型收到错误文本，任务 blocked 收尾 ----
        proj2 = make_project(tmp, "p-deny")
        make_task(proj2.path, "P1-t2")
        task2 = parse_task_file(proj2.path / "tasks" / "P1-t2.md")
        ex2 = TaskExecutor(proj2, llm_cfg, permission_mode="ask_on_change",
                           permission_timeout_minutes=1)
        ApprovalFakeLLM.script = [
            ("delete_file", {"path": "app.py"}),
            ("finish", {"action": "blocked", "summary": "删除被拒绝，任务受阻",
                        "files_changed": [], "commit_message": "",
                        "notes": "用户拒绝了 delete_file"}),
        ]
        ApprovalFakeLLM.outputs.clear()
        result2, entry2 = run_with_approval(
            ex2, task2, lambda e: mgr.respond(e["id"], False), None)
        assert entry2 is not None and entry2["tool"] == "delete_file"
        assert result2.outcome == "blocked", result2.outcome
        assert (proj2.path / "app.py").exists(), "被拒的删除不得有副作用"
        denied = [o for n, o in ApprovalFakeLLM.outputs if n == "delete_file"]
        assert denied and "用户拒绝" in denied[0], denied
        progress2 = (proj2.path / "tasks" / "P1-t2.md").read_text(encoding="utf-8")
        assert "⛔ 审批被拒绝：delete_file" in progress2, progress2
        print("ok: 场景2 拒绝——零副作用、模型收到错误反馈、进度留痕")

        # ---- 场景 3：始终允许 → 同任务内同类操作不再挂起 ----
        proj3 = make_project(tmp, "p-always")
        make_task(proj3.path, "P1-t3")
        task3 = parse_task_file(proj3.path / "tasks" / "P1-t3.md")
        ex3 = TaskExecutor(proj3, llm_cfg, permission_mode="autonomous",
                           permission_timeout_minutes=1)
        ApprovalFakeLLM.script = [
            ("write_file", {"path": "note.txt", "content": "n"}),  # 自主模式下写文件不询问
            ("run_command", {"command": ["git", "diff", "--stat"]}),  # 高风险：挂起
            ("run_command", {"command": ["git", "status", "--short"]}),  # 始终允许后放行
            ("finish", {"action": "completed", "summary": "两条命令都跑了",
                        "files_changed": ["note.txt"],
                        "commit_message": "feat: add note"}),
        ]
        hang_count = {"n": 0}

        def approve_always(entry):
            hang_count["n"] += 1
            mgr.respond(entry["id"], True, always=True)
        result3, entry3 = run_with_approval(ex3, task3, approve_always, None)
        assert entry3 is not None and entry3["tool"] == "run_command"
        assert result3.outcome == "done", result3.notes
        assert hang_count["n"] == 1, f"「始终允许」后第二条 run_command 不应再挂起（{hang_count}）"
        progress3 = (proj3.path / "tasks" / "P1-t3.md").read_text(encoding="utf-8")
        assert "本任务内后续不再询问" in progress3, progress3
        print("ok: 场景3 始终允许——同任务内同工具只询问一次")

        # ---- 场景 4：完全访问（默认）——变更操作零挂起，行为与历史一致 ----
        proj4 = make_project(tmp, "p-full")
        make_task(proj4.path, "P1-t4")
        task4 = parse_task_file(proj4.path / "tasks" / "P1-t4.md")
        ex4 = TaskExecutor(proj4, llm_cfg)  # 默认 full_access
        ApprovalFakeLLM.script = [
            ("write_file", {"path": "a.txt", "content": "x"}),
            ("delete_file", {"path": "a.txt"}),
            ("write_file", {"path": "b.txt", "content": "y"}),  # 留下净变更，completed 成立
            ("finish", {"action": "completed", "summary": "写完又删再写",
                        "files_changed": ["b.txt"], "commit_message": "feat: add b.txt"}),
        ]
        result4, entry4 = run_with_approval(ex4, task4, None, None)
        assert entry4 is None, "完全访问不应产生任何审批挂起"
        assert result4.outcome == "done", result4.notes
        progress4 = (proj4.path / "tasks" / "P1-t4.md").read_text(encoding="utf-8")
        assert "等待审批" not in progress4
        print("ok: 场景4 完全访问（默认）——零挂起零变化")

        print("\n人工审批端到端全部通过 ✅")
        return 0
    finally:
        get_manager().clear()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
