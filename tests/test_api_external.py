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

from orcsy.config import (AppConfig, LLMConfig, ReviewConfig, UpdateConfig,
                          WebConfig, load_config, save_config, validate_web_security)
from orcsy.logging_setup import setup_logging
from orcsy.scheduler import Scheduler
from orcsy.web import AppState, make_server

TEST_KEY_1 = "sk-orcsy-test-key-alpha-12345"
TEST_KEY_2 = "sk-orcsy-test-key-beta-67890"
PORT = 0


def http_request(port: int, path: str, method: str = "GET",
                 body: dict | None = None, headers: dict | None = None) -> tuple[int, object, dict]:
    url = f"http://127.0.0.1:{port}{path}"
    hdrs = {"Connection": "close", **(headers or {})}
    data = None
    if body is not None:
        hdrs["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    last_err = None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(4):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with opener.open(req, timeout=10) as resp:
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
    server = None
    port = PORT
    try:
        app_cfg = AppConfig(
            llm=LLMConfig(provider="openai", api_key="sk-test", base_url="http://test/v1", model="test"),
            web=WebConfig(
                host="127.0.0.1",
                port=8080,
                auth_password="test-login-password",
                api_keys=[TEST_KEY_1, TEST_KEY_2],
                cors_origins=["*"],
            ),
            projects=[],
            update=UpdateConfig(repo="https://example.com/releases", token="update-secret", auto_check=False),
            review=ReviewConfig(llm=LLMConfig(provider="openai", api_key="review-secret",
                                               base_url="https://example.com/v1", model="review-model")),
            source_path=tmp / "config.yaml",
        )
        app_cfg.llm.extra_body = {"provider_options": {"audit": True}}
        save_config(app_cfg)
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
        server = make_server(state, "127.0.0.1", port)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        # 契约：免登录 /api/auth 与受保护端点的认证边界。
        code, _, auth = http_request(port, "/api/auth")
        assert code == 200 and auth.get("authed") is False
        for path in ("/api/state", "/api/state?summary=true"):
            code, _, denied = http_request(port, path)
            assert code == 401 and denied.get("ok") is False, (path, code, denied)

        # 1. 验证 OPTIONS 跨域预检请求（Preflight）
        code, hdrs, _ = http_request(port, "/api/state", method="OPTIONS")
        assert code == 204, f"OPTIONS 应返回 204，实际 {code}"
        assert hdrs.get("Access-Control-Allow-Origin") == "*"
        assert "GET" in hdrs.get("Access-Control-Allow-Methods", "")
        assert "Authorization" in hdrs.get("Access-Control-Allow-Headers", "")
        assert "X-API-Key" in hdrs.get("Access-Control-Allow-Headers", "")

        # 2. 验证未携带任何凭证时，受保护接口报 401
        code, hdrs, body = http_request(port, "/api/state")
        assert code == 401, f"未登录应报 401，实际 {code}"
        assert body.get("unauthorized") is True
        assert hdrs.get("Access-Control-Allow-Origin") == "*", "401 响应也应带 CORS 头"

        # 3. 验证携带错误 API Key 报 401
        code, _, _ = http_request(port, "/api/state", headers={"X-API-Key": "invalid-key"})
        assert code == 401

        code, _, _ = http_request(port, "/api/state", headers={"Authorization": "Bearer invalid-key"})
        assert code == 401

        # 4. 验证通过 Authorization: Bearer <api_key> 调用受保护接口
        code, hdrs, body = http_request(port, "/api/state", headers={"Authorization": f"Bearer {TEST_KEY_1}"})
        assert code == 200, f"Bearer API Key 应通过认证返回 200，实际 {code}: {body}"
        assert body.get("version") is not None
        assert hdrs.get("Access-Control-Allow-Origin") == "*"

        # 5. 验证通过 X-API-Key 调用受保护接口
        code, hdrs, body = http_request(port, "/api/state", headers={"X-API-Key": TEST_KEY_2})
        assert code == 200, f"X-API-Key 应通过认证返回 200，实际 {code}: {body}"
        assert body.get("version") is not None

        # 6. 验证 /api/auth 探测接口识别 Bearer Key
        code, _, body = http_request(port, "/api/auth", headers={"Authorization": f"Bearer {TEST_KEY_1}"})
        assert code == 200
        assert body.get("authed") is True
        assert body.get("enabled") is True

        # 7. 验证网页原生登录换取 Cookie 依然可用
        code, hdrs, body = http_request(port, "/api/login", method="POST", body={"password": "test-login-password"})
        assert code == 200 and body.get("ok") is True
        set_cookie = hdrs.get("Set-Cookie") or ""
        assert "orcsy_session=" in set_cookie
        cookie_val = set_cookie.split(";")[0]

        # 契约：remember_me=true 延长会话有效期。
        code, _, remembered = http_request(
            port, "/api/login", method="POST",
            body={"password": "test-login-password", "remember_me": True})
        assert code == 200 and remembered["expires_in"] == 30 * 24 * 3600

        # 带 Cookie 访问
        code, _, body = http_request(port, "/api/state", headers={"Cookie": cookie_val})
        assert code == 200

        # 从 Cookie 提取 Token，改用 Authorization: Bearer <session_token> 访问，亦能通过
        session_token = cookie_val.split("=")[1]
        code, _, body = http_request(port, "/api/state", headers={"Authorization": f"Bearer {session_token}"})
        assert code == 200

        # 稳定端点：摘要状态与看板分块保存；落盘再加载仍保留未提交分区。
        code, _, summary = http_request(port, "/api/state?summary=true", headers={"Cookie": cookie_val})
        assert code == 200 and summary["version"] == body["version"]
        assert isinstance(summary["projects"], list)

        def save_section(payload: dict) -> None:
            code, _, result = http_request(port, "/api/config", method="POST",
                                           body=payload, headers={"Cookie": cookie_val})
            assert code == 200 and result.get("ok") is True, (payload, code, result)

        save_section({"notify": {"sound": False, "system": True}})
        save_section({"update": {"repo": "https://example.com/new", "auto_check": False}})
        save_section({"llm": {"provider": "openai", "model": "test-new",
                              "base_url": "https://example.com/v1", "temperature": 0.4,
                              "max_tokens": 4096, "max_context_tokens": 131072,
                              "enable_thinking": False, "timeout_seconds": 120,
                              "extra_body": {"provider_options": {"audit": True}},
                              "tool_protocol": "native"}})
        save_section({"review": {"enabled": True, "llm": {"provider": "openai",
                                                  "model": "review-new",
                                                  "base_url": "https://example.com/v1"},
                                 "max_turns": 12}})
        persisted = load_config(app_cfg.source_path, require_api_key=False)
        assert persisted.notify.sound is False and persisted.notify.system is True
        assert persisted.update.repo == "https://example.com/new"
        assert persisted.update.auto_check is False and persisted.update.token == "update-secret"
        assert persisted.llm.model == "test-new" and persisted.llm.api_key == "sk-test"
        assert persisted.llm.extra_body == {"provider_options": {"audit": True}}
        assert persisted.review.llm is not None
        assert persisted.review.llm.model == "review-new"
        assert persisted.review.llm.api_key == "review-secret"
        code, _, refreshed = http_request(port, "/api/state", headers={"Cookie": cookie_val})
        assert code == 200 and refreshed["notify_cfg"] == {"sound": False, "system": True}
        assert refreshed["update_cfg"]["token_configured"] is True
        assert refreshed["review_cfg"]["model"] == "review-new"
        assert "update-secret" not in json.dumps(refreshed)
        assert "review-secret" not in json.dumps(refreshed)

        # 稳定任务入口：无项目时校验错误是 JSON 400。
        code, _, invalid = http_request(port, "/api/task", method="POST",
                                        body={"project": "missing", "id": "task-1",
                                              "title": "Example"}, headers={"Cookie": cookie_val})
        assert code == 400 and invalid["ok"] is False and "message" in invalid

    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_config_api_keys_and_security()
    test_external_api_server_e2e()
    print("ALL TESTS PASSED")
