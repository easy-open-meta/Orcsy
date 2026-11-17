"""单实例探测与退出连接清理回归测试。

背景（2026-11-17 生产问题）：
1. 开启登录密码后，启动时的「端口已有 Orcsy 实例」探测访问 /api/state，
   被 401 拒绝，误判成别的程序占端口 → 自动换随机端口再起一个隐藏实例。
   两个实例会同时扫描/执行同一批任务；退出其中一个托盘，另一个还在跑。
2. HTTP/1.1 keep-alive 让浏览器保留长连接；托盘退出只关监听 socket 时，
   已建立连接仍可继续操作看板，表现为「托盘已退出，-web 还在跑」。

本测试覆盖：
- /api/auth 免登录探测（含 401 的 /api/state 并存场景）
- /api/auth 的 app=orcsy 标记
- server_close 主动断开 keep-alive 活动连接
- 同一配置下第二次启动被识别并取消（子进程端到端）
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.__main__ import _probe_orcsy_instance
from orcsy.web import AppState, OrcsyHTTPServer

ROOT = Path(__file__).resolve().parents[1]


class _AuthHandler(BaseHTTPRequestHandler):
    """模拟 Orcsy：/api/auth 免登录返回身份，/api/state 需要鉴权（401）。"""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/auth"):
            code, body = 200, json.dumps(
                {"ok": True, "app": "orcsy", "enabled": True, "authed": False}
            ).encode("utf-8")
        elif self.path.startswith("/api/state"):
            code, body = 401, b'{"ok": false, "message": "unauthorized"}'
        else:
            code, body = 404, b"not found"
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # noqa: A002
        pass


class _OtherHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        body = b'{"ok": true, "hello": "world"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # noqa: A002
        pass


class _KeepAliveHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # noqa: A002
        pass


def _serve(server: ThreadingHTTPServer) -> threading.Thread:
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True)
    t.start()
    return t


def test_probe_detects_auth_enabled_instance() -> None:
    """开了登录密码也能识别已运行的 Orcsy（/api/state 401 不影响）。"""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _AuthHandler)
    t = _serve(srv)
    port = srv.server_address[1]
    try:
        assert _probe_orcsy_instance("127.0.0.1", port), "带鉴权的 Orcsy 应被识别"
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=3)

    # 0.0.0.0 绑定通配符要换算成回环探测（用新服务实例避免本机回环
    # 快速重连的 TIME_WAIT 抖动影响断言）
    srv2 = ThreadingHTTPServer(("127.0.0.1", 0), _AuthHandler)
    t2 = _serve(srv2)
    port2 = srv2.server_address[1]
    try:
        assert _probe_orcsy_instance("0.0.0.0", port2), "0.0.0.0 应换算为回环探测"
    finally:
        srv2.shutdown()
        srv2.server_close()
        t2.join(timeout=3)
    print("ok: 带登录密码的已有实例可被 /api/auth 识别")


def test_probe_rejects_non_orcsy() -> None:
    """不是 Orcsy 的服务（JSON 结构不匹配）不能被误认。"""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _OtherHandler)
    t = _serve(srv)
    port = srv.server_address[1]
    try:
        assert not _probe_orcsy_instance("127.0.0.1", port)
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=3)
    print("ok: 非 Orcsy 服务不会被误判")


def test_auth_status_has_app_marker() -> None:
    """AppState.auth_status 返回 app=orcsy，新版本探测可强校验。"""
    class _FakeState:
        auth_enabled = True

        def token_valid(self, token):  # noqa: ANN001
            return False

        def is_valid_api_key(self, key):  # noqa: ANN001
            return False

    result = AppState.auth_status(_FakeState(), None)  # type: ignore[arg-type]
    assert result == {"ok": True, "app": "orcsy", "enabled": True, "authed": False}, result
    print("ok: /api/auth 返回 app=orcsy 标记")


def test_server_close_disconnects_keepalive_clients() -> None:
    """退出时 server_close 要主动断开已建立的 keep-alive 连接。"""
    srv = OrcsyHTTPServer(("127.0.0.1", 0), _KeepAliveHandler)
    srv.handle_error = lambda *args: None  # 关闭连接时的预期异常不刷屏
    t = _serve(srv)
    port = srv.server_address[1]
    client = socket.create_connection(("127.0.0.1", port), timeout=3)
    client.sendall(b"GET / HTTP/1.1\r\nHost: test\r\n\r\n")
    raw = b""
    while b"\r\n\r\n" not in raw:
        raw += client.recv(4096)
    head, body = raw.split(b"\r\n\r\n", 1)
    assert b"200 OK" in head, head
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    while len(body) < length:
        body += client.recv(length - len(body))
    assert body == b"ok", body
    # 保持客户端连接空闲，模拟浏览器 keep-alive
    time.sleep(0.2)

    done = threading.Event()

    def close() -> None:
        srv.shutdown()
        srv.server_close()
        done.set()

    threading.Thread(target=close, daemon=True).start()
    assert done.wait(3.0), "server_close 不应被 keep-alive 连接挂住"
    client.settimeout(2)
    try:
        tail = client.recv(1)
    except (ConnectionResetError, ConnectionAbortedError, OSError):
        tail = b""
    assert tail == b"", f"退出后 keep-alive 连接应被断开，实际收到 {tail!r}"
    client.close()
    t.join(timeout=3)
    print("ok: server_close 主动断开 keep-alive 连接")


def test_second_start_is_cancelled_with_same_config() -> None:
    """同一配置第二次启动应被识别并取消，而不是换随机端口再起一个实例。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "config.yaml").write_text(textwrap.dedent("""\
            llm:
              provider: openai
              api_key: "sk-test"
              base_url: "http://127.0.0.1:1/v1"
              model: "test"
            scheduler:
              run_once: true
            logging:
              level: INFO
              file: "logs/test.log"
            web:
              host: 127.0.0.1
              port: 0
              open_browser: false
              auth_password: "pw"
            projects: []
            """), encoding="utf-8")
        # 选一个当前空闲端口写回配置（不能用 0：配置端口须为 1~65535）
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        cfg = (root / "config.yaml").read_text(encoding="utf-8")
        (root / "config.yaml").write_text(
            cfg.replace("port: 0", f"port: {port}"), encoding="utf-8")

        cmd = [sys.executable, "-m", "orcsy", "web", "--config",
               str(root / "config.yaml"), "--no-browser", "--no-sched", "--no-tray"]
        p1 = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True,
                              encoding="utf-8", errors="replace")
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if p1.poll() is not None:
                    raise AssertionError(
                        f"首个实例提前退出 rc={p1.returncode}: {p1.stdout.read()}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                raise AssertionError("首个实例未在 15s 内监听端口")

            p2 = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=20)
            combined = (p2.stdout or "") + (p2.stderr or "")
            assert p2.returncode == 0, (p2.returncode, combined[-1000:])
            assert "检测到已有 Orcsy 实例" in combined, combined[-1000:]
            assert "看板服务启动" not in combined, combined[-1000:]
        finally:
            p1.terminate()
            try:
                p1.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                p1.kill()
                p1.communicate()
    print("ok: 同一配置的重复启动被识别并取消")


def main() -> int:
    test_probe_detects_auth_enabled_instance()
    test_probe_rejects_non_orcsy()
    test_auth_status_has_app_marker()
    test_server_close_disconnects_keepalive_clients()
    test_second_start_is_cancelled_with_same_config()
    print("\n单实例探测与退出连接清理全部测试通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
