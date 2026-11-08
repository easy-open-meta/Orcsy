"""端到端任务演练测试：模拟 Agent 使用 dev_server 与 http_request 调试并完成任务。

覆盖完整链路：任务启动 → 调试会话创建 → 后台启动服务 → 本地 HTTP 验证 → 检查日志 → 停止服务 → 合并归档 → 会话销毁。
运行：uv run python tests/e2e_debug_task.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

import orcsy.executor
from orcsy.config import LLMConfig, LocalDebugConfig, ProjectConfig
from orcsy.executor import TaskExecutor
from orcsy.git_ops import Git
from orcsy.task_model import parse_task_file


class FakeDebugLLMClient:
    """脚本化的 LLMClient 替身：按预排脚本调用本地调试工具。"""

    script: list[tuple[str, dict]] = []
    calls: list[str] = []

    def __init__(self, cfg) -> None:
        pass

    def reset(self, system_prompt: str) -> None:
        self.system_prompt = system_prompt

    def run_task_loop(self, task_prompt, ctx, max_turns=40, on_progress=None):
        from orcsy.tools import FinishSignal, dispatch_tool

        for name, args in self.script:
            FakeDebugLLMClient.calls.append(name)
            if name == "finish":
                raise FinishSignal(args)
            out = dispatch_tool(ctx, name, json.dumps(args, ensure_ascii=False))
            if on_progress is not None:
                on_progress(f"第0轮 · {name}")
            print(f"  [tool] {name} -> {out[:120].replace(chr(10), ' / ')}")
            assert not out.startswith("错误"), f"工具返回错误：{out}"

        raise AssertionError("脚本执行完仍未 finish")


def make_app_project(root: Path) -> ProjectConfig:
    proj = root / "web-app"
    proj.mkdir(parents=True)

    # 一个带简单 HTTP 服务的 Python 脚本
    (proj / "server.py").write_text("""\
import os, sys
from http.server import HTTPServer, BaseHTTPRequestHandler

port = int(os.environ.get("PORT", 8000))

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok", "service": "ready"}')
    def log_message(self, format, *args): pass

server = HTTPServer(('127.0.0.1', port), Handler)
server.serve_forever()
""", encoding="utf-8")

    (proj / "AGENT.md").write_text("# 项目指引\n\n使用 dev_server_start 运行服务。\n", encoding="utf-8")

    g = Git(proj)
    g.run("init", "-q")
    g.run("config", "user.email", "t@t")
    g.run("config", "user.name", "t")
    g.run("add", "-A")
    g.run("commit", "-qm", "init")

    (proj / "tasks").mkdir()
    (proj / "tasks" / "P1-test-api.md").write_text(
        "---\ntitle: 启动服务并测试 API 路由\npriority: P1\nstatus: open\n---\n\n启动服务验证接口返回。\n",
        encoding="utf-8",
    )
    g.run("add", "-A")
    g.run("commit", "-qm", "add task")

    return ProjectConfig(name="web-app", path=proj)


def test_e2e_debug_task_lifecycle() -> None:
    root = Path(tempfile.mkdtemp(prefix="orcsy-e2e-debug-task-")).resolve()

    try:
        proj = make_app_project(root)
        llm = LLMConfig()
        local_debug_cfg = LocalDebugConfig(
            browser="auto",
            headless=True,
            allowed_hosts=["localhost", "127.0.0.1"],
        )

        # 预排工具调用脚本：启动服务 -> 调接口 -> 读日志 -> 停止服务 -> finish
        FakeDebugLLMClient.calls.clear()
        FakeDebugLLMClient.script = [
            ("dev_server_start", {
                "name": "api_srv",
                "command": [sys.executable, "server.py"],
                "ready_url": "http://127.0.0.1:{PORT}/",
                "ready_timeout_seconds": 15,
            }),
            ("http_request", {
                "url": "http://127.0.0.1:{PORT}/",  # 这里模拟通过占位符或已知端口调接口
            }),
            ("dev_server_logs", {
                "name": "api_srv",
                "tail": 20,
            }),
            ("dev_server_stop", {
                "name": "api_srv",
            }),
            ("write_file", {
                "path": "test_report.txt",
                "content": "API test passed\n",
            }),
            ("finish", {
                "action": "completed",
                "summary": "接口实测验证通过，状态正常",
                "commit_message": "test: 验证本地 API 接口",
            }),
        ]

        # 动态替换脚本中的实际端口（因为 dev_server_start 会在运行时分配端口）
        # 这里用 monkeypatch 让第二步使用第一步启动生成的端口
        orig_dispatch = orcsy.tools.dispatch_tool

        def custom_dispatch(ctx, name, args_json):
            import json
            args = json.loads(args_json) if isinstance(args_json, str) else dict(args_json)
            if name == "http_request" and "{PORT}" in args.get("url", ""):
                # 从 session 查找当前启动的服务端口
                if ctx.session and "api_srv" in ctx.session.services._services:
                    actual_port = ctx.session.services._services["api_srv"].port
                    args["url"] = args["url"].replace("{PORT}", str(actual_port))
            return orig_dispatch(ctx, name, json.dumps(args, ensure_ascii=False))

        orcsy.executor.LLMClient = FakeDebugLLMClient
        orcsy.tools.dispatch_tool = custom_dispatch

        executor = TaskExecutor(
            proj,
            llm,
            tools={
                "dev_server": True,
                "http_request": True,
            },
            local_debug=local_debug_cfg,
        )

        tasks_dir = proj.path / "tasks"
        task_file = tasks_dir / "P1-test-api.md"
        task = parse_task_file(task_file)

        result = executor.run_task(task, "项目指引")

        assert result.outcome == "done"
        assert result.commit is not None
        assert "接口实测验证通过" in result.summary

        # 断言所有预排工具都被调用
        assert "dev_server_start" in FakeDebugLLMClient.calls
        assert "http_request" in FakeDebugLLMClient.calls
        assert "dev_server_logs" in FakeDebugLLMClient.calls
        assert "dev_server_stop" in FakeDebugLLMClient.calls

        print("e2e_debug_task 完整调度与执行链路验证 ✅")
    finally:
        orcsy.tools.dispatch_tool = orig_dispatch
        shutil.rmtree(root, ignore_errors=True)


def main() -> None:
    test_e2e_debug_task_lifecycle()
    print("\nAll e2e_debug_task tests passed! ✨")


if __name__ == "__main__":
    main()
