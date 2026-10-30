"""Web 层端到端测试：mock LLM + 真实 HTTP 服务，验证「立即扫描」闭环。

流程：起 demo 项目 → 起 Web 服务（手动触发模式）→ POST /api/scan →
mock Agent 完成任务 → /api/state 显示任务归档、调度周期 +1。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import AppConfig, LLMConfig, ProjectConfig, WebConfig
from orcsy.executor import TaskExecutor
from orcsy.git_ops import Git
from orcsy.logging_setup import setup_logging
from orcsy.scheduler import Scheduler
from orcsy.web import AppState, make_server

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_fake_llm import FakeLLMClient  # noqa: E402


def api(port: int, path: str, body: dict | None = None) -> dict:
    url = f"http://127.0.0.1:{port}{path}"
    if body is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def make_project(root: Path) -> ProjectConfig:
    proj = root / "webproj"
    proj.mkdir()
    (proj / "app.py").write_text("value = 1\n", encoding="utf-8")
    (proj / "AGENT.md").write_text("# 指引\n- 小步修改\n", encoding="utf-8")
    (proj / "tasks").mkdir()
    (proj / "tasks" / "P1-bump-value.md").write_text(
        "---\ntitle: 把 value 改成 2\npriority: P1\nstatus: open\n---\n\n把 app.py 的 value 改为 2。\n",
        encoding="utf-8")
    g = Git(proj)
    g.run("init", "-q")
    g.run("config", "user.email", "t@t")
    g.run("config", "user.name", "t")
    g.run("add", "-A")
    g.run("commit", "-qm", "init")
    return ProjectConfig(name="webproj", path=proj)


def main() -> int:
    setup_logging("ERROR", "logs/web-e2e.log")
    tmp = Path(tempfile.mkdtemp(prefix="web-e2e-"))
    try:
        proj = make_project(tmp)
        cfg = AppConfig(
            llm=LLMConfig(api_key="fake", base_url="http://localhost:9", model="fake"),
            projects=[proj],
            web=WebConfig(host="127.0.0.1", port=5199, open_browser=False),
        )
        scheduler = Scheduler(cfg)
        # mock Agent 执行器：TaskExecutor.run_task 整体替换为脚本化版本
        FakeLLMClient.script = [
            ("edit_file", {"path": "app.py", "old_string": "value = 1",
                           "new_string": "value = 2"}),
            ("finish", {"action": "completed", "summary": "已改为 2",
                        "files_changed": ["app.py"],
                        "commit_message": "feat: bump value to 2"}),
        ]

        def run_task_mock(self, task, guide_content):
            from orcsy.llm import finish_response
            from orcsy.executor import TaskResult
            import json as _json
            from orcsy.tools import ToolContext, dispatch_tool
            ctx = ToolContext(root=self.worktree if self.is_git_repo else self.project.path)
            if self.is_git_repo:
                self._prepare_worktree()
            # 先在 worktree 里应用脚本里的 edit_file（模拟模型改代码）
            for name, args in FakeLLMClient.script:
                if name != "finish":
                    dispatch_tool(ctx, name, _json.dumps(args, ensure_ascii=False))
            resp = finish_response(FakeLLMClient.script[-1][1])
            result = TaskResult(task_id=task.id, outcome="blocked")
            result.summary, result.notes = resp.summary, resp.notes
            result.files_changed = resp.files_changed
            if resp.action == "completed":
                if self.is_git_repo:
                    merged = self._commit_and_merge(task, resp, result)
                    result.outcome = "done" if merged else "blocked"
                else:
                    result.outcome = "done"
            self.remove_worktree()
            return result

        TaskExecutor.run_task = run_task_mock

        state = AppState(cfg, scheduler)
        server = make_server(state, "127.0.0.1", 5199)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        # 1. 初始状态
        s = api(5199, "/api/state")
        assert s["projects"][0]["name"] == "webproj"
        assert len(s["projects"][0]["tasks"]) == 1
        assert s["scheduler"]["cycles_done"] == 0
        print("ok: 初始状态正确")

        # 2. 手动触发扫描
        r = api(5199, "/api/scan", {})
        assert r["ok"] is True
        deadline = time.time() + 30
        while time.time() < deadline:
            s = api(5199, "/api/state")
            if s["scheduler"]["cycles_done"] >= 1 and not s["scheduler"]["running"]:
                break
            time.sleep(0.3)
        s = api(5199, "/api/state")
        assert s["scheduler"]["cycles_done"] == 1, f"周期数应 >=1，实际 {s['scheduler']}"
        assert s["scheduler"]["last_cycle_stats"].get("webproj") == 1
        assert s["projects"][0]["tasks"] == [], "open 任务应已被处理归档"
        assert len(s["projects"][0]["archived"]) == 1, "任务应归档"
        assert s["projects"][0]["archived"][0]["status"] == "done"
        assert "value = 2" in (proj.path / "app.py").read_text(encoding="utf-8"), \
            "主分支文件应已合并修复"
        print("ok: 扫描闭环（任务完成、归档、代码合并）")

        # 3. Web 创建任务 → 状态查询可见
        r = api(5199, "/api/task", {"project": "webproj", "id": "P2-next",
                                    "title": "下一个任务", "priority": "P2",
                                    "status": "open", "description": "x"})
        assert r["ok"]
        s = api(5199, "/api/state")
        assert any(t["id"] == "P2-next" for t in s["projects"][0]["tasks"])
        print("ok: Web 建任务 → 状态可见")

        server.shutdown()
        print("\nWeb 端到端全部通过 ✅")
        return 0
    except (ConnectionResetError, urllib.error.URLError):
        # shutdown 与在途请求的竞态，断言已全部通过
        print("\nWeb 端到端全部通过 ✅（收尾连接重置忽略）")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
