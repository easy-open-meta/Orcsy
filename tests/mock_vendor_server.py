"""Local fake vendor server for GitLab/GitHub/Feishu/DingTalk adapter HTTP tests.

Implements only the endpoints the adapters call. Control endpoints under /__mock/
simulate a human approving/rejecting in the vendor UI.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

GITLAB_TOKEN = "gl-token"
GITHUB_TOKEN = "gh-token"
FEISHU_APP = ("app-id", "app-secret")
FEISHU_TOKEN = "fs-token"
DINGTALK_APP = ("dt-key", "dt-secret")
DINGTALK_TOKEN = "dt-token"


class MockState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.gitlab_issues: dict[int, dict] = {}
        self.github_issues: dict[int, dict] = {}
        self.feishu_instances: dict[str, dict] = {}
        self.dingtalk_instances: dict[str, dict] = {}
        self.seq = 0

    def next_id(self) -> int:
        with self.lock:
            self.seq += 1
            return self.seq


class Handler(BaseHTTPRequestHandler):
    state: MockState

    def log_message(self, fmt, *args) -> None:
        return

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _gitlab_auth(self) -> bool:
        return self.headers.get("PRIVATE-TOKEN") == GITLAB_TOKEN

    def _github_auth(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {GITHUB_TOKEN}"

    def _feishu_auth(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {FEISHU_TOKEN}"

    def _dingtalk_token(self) -> str:
        query = parse_qs(urlparse(self.path).query)
        return str((query.get("access_token") or [""])[0])

    # ---- GET ----

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        if path == "/gettoken":
            query = parse_qs(urlparse(self.path).query)
            if (str((query.get("appkey") or [""])[0]),
                    str((query.get("appsecret") or [""])[0])) != DINGTALK_APP:
                return self._send(401, {"errcode": 40089, "errmsg": "bad app"})
            return self._send(200, {"access_token": DINGTALK_TOKEN})
        if path.startswith("/open-apis/approval/v4/instances/"):
            if not self._feishu_auth():
                return self._send(401, {"code": 99991663, "msg": "bad token"})
            code = path.rsplit("/", 1)[-1]
            with self.state.lock:
                inst = self.state.feishu_instances.get(code)
            if inst is None:
                return self._send(404, {"code": 1, "msg": "missing"})
            return self._send(200, {"data": {"task_list": list(inst["tasks"])}})
        parts = path.split("/")
        # /api/v4/projects/{project}/issues/{iid}/notes
        if len(parts) >= 8 and parts[1:4] == ["api", "v4", "projects"] \
                and parts[-1] == "notes":
            if not self._gitlab_auth():
                return self._send(401, {"message": "401 Unauthorized"})
            try:
                iid = int(parts[-2])
            except ValueError:
                return self._send(404, {"message": "missing"})
            with self.state.lock:
                issue = self.state.gitlab_issues.get(iid)
            if issue is None:
                return self._send(404, {"message": "missing"})
            return self._send(200, list(issue["notes"]))
        # /repos/{owner}/{repo}/issues/{number}/comments
        if len(parts) >= 7 and parts[1] == "repos" and parts[-1] == "comments":
            if not self._github_auth():
                return self._send(401, {"message": "Bad credentials"})
            try:
                number = int(parts[-2])
            except ValueError:
                return self._send(404, {"message": "missing"})
            with self.state.lock:
                issue = self.state.github_issues.get(number)
            if issue is None:
                return self._send(404, {"message": "missing"})
            return self._send(200, list(issue["comments"]))
        return self._send(404, {"message": "not found"})

    # ---- POST ----

    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        body = self._read_json()
        if path == "/open-apis/auth/v3/tenant_access_token/internal":
            if (body.get("app_id"), body.get("app_secret")) != FEISHU_APP:
                return self._send(401, {"code": 10003, "msg": "bad app"})
            return self._send(200, {"tenant_access_token": FEISHU_TOKEN,
                                    "expire": 7200})
        if path == "/open-apis/approval/v4/instances":
            if not self._feishu_auth():
                return self._send(401, {"code": 99991663, "msg": "bad token"})
            code = f"fs-{self.state.next_id()}"
            with self.state.lock:
                self.state.feishu_instances[code] = {"tasks": []}
            return self._send(200, {"data": {"instance_code": code}})
        if path.endswith("/topapi/processinstance/create"):
            if self._dingtalk_token() != DINGTALK_TOKEN:
                return self._send(401, {"errcode": 40014, "errmsg": "bad token"})
            pi = f"dt-{self.state.next_id()}"
            with self.state.lock:
                self.state.dingtalk_instances[pi] = {"records": []}
            return self._send(200, {"process_instance_id": pi})
        if path.endswith("/topapi/processinstance/get"):
            if self._dingtalk_token() != DINGTALK_TOKEN:
                return self._send(401, {"errcode": 40014, "errmsg": "bad token"})
            pi = str(body.get("process_instance_id") or "")
            with self.state.lock:
                inst = self.state.dingtalk_instances.get(pi)
            if inst is None:
                return self._send(404, {"errcode": 88, "errmsg": "missing"})
            return self._send(200, {"process_instance": {
                "operation_records": list(inst["records"])}})
        # GitLab create issue
        if path.startswith("/api/v4/projects/") and path.endswith("/issues"):
            if not self._gitlab_auth():
                return self._send(401, {"message": "401 Unauthorized"})
            iid = self.state.next_id()
            with self.state.lock:
                self.state.gitlab_issues[iid] = {"notes": []}
            return self._send(200, {
                "iid": iid,
                "web_url": f"http://mock/gitlab/issues/{iid}",
            })
        # GitHub create issue
        if path.startswith("/repos/") and path.endswith("/issues"):
            if not self._github_auth():
                return self._send(401, {"message": "Bad credentials"})
            number = self.state.next_id()
            with self.state.lock:
                self.state.github_issues[number] = {"comments": []}
            return self._send(201, {
                "number": number,
                "html_url": f"http://mock/github/issues/{number}",
            })
        # Control endpoints
        if path == "/__mock/gitlab/comment":
            with self.state.lock:
                issue = self.state.gitlab_issues.get(int(body.get("iid") or 0))
                if issue is not None:
                    note = {
                        "system": False,
                        "body": body.get("body") or "",
                        "author": {"username": body.get("username") or "",
                                   "name": body.get("name") or ""},
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                    # 控制调用可能因客户端重试重复到达；同一用户同一内容只记一次
                    if note not in issue["notes"]:
                        issue["notes"].append(note)
            return self._send(200, {"ok": True})
        if path == "/__mock/github/comment":
            with self.state.lock:
                issue = self.state.github_issues.get(int(body.get("number") or 0))
                if issue is not None:
                    comment = {
                        "body": body.get("body") or "",
                        "user": {"login": body.get("login") or ""},
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                    if comment not in issue["comments"]:
                        issue["comments"].append(comment)
            return self._send(200, {"ok": True})
        if path == "/__mock/feishu/approve":
            code = str(body.get("instance_code") or "")
            with self.state.lock:
                inst = self.state.feishu_instances.get(code)
                if inst is not None:
                    task = {
                        "user_id": body.get("user_id") or "",
                        "status": body.get("status") or "APPROVED",
                    }
                    if task not in inst["tasks"]:
                        inst["tasks"].append(task)
            return self._send(200, {"ok": True})
        if path == "/__mock/dingtalk/approve":
            pi = str(body.get("process_instance_id") or "")
            with self.state.lock:
                inst = self.state.dingtalk_instances.get(pi)
                if inst is not None:
                    record = {
                        "userid": body.get("userid") or "",
                        "result": body.get("result") or "AGREE",
                        "date": "2026-01-01",
                    }
                    if record not in inst["records"]:
                        inst["records"].append(record)
            return self._send(200, {"ok": True})
        return self._send(404, {"message": "not found"})

    # ---- PATCH/PUT ----

    def do_PATCH(self) -> None:  # noqa: N802
        if unquote(urlparse(self.path).path).startswith("/repos/"):
            self._read_json()
            return self._send(200, {"ok": True})
        return self._send(404, {"message": "not found"})

    def do_PUT(self) -> None:  # noqa: N802
        if unquote(urlparse(self.path).path).startswith("/api/v4/projects/"):
            self._read_json()
            return self._send(200, {"ok": True})
        return self._send(404, {"message": "not found"})


def start_server() -> tuple[ThreadingHTTPServer, str]:
    state = MockState()
    handler = type("BoundHandler", (Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"
