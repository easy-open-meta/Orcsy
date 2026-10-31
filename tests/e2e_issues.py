"""端到端：项目绑定远程仓库 → 拉取 Issues 入任务队列（离线，假 GitHub API）。

覆盖：save_project 持久化 repo 绑定、issues-sync 端点导入/去重、未绑定报错。
运行：uv run python tests/e2e_issues.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import http.server
import socketserver
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

from orcsy.config import (AppConfig, LLMConfig, LoggingConfig, ProjectConfig,
                          SchedulerConfig, WebConfig)
from orcsy.scheduler import Scheduler
from orcsy.web import AppState

ISSUES_PAGE1 = [
    {"number": 11, "title": "修复登录超时", "body": "超时 30 秒即断开",
     "labels": [{"name": "bug"}, {"name": "P0"}], "user": {"login": "alice"},
     "html_url": "https://github.com/o/r/issues/11"},
    {"number": 12, "title": "支持暗色模式", "body": "",
     "labels": [{"name": "enhancement"}], "user": {"login": "bob"},
     "html_url": "https://github.com/o/r/issues/12"},
]


class FakeGitHub(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        from urllib.parse import parse_qs
        query = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        page = int(query.get("page", ["1"])[0])
        payload = ISSUES_PAGE1 if page == 1 else []
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass


class _Srv(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    srv = _Srv(("127.0.0.1", 0), FakeGitHub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    api_base = f"http://127.0.0.1:{srv.server_address[1]}"
    tmp = Path(tempfile.mkdtemp(prefix="orcsy-issues-e2e-"))
    try:
        proj = tmp / "proj"
        proj.mkdir()
        cfg = AppConfig(
            llm=LLMConfig(api_key="sk-x", base_url="http://localhost:9/v1"),
            scheduler=SchedulerConfig(run_once=True),
            logging=LoggingConfig(), web=WebConfig(),
            projects=[ProjectConfig(name="p", path=proj)],
            source_path=tmp / "config.yaml",
        )
        state = AppState(cfg, Scheduler(cfg))

        # 1. 未绑定 repo 时拉取 → 明确报错
        try:
            state.sync_project_issues({"project": "p"})
            raise AssertionError("未绑定 repo 应报错")
        except ValueError as e:
            assert "未绑定远程仓库" in str(e), e

        # 2. save_project 绑定仓库（走真实保存路径：持久化 + 热重载）
        r = state.save_project({"name": "p", "original_name": "p", "path": str(proj),
                                "repo": {"url": "https://github.com/o/r",
                                         "token": "tok", "api_base": api_base}})
        assert r["ok"], r
        bound = next(p for p in state.config.projects if p.name == "p")
        assert bound.repo == {"url": "https://github.com/o/r", "provider": "github",
                              "token": "tok", "api_base": api_base}, bound.repo

        # 3. 同步：两个 Issue 导入，P0 标签映射优先级
        r = state.sync_project_issues({"project": "p"})
        assert r["ok"] and r["imported"] == 2 and r["skipped"] == 0, r
        tasks = proj / "tasks"
        f11 = tasks / "issue-github-r-11.md"
        assert f11.exists() and "priority: P0" in f11.read_text(encoding="utf-8")
        assert (tasks / "issue-github-r-12.md").exists()
        # 空正文 Issue 回退标题，保证可解析执行
        assert "支持暗色模式" in (tasks / "issue-github-r-12.md").read_text(encoding="utf-8")

        # 4. 二次同步：全部跳过
        r = state.sync_project_issues({"project": "p"})
        assert r["imported"] == 0 and r["skipped"] == 2, r

        # 5. 批量删除：一次删掉两个导入的任务；不存在的 id 记入 failed 不阻断
        r = state.delete_tasks_batch({"project": "p", "ids": [
            "issue-github-r-11", "issue-github-r-12", "not-exist"]})
        assert r["deleted"] == 2 and r["failed"] == ["not-exist"], r
        assert not (tasks / "issue-github-r-11.md").exists()
        assert not (tasks / "issue-github-r-12.md").exists()
        # 空 ids 防呆
        try:
            state.delete_tasks_batch({"project": "p", "ids": []})
            raise AssertionError("空 ids 应报错")
        except ValueError as e:
            assert "ids" in str(e)

        # 6. 一键清空：先造一个已归档任务，清空后归档必须原样保留
        r = state.sync_project_issues({"project": "p"})
        assert r["imported"] == 2, r
        done = tasks / "done"
        done.mkdir(exist_ok=True)
        (tasks / "issue-github-r-11.md").replace(done / "issue-github-r-11.md")
        r = state.clear_tasks({"project": "p"})
        assert r["ok"] and r["cleared"] == 1, r  # 根目录只剩 1 个活动任务
        assert not (tasks / "issue-github-r-12.md").exists(), "活动任务应被清空"
        assert (done / "issue-github-r-11.md").exists(), "已归档任务必须保留"

        print("绑定持久化 / Issues 导入 / 去重 / 批量删除 / 一键清空(保归档) 全链路 ✅")
        print("\n全部断言通过 ✅")
        return 0
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
