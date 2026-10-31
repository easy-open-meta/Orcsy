"""Issues 导入自检（离线）：平台识别、任务文件生成、去重、配置往返。

运行：uv run python tests/test_issues.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

from orcsy.config import load_config
from orcsy.issues import (IssueError, build_task_file, detect_repo, fetch_issues,
                          existing_task_stems, normalize_repo, sync_issues)

AUTH: dict[str, str] = {}


def check_detect() -> None:
    r = detect_repo("https://github.com/owner/repo")
    assert (r["provider"], r["owner"], r["repo"]) == ("github", "owner", "repo")
    assert r["origin"] == "https://github.com"
    r = detect_repo("https://gitee.com/o/r.git/")
    assert r["provider"] == "gitee" and r["repo"] == "r"
    r = detect_repo("https://gitlab.example.cn/group/app", "gitlab")  # 显式 provider
    assert r["provider"] == "gitlab" and r["owner"] == "group"
    r = detect_repo("https://repo.oscode.top/java-and-net/zp-web", "gitea")
    assert (r["provider"], r["owner"], r["repo"]) == ("gitea", "java-and-net", "zp-web")
    for bad_url, prov in (("https://scm.corp.cn/a/b", None), ("notaurl", None),
                          ("https://gitlab.com/only-one-segment", None)):
        try:
            detect_repo(bad_url, prov)
            raise AssertionError(f"{bad_url} 应报错")
        except IssueError:
            pass
    print("平台识别与 URL 解析（含自托管 Gitea）✅")


def check_normalize() -> None:
    r = normalize_repo({"url": "https://github.com/o/r", "token": " t "})
    assert r == {"url": "https://github.com/o/r", "provider": "github", "token": "t"}
    r = normalize_repo({"url": "https://gitlab.com/o/r", "username": " u ",
                        "password": " p "})
    assert r["username"] == "u" and r["password"] == " p "  # 密码原样保留（可能含空格）
    r = normalize_repo({"url": "https://gitee.com/o/r", "provider": "gitee",
                        "api_base": "http://127.0.0.1:9 "})
    assert r["provider"] == "gitee" and r["api_base"] == "http://127.0.0.1:9"
    for bad in ({}, {"url": ""}, {"url": "https://scm.corp.cn/a/b"}):
        try:
            normalize_repo(bad)
            raise AssertionError(f"{bad} 应报错")
        except IssueError:
            pass
    print("repo 绑定清洗/校验（token 与账号密码）✅")


def check_task_file() -> None:
    fn, content = build_task_file({
        "provider": "github", "owner": "o", "repo": "My.Repo", "number": 7,
        "title": "标题\n带换行", "body": "", "labels": ["bug", "P0"],
        "url": "https://github.com/o/My.Repo/issues/7", "author": "bob",
    })
    assert fn == "issue-github-My.Repo-7.md"
    assert "title: 标题 带换行" in content
    assert "priority: P0" in content and "status: open" in content
    assert "source: github o/My.Repo#7" in content
    assert "> 来自 GitHub Issue #7 · 作者 bob" in content
    assert "标题 带换行" in content.split("---", 2)[2]  # 空正文回退标题
    # 无 P 标签 → 默认 P2；非法字符仓库名清洗
    fn2, c2 = build_task_file({"provider": "gitee", "owner": "o", "repo": "a/b?c",
                               "number": 1, "title": "t", "body": "b",
                               "labels": [], "url": "", "author": "x"})
    assert "priority: P2" in c2 and fn2 == "issue-gitee-a-b-c-1.md"
    print("任务文件生成（标签优先级/正文回退/文件名清洗）✅")


def check_sync_dedup() -> None:
    """同步去重全链路（离线）：本地假 GitHub API，fetch → 导入 → 去重 → 归档不重导。"""
    import json
    import threading
    import http.server
    import socketserver

    ISSUES_PAGE1 = [
        {"number": 1, "title": "one", "body": "b1", "labels": [],
         "user": {"login": "a"}, "html_url": "https://github.com/o/r/issues/1"},
        {"number": 2, "title": "two", "body": "b2",
         "labels": [{"name": "P1"}], "user": {"login": "a"},
         "html_url": "https://github.com/o/r/issues/2"},
    ]

    class FakeGitHub(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import parse_qs
            AUTH["basic"] = self.headers.get("Authorization") or ""
            AUTH["path"] = self.path.split("?")[0]
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

    srv = _Srv(("127.0.0.1", 0), FakeGitHub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    api_base = f"http://127.0.0.1:{srv.server_address[1]}"
    tmp = Path(tempfile.mkdtemp(prefix="orcsy-issues-"))
    try:
        tasks = tmp / "proj" / "tasks"
        repo = {"url": "https://github.com/o/r", "api_base": api_base}
        r1 = sync_issues("p", repo, tasks)
        assert r1 == {"imported": 2, "skipped": 0, "total": 2, "project": "p"}, r1
        assert (tasks / "issue-github-r-1.md").exists()
        assert "priority: P1" in (tasks / "issue-github-r-2.md").read_text(encoding="utf-8")
        # 二次同步：全部跳过
        r2 = sync_issues("p", repo, tasks)
        assert r2["imported"] == 0 and r2["skipped"] == 2, r2
        # 归档后也不重导：把 1 号归档到 done/
        done = tasks / "done"
        done.mkdir()
        (tasks / "issue-github-r-1.md").replace(done / "issue-github-r-1.md")
        assert existing_task_stems(tasks) >= {"issue-github-r-1", "issue-github-r-2"}
        r3 = sync_issues("p", repo, tasks)
        assert r3["imported"] == 0, r3
        # 账号密码认证：Basic 头到达服务端（base64 of user:pass）——用 gitee 桩
        # （github 分支在到达服务端前就拒绝密码认证）
        import base64
        r4 = sync_issues("p", {"url": "https://gitee.com/o/r", "api_base": api_base,
                               "username": "u1", "password": "p1"}, tasks)
        assert r4["imported"] == 2, r4
        assert AUTH["basic"] == "Basic " + base64.b64encode(b"u1:p1").decode(), AUTH
        # GitHub + 仅账号密码 → 明确拒绝
        try:
            fetch_issues({"url": "https://github.com/o/r", "username": "u",
                          "password": "p"})
            raise AssertionError("GitHub 密码认证应被拒绝")
        except IssueError as e:
            assert "Personal Access Token" in str(e)
        # Gitea（自托管，显式 provider）：/api/v1 路径 + Authorization: token 头；
        # 且 Gitea 对普通 issue 也返回 pull_request: null 键——必须不被误过滤
        AUTH["basic"] = ""
        GITEA_PAGE1 = [
            {**it, "pull_request": None} for it in ISSUES_PAGE1  # 普通 issue：键存在值为 null
        ] + [  # 混入一条真 PR（pull_request 为对象）——应被过滤
            {"number": 99, "title": "PR 不该进队列", "body": "", "labels": [],
             "user": {"login": "a"}, "pull_request": {"url": "x"}},
        ]
        orig = ISSUES_PAGE1[:]
        ISSUES_PAGE1[:] = GITEA_PAGE1
        try:
            r5 = sync_issues("p", {"url": "https://repo.oscode.top/o/r", "provider": "gitea",
                                   "api_base": api_base, "token": "gitea-tok"}, tasks)
            assert r5["imported"] == 2, r5
            assert AUTH["path"] == "/api/v1/repos/o/r/issues", AUTH
            assert AUTH["basic"] == "token gitea-tok", AUTH
        finally:
            ISSUES_PAGE1[:] = orig
        # GitHub + 仅账号密码 → 明确拒绝
        try:
            fetch_issues({"url": "https://github.com/o/r", "username": "u",
                          "password": "p"})
            raise AssertionError("GitHub 密码认证应被拒绝")
        except IssueError as e:
            assert "Personal Access Token" in str(e)
        print("同步去重（假 GitHub API，含 done/ 归档）+ Basic/Gitea 认证 ✅")
        print("同步去重（假 GitHub API，含 done/ 归档）+ Basic/Gitea 认证 ✅")
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


def check_config_roundtrip() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="orcsy-issues-cfg-"))
    try:
        proj = tmp / "proj"
        proj.mkdir()
        (tmp / "config.yaml").write_text(
            "llm:\n  api_key: sk-x\n  base_url: https://api.deepseek.com/v1\n"
            "projects:\n"
            "  - name: bound\n"
            f"    path: {proj.as_posix()}\n"
            "    repo:\n"
            "      url: https://github.com/o/r\n"
            "      token: tok-1\n",
            encoding="utf-8")
        cfg = load_config(tmp / "config.yaml")
        p = cfg.projects[0]
        assert p.repo == {"url": "https://github.com/o/r", "provider": "github",
                          "token": "tok-1"}, p.repo
        # 非法平台快速失败
        (tmp / "config.yaml").write_text(
            "llm:\n  api_key: sk-x\n"
            "projects:\n"
            "  - name: bad\n"
            f"    path: {proj.as_posix()}\n"
            "    repo:\n"
            "      url: https://scm.corp.cn/a/b\n",
            encoding="utf-8")
        try:
            load_config(tmp / "config.yaml")
            raise AssertionError("无法识别的平台应报错")
        except ValueError as e:
            assert "bad" in str(e)
        print("config.yaml repo 读回/快速失败 ✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_detect()
    check_normalize()
    check_task_file()
    check_sync_dedup()
    check_config_roundtrip()
    print("\n全部断言通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
