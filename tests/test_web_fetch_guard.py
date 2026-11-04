"""web_fetch 重定向 SSRF 防护自检（离线）：手动重定向循环，每一跳都重新校验。

背景（安全审计 2026-11-04 H1）：修复前 httpx 自动跟随重定向且不再校验，
攻击者页面可 302 → 内网/元数据服务绕过 SSRF 硬边界。现在每跳都过
_assert_public_url，本测试用假 client 驱动循环，不起外网。

运行：uv run python tests/test_web_fetch_guard.py
"""

from __future__ import annotations

import sys
import threading
import time
import http.server
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from orcsy import tools
from orcsy.tools import (_WebFetchReject, _fetch_with_guard, MAX_WEB_FETCH_BYTES)

# 公网 IP 字面量：真实 SSRF 守卫无需 DNS 即可放行（离线可跑）
PUBLIC = "https://93.184.216.34"


class FakeResponse:
    """假 httpx 流式响应：只实现 _fetch_with_guard 用到的最小接口。"""

    def __init__(self, status: int, headers: dict | None = None,
                 body: bytes = b"", encoding: str = "utf-8") -> None:
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self.encoding = encoding
        self.closed = False

    def iter_bytes(self, chunk_size: int = 65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


class FakeClient:
    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self.responses = responses
        self.requested: list[str] = []

    def stream(self, method: str, url: str) -> FakeResponse:
        assert method == "GET", method
        self.requested.append(url)
        return self.responses[url]


def check_redirect_targets_rejected() -> None:
    """重定向目标落到回环/元数据/私网：每一跳都被真实守卫拒绝。"""
    targets = [
        "http://127.0.0.1/",
        "http://localhost:5116/api/state",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://192.168.1.1/admin",
    ]
    for target in targets:
        resp = FakeResponse(302, {"location": target})
        client = FakeClient({PUBLIC + "/start": resp})
        try:
            _fetch_with_guard(client, PUBLIC + "/start")
            raise AssertionError(f"重定向目标应被 SSRF 守卫拒绝：{target}")
        except ValueError as e:
            assert "非公网地址" in str(e), (target, e)
        assert resp.closed, "跳转前必须关闭未读完的流式响应"
    print("ok: 重定向到回环/元数据/私网均被拒（且流式响应已关闭）")


def check_relative_location_and_success() -> None:
    """相对 Location 正确 urljoin；200 正常读取正文（回归）。"""
    client = FakeClient({
        PUBLIC + "/start": FakeResponse(302, {"location": "/next"}),
        PUBLIC + "/next": FakeResponse(302, {"location": "../final"}),
        PUBLIC + "/final": FakeResponse(
            200, {"content-type": "text/html; charset=utf-8"}, b"<p>hi</p>"),
    })
    status, ctype, buf, encoding, final_url = _fetch_with_guard(
        client, PUBLIC + "/start")
    assert status == 200 and ctype == "text/html", (status, ctype)
    assert bytes(buf) == b"<p>hi</p>" and encoding == "utf-8"
    assert final_url == PUBLIC + "/final", final_url
    assert client.requested == [PUBLIC + "/start", PUBLIC + "/next", PUBLIC + "/final"]
    print("ok: 相对 Location 逐跳 urljoin，200 正文/类型/最终地址正确")


def check_unsupported_scheme() -> None:
    client = FakeClient({PUBLIC + "/start": FakeResponse(
        302, {"location": "ftp://8.8.8.8/x"})})
    try:
        _fetch_with_guard(client, PUBLIC + "/start")
        raise AssertionError("非 http(s) 重定向应被拒绝")
    except httpx.HTTPError as e:
        assert "不支持的协议" in str(e), e
    print("ok: 重定向到非 http(s) 协议被拒")


def check_missing_location() -> None:
    client = FakeClient({PUBLIC + "/start": FakeResponse(302, {})})
    try:
        _fetch_with_guard(client, PUBLIC + "/start")
        raise AssertionError("缺少 Location 的重定向应报错")
    except httpx.HTTPError as e:
        assert "缺少 Location" in str(e), e
    print("ok: 重定向缺少 Location 明确报错")


def check_hop_limit() -> None:
    chain = {f"{PUBLIC}/hop{i}": FakeResponse(302, {"location": f"/hop{i + 1}"})
             for i in range(6)}
    client = FakeClient(chain)
    try:
        _fetch_with_guard(client, PUBLIC + "/hop0")
        raise AssertionError("超过跳数上限应中止")
    except httpx.HTTPError as e:
        assert "超过 5 跳" in str(e), e
    assert len(client.requested) == 5, client.requested  # 只发了上限内的 5 跳
    print("ok: 重定向循环超 5 跳中止，不发第 6 跳")


def check_status_and_content_type() -> None:
    client = FakeClient({PUBLIC + "/start": FakeResponse(404, {})})
    try:
        _fetch_with_guard(client, PUBLIC + "/start")
        raise AssertionError("HTTP 404 应报错")
    except _WebFetchReject as e:
        assert "HTTP 404" in str(e), e
    client = FakeClient({PUBLIC + "/start": FakeResponse(
        200, {"content-type": "application/octet-stream"}, b"x")})
    try:
        _fetch_with_guard(client, PUBLIC + "/start")
        raise AssertionError("二进制内容类型应拒绝")
    except _WebFetchReject as e:
        assert "不支持的内容类型" in str(e), e
    print("ok: 状态码≥400 / 非文本内容类型按原语义拒绝")


def check_body_cap() -> None:
    big = b"a" * (MAX_WEB_FETCH_BYTES + 1000)
    client = FakeClient({PUBLIC + "/start": FakeResponse(
        200, {"content-type": "text/plain"}, big)})
    _, _, buf, _, _ = _fetch_with_guard(client, PUBLIC + "/start")
    assert MAX_WEB_FETCH_BYTES <= len(buf) < MAX_WEB_FETCH_BYTES + 65536, len(buf)
    print("ok: 下载字节硬上限仍然生效")


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/hop1":
            self.send_response(302)
            self.send_header("Location", "/hop2")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = "<p>final body</p>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # noqa: A002
        pass


def check_real_httpx_redirect() -> None:
    """真实 httpx + 本机服务：确认客户端不自动跟随、循环按跳取到最终正文。"""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    guard = tools._assert_public_url
    tools._assert_public_url = lambda url: None  # 本机回环放行（守卫另有直测）
    try:
        # Windows 本机回环偶发 RST（快速连断竞态）：重试几次覆盖突发窗口
        last: Exception | None = None
        for _ in range(5):
            try:
                with httpx.Client(follow_redirects=False, timeout=5.0,
                                  trust_env=False) as client:
                    status, ctype, buf, _, final_url = _fetch_with_guard(
                        client, f"http://127.0.0.1:{port}/hop1")
                break
            except httpx.TransportError as e:
                last = e
                time.sleep(0.2)
        else:
            raise last  # type: ignore[misc]
        assert status == 200 and b"final body" in bytes(buf), bytes(buf)
        assert final_url.endswith("/hop2"), final_url
        print("ok: 真实 httpx 按跳手动跟随，取到最终正文")
    finally:
        tools._assert_public_url = guard
        server.shutdown()


def main() -> int:
    check_redirect_targets_rejected()
    check_relative_location_and_success()
    check_unsupported_scheme()
    check_missing_location()
    check_hop_limit()
    check_status_and_content_type()
    check_body_cap()
    check_real_httpx_redirect()
    print("\nweb_fetch 重定向 SSRF 防护全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
