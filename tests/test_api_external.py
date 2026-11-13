"""第三方系统 HTTP 调用支持验证测试：
验证静态 API Key 认证、Bearer Token / X-API-Key 请求头、CORS 响应头与 OPTIONS 预检请求。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import AppConfig, LLMConfig, WebConfig, validate_web_security
from orcsy.logging_setup import setup_logging
from orcsy.scheduler import Scheduler
from orcsy.web import AppState, make_server

TEST_KEY_1 = "sk-orcsy-test-key-alpha-12345"
TEST_KEY_2 = "sk-orcsy-test-key-beta-67890"
PORT = 5218


def http_request(port: int, path: str, method: str = "GET",
                 body: dict | None = None, headers: dict | None = None) -> tuple[int, object, dict]:
    url = f"http://127.0.0.1:{port}{path}"
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        hdrs["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    last_err = None
    for _ in range(4):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                res_json = json.loads(raw) if raw else {}
                return resp.status, resp.headers, res_json
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            res_json = json.loads(raw) if raw else {}
            return e.code, e.headers, res_json
        except (ConnectionResetError, TimeoutError, OSError) as e:
            last_err = e
    if last_err:
        raise last_err
    raise RuntimeError("请求失败")


def test_config_api_keys_and_security():
    """验证 WebConfig 对 api_keys 与 cors_origins 的支持与非回环鉴权守卫。"""
    cfg = WebConfig(host="127.0.0.1", api_keys=[TEST_KEY_1])
    assert cfg.api_keys == [TEST_KEY_1]
    assert cfg.cors_origins == ["*"]

    # 非回环地址但配置了 api_keys：应放行不抛异常（符合 M1 守卫）
    validate_web_security("0.0.0.0", "", context="test", api_keys=[TEST_KEY_1])

    # 非回环地址且无密码无 api_keys：必须抛异常拒绝
    try:
        validate_web_security("0.0.0.0", "", context="test", api_keys=[])
        assert False, "非回环地址无鉴权应抛异常"
    except ValueError as e:
        assert "web.auth_password" in str(e)


def test_external_api_server_e2e():
    """全链路端到端 HTTP 测试：OPTIONS 预检、Bearer Key、X-API-Key、Session Token 与 CORS。"""
    tmp = Path(tempfile.mkdtemp(prefix="orcsy-test-external-"))
    try:
        app_cfg = AppConfig(
            llm=LLMConfig(provider="openai", api_key="sk-test", base_url="http://test/v1", model="test"),
            web=WebConfig(
                host="127.0.0.1",
                port=PORT,
                auth_password="test-login-password",
                api_keys=[TEST_KEY_1, TEST_KEY_2],
                cors_origins=["*"],
            ),
            projects=[],
            source_path=tmp / "config.yaml",
        )
        scheduler = Scheduler(app_cfg)
        state = AppState(app_cfg, scheduler)

        # 单元逻辑验证
        assert state.auth_enabled is True
        assert state.is_valid_api_key(TEST_KEY_1) is True
        assert state.is_valid_api_key(TEST_KEY_2) is True
        assert state.is_valid_api_key("wrong-key") is False
        assert state.is_valid_api_key("") is False

        # 验证 /api/state 中的密钥掩码不泄露明文
        st = state.full_state()
        web_info = st["web_cfg"]
        assert web_info["auth_enabled"] is True
        assert web_info["api_keys_configured"] is True
        for masked in web_info["api_keys_masked"]:
            assert masked != TEST_KEY_1
            assert masked != TEST_KEY_2
            assert "••••" in masked or "…" in masked

        # 启动真实 HTTP 服务
        server = make_server(state, "127.0.0.1", PORT)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        # 1. 验证 OPTIONS 跨域预检请求（Preflight）
        code, hdrs, _ = http_request(PORT, "/api/state", method="OPTIONS")
        assert code == 204, f"OPTIONS 应返回 204，实际 {code}"
        assert hdrs.get("Access-Control-Allow-Origin") == "*"
        assert "GET" in hdrs.get("Access-Control-Allow-Methods", "")
        assert "Authorization" in hdrs.get("Access-Control-Allow-Headers", "")
        assert "X-API-Key" in hdrs.get("Access-Control-Allow-Headers", "")

        # 2. 验证未携带任何凭证时，受保护接口报 401
        code, hdrs, body = http_request(PORT, "/api/state")
        assert code == 401, f"未登录应报 401，实际 {code}"
        assert body.get("unauthorized") is True
        assert hdrs.get("Access-Control-Allow-Origin") == "*", "401 响应也应带 CORS 头"

        # 3. 验证携带错误 API Key 报 401
        code, _, _ = http_request(PORT, "/api/state", headers={"X-API-Key": "invalid-key"})
        assert code == 401

        code, _, _ = http_request(PORT, "/api/state", headers={"Authorization": "Bearer invalid-key"})
        assert code == 401

        # 4. 验证通过 Authorization: Bearer <api_key> 调用受保护接口
        code, hdrs, body = http_request(PORT, "/api/state", headers={"Authorization": f"Bearer {TEST_KEY_1}"})
        assert code == 200, f"Bearer API Key 应通过认证返回 200，实际 {code}: {body}"
        assert body.get("version") is not None
        assert hdrs.get("Access-Control-Allow-Origin") == "*"

        # 5. 验证通过 X-API-Key 调用受保护接口
        code, hdrs, body = http_request(PORT, "/api/state", headers={"X-API-Key": TEST_KEY_2})
        assert code == 200, f"X-API-Key 应通过认证返回 200，实际 {code}: {body}"
        assert body.get("version") is not None

        # 6. 验证 /api/auth 探测接口识别 Bearer Key
        code, _, body = http_request(PORT, "/api/auth", headers={"Authorization": f"Bearer {TEST_KEY_1}"})
        assert code == 200
        assert body.get("authed") is True
        assert body.get("enabled") is True

        # 7. 验证网页原生登录换取 Cookie 依然可用
        code, hdrs, body = http_request(PORT, "/api/login", method="POST", body={"password": "test-login-password"})
        assert code == 200 and body.get("ok") is True
        set_cookie = hdrs.get("Set-Cookie") or ""
        assert "orcsy_session=" in set_cookie
        cookie_val = set_cookie.split(";")[0]

        # 带 Cookie 访问
        code, _, body = http_request(PORT, "/api/state", headers={"Cookie": cookie_val})
        assert code == 200

        # 从 Cookie 提取 Token，改用 Authorization: Bearer <session_token> 访问，亦能通过
        session_token = cookie_val.split("=")[1]
        code, _, body = http_request(PORT, "/api/state", headers={"Authorization": f"Bearer {session_token}"})
        assert code == 200

        # 关闭测试服务
        server.shutdown()
        server.server_close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_config_api_keys_and_security()
    test_external_api_server_e2e()
    print("ALL TESTS PASSED")
