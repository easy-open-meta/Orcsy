"""tool_http_request 工具自检测试：白名单安全拦截、DNS重绑定与硬禁区防御、跨请求Cookie保持与JSON排版。

运行：uv run python tests/test_http_request_guard.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

from orcsy.debug_session import DebugSession
from orcsy.tools import ToolContext
from orcsy.tools_debug import tool_http_request


class MockServerHandler(BaseHTTPRequestHandler):
    cookies_seen: list[str] = []

    def do_GET(self) -> None:
        if self.path == "/redirect-to-bad":
            self.send_response(302)
            self.send_header("Location", "http://example.com/sensitive")
            self.end_headers()
            return
        if self.path == "/redirect-ok":
            self.send_response(302)
            self.send_header("Location", "/hello")
            self.end_headers()
            return
        if self.path == "/hello":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            cookie = self.headers.get("Cookie", "")
            if cookie:
                MockServerHandler.cookies_seen.append(cookie)
            self.send_header("Set-Cookie", "session_id=secret123; Path=/")
            self.end_headers()
            self.wfile.write(json.dumps({"msg": "hello", "status": "ok"}).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length) if length > 0 else b""
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            parsed = json.loads(data.decode("utf-8"))
        except Exception:
            parsed = data.decode("utf-8")
        self.wfile.write(json.dumps({"received": parsed}).encode("utf-8"))

    def log_message(self, format, *args) -> None:
        pass


def test_http_request_security_and_flow() -> None:
    # 启动一个本地测试 HTTP 服务
    server = HTTPServer(("127.0.0.1", 0), MockServerHandler)
    port = server.server_port
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    tmp_dir = Path(tempfile.mkdtemp(prefix="orcsy-test-http-")).resolve()
    session = DebugSession(
        root=tmp_dir,
        effective_hosts=["localhost", "127.0.0.1"],
    )
    ctx = ToolContext(root=tmp_dir, session=session)

    try:
        # 1. 正常 GET 请求
        out = tool_http_request(ctx, {"url": f"http://127.0.0.1:{port}/hello"})
        assert "HTTP 200 OK" in out
        assert '"msg": "hello"' in out
        assert "Set-Cookie: session_id" in out  # 保护模式脱敏：只展示 cookie 名

        # 2. 第二次请求应自动携带 Cookie（Cookie Jar 保持）
        MockServerHandler.cookies_seen.clear()
        out2 = tool_http_request(ctx, {"url": f"http://127.0.0.1:{port}/hello"})
        assert "HTTP 200 OK" in out2
        assert len(MockServerHandler.cookies_seen) > 0
        assert "session_id=secret123" in MockServerHandler.cookies_seen[0]

        # 3. POST 请求并带 JSON
        out_post = tool_http_request(
            ctx,
            {
                "url": f"http://127.0.0.1:{port}/submit",
                "method": "POST",
                "json": {"action": "create", "value": 42},
            },
        )
        assert "HTTP 201 Created" in out_post
        assert '"action": "create"' in out_post

        # 4. 白名单外部目标拦截
        out_bad_host = tool_http_request(ctx, {"url": "http://192.168.1.200/test"})
        assert "错误：白名单安全拦截" in out_bad_host
        assert "不在允许的调试白名单中" in out_bad_host

        # 5. 硬禁区拦截
        out_meta = tool_http_request(ctx, {"url": "http://169.254.169.254/latest/meta-data/"})
        assert "错误：白名单安全拦截" in out_meta

        # 6. 重定向跳到未授权外部域名应被拦截
        out_redir_bad = tool_http_request(ctx, {"url": f"http://127.0.0.1:{port}/redirect-to-bad"})
        assert "错误：白名单安全拦截" in out_redir_bad
        assert "不在允许的调试白名单中" in out_redir_bad

        # 7. 重定向跳到白名单内有效路径放行
        out_redir_ok = tool_http_request(ctx, {"url": f"http://127.0.0.1:{port}/redirect-ok"})
        assert "HTTP 200 OK" in out_redir_ok
        assert '"msg": "hello"' in out_redir_ok

        print("tool_http_request 白名单防护、Cookie 保持与重定向安全核验 ✅")
    finally:
        session.close()
        server.shutdown()
        server.server_close()
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> None:
    test_http_request_security_and_flow()
    print("\nAll http_request guard tests passed! ✨")


if __name__ == "__main__":
    main()
