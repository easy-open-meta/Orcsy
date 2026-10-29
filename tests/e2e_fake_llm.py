"""端到端冒烟测试：用模拟 LLM（不联网）驱动完整任务执行链路。

模拟 Agent 的行为：list_dir → read_file → write_file 修 bug → run_command 验证
→ finish(completed)。覆盖：worktree 创建、文件修改、提交合并、任务状态回写与归档。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

from task_arranger.config import LLMConfig, ProjectConfig
from task_arranger.executor import TaskExecutor
from task_arranger.git_ops import Git
from task_arranger.task_model import parse_task_file


class FakeLLMClient:
    """脚本化的 LLMClient 替身：按预排脚本产生工具调用。"""

    script: list[tuple[str, dict]] = []
    calls: list[str] = []

    def __init__(self, cfg) -> None:
        pass

    def reset(self, system_prompt: str) -> None:
        self.system_prompt = system_prompt

    def run_task_loop(self, task_prompt, ctx, max_turns=40):
        from task_arranger.tools import FinishSignal, dispatch_tool

        # 校验系统提示词里注入了 AGENT.md 指引
        assert "项目指引" in self.system_prompt, \
            "系统提示词未包含 AGENT.md 内容"

        for name, args in self.script:
            FakeLLMClient.calls.append(name)
            if name == "finish":
                raise FinishSignal(args)
            out = dispatch_tool(ctx, name, json.dumps(args, ensure_ascii=False))
            print(f"  [tool] {name} {json.dumps(args, ensure_ascii=False)[:80]}")
            print(f"         -> {out[:150].replace(chr(10), ' / ')}")
            assert not out.startswith("错误"), f"工具返回错误：{out}"

        raise AssertionError("脚本执行完仍未 finish")


def make_project(root: Path) -> ProjectConfig:
    proj = root / "sample-app"
    proj.mkdir(parents=True)
    (proj / "app.py").write_text(
        'def greet(name):\n'
        '    # TODO: 空名字会崩溃\n'
        '    return "Hello, " + name.upper()\n',
        encoding="utf-8",
    )
    (proj / "AGENT.md").write_text(
        "# 项目指引\n\n- 修改后运行 python -m pytest（本项目暂无测试，允许跳过）。\n",
        encoding="utf-8",
    )
    g = Git(proj)
    g.run("init", "-q")
    g.run("config", "user.email", "t@t")
    g.run("config", "user.name", "t")
    g.run("add", "-A")
    g.run("commit", "-qm", "init")
    return ProjectConfig(name="sample-app", path=proj)


def make_task(proj: Path) -> None:
    (proj / "tasks").mkdir()
    (proj / "tasks" / "P1-fix-greet-crash.md").write_text(
        "---\ntitle: 修复 greet 空名字崩溃\npriority: P1\nstatus: open\n---\n\n"
        "空名字调用 greet 时返回 'Hello, ' 而不是崩溃/大写异常。\n",
        encoding="utf-8",
    )
    # 任务清单纳入版本控制，避免被误判为未跟踪变更
    g = Git(proj)
    g.run("add", "-A")
    g.run("commit", "-qm", "chore: add task list")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="task-arranger-e2e-"))
    try:
        proj_cfg = make_project(tmp)
        make_task(proj_cfg.path)
        task = parse_task_file(proj_cfg.path / "tasks" / "P1-fix-greet-crash.md")
        assert task.is_actionable

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
            ("run_command", {"command": ["git", "diff", "--stat"]}),
            ("finish", {
                "action": "completed",
                "summary": "修复了 greet 空名字崩溃，加入空值分支",
                "files_changed": ["app.py"],
                "commit_message": "fix: greet 空名字时返回 stranger 而非崩溃",
                "notes": "",
            }),
        ]

        llm_cfg = LLMConfig(api_key="fake", base_url="http://localhost:9", model="fake")
        executor = TaskExecutor(proj_cfg, llm_cfg)
        # 注入替身
        executor.run_task.__globals__["LLMClient"] = FakeLLMClient

        result = executor.run_task(task, guide_content="## 项目指引\n\n- 修改后运行 python -m pytest（本项目暂无测试，允许跳过）。\n")

        print("\n== 结果 ==")
        print("outcome:", result.outcome)
        print("summary:", result.summary)
        print("commit:", result.commit)

        assert result.outcome == "done", f"期望 done，实际 {result.outcome}: {result.notes}"
        assert result.commit, "应有 commit"

        # 验证主分支已合并修复
        g = Git(proj_cfg.path)
        merged = (proj_cfg.path / "app.py").read_text(encoding="utf-8")
        assert "stranger" in merged, "主分支未包含修复内容"
        assert g.is_dirty() is False, "主工作区应是干净的"

        # worktree 已清理
        wt = proj_cfg.path / ".worktree-arranger"
        assert not wt.exists(), "worktree 应已移除"

        # 通过 ProjectAgent 验证任务状态回写与归档
        from task_arranger.agent import ProjectAgent
        agent = ProjectAgent(proj_cfg, llm_cfg)
        agent.executor.run_task = executor.run_task  # 复用同一个替身注入的执行器
        # 直接手工回写验证（避免重复跑一遍循环）
        agent._write_back_result(task, result)
        assert (proj_cfg.path / "tasks" / "done" / task.path.name).exists(), "done 任务应归档"
        archived = (proj_cfg.path / "tasks" / "done" / task.path.name).read_text(encoding="utf-8")
        assert "status: done" in archived, "归档任务状态应为 done"
        assert "Agent 处理结果" in archived, "归档任务应含处理结果小节"

        print("\n全部断言通过 ✅")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
