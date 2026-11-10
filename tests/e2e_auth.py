"""登录鉴权端到端测试：真实 HTTP 服务，验证守卫、会话 Cookie、改密三态。

流程：起带 auth_password 的 Web 服务 → 未登录 401 → 错密码 401 →
登录成功拿 Cookie → 带 Cookie 200 → 登出失效 → 改密/清密三态 →
关闭鉴权后免登录（回归兼容）。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import os
os.environ.setdefault('NO_PROXY', '127.0.0.1,localhost')  # 本机回环不走系统代理（环境可能带 http_proxy）
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import AppConfig, UpdateConfig, LLMConfig, WebConfig
from orcsy.logging_setup import setup_logging
from orcsy.scheduler import Scheduler
from orcsy.web import AppState, make_server

PORT = 5197


def req(port: int, path: str, body: dict | None = None,
        cookie: str | None = None, read_body: bool = True) -> tuple[int, object, dict]:
    """返回 (状态码, 响应头, JSON 体)；HTTPError（401/400）也解析成三元组。

    read_body=False 用于 HTML 路由（/）：只看状态码，不读体不解析——
    本机回环存在会重写 HTML 响应的中间层，读体可能超时。
    Windows 本机回环偶发 ConnectionResetError（快速连断竞态）重试两次。
    """
    last_err: Exception | None = None
    for _ in range(3):
        url = f"http://127.0.0.1:{port}{path}"
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        r = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                if not read_body:
                    return resp.status, resp.headers, {}
                return resp.status, resp.headers, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, e.headers, json.loads(e.read().decode())
        except (ConnectionResetError, TimeoutError, OSError) as e:
            last_err = e
    raise last_err  # type: ignore[misc]


def session_cookie(headers) -> str:
    """从 Set-Cookie 提取 'orcsy_session=<token>'（无则返回空串）。"""
    raw = headers.get("Set-Cookie") or ""
    return raw.split(";")[0] if raw.startswith("orcsy_session=") else ""


def login(port: int, password: str) -> str:
    code, headers, body = req(port, "/api/login", {"password": password})
    assert code == 200 and body["ok"] is True, (code, body)
    ck = session_cookie(headers)
    assert ck, f"登录成功应下发会话 Cookie，实际 Set-Cookie: {headers.get('Set-Cookie')}"
    return ck


def main() -> int:
    setup_logging("ERROR", "logs/auth-e2e.log")
    tmp = Path(tempfile.mkdtemp(prefix="auth-e2e-"))
    done = False  # 断言全部完成才允许把收尾连接重置当通过（否则原样抛出，不掩盖真失败）
    try:
        # save_settings 会写回 config.yaml：给一个真实落盘的 source_path
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text("llm: {}\n", encoding="utf-8")
        cfg = AppConfig(
            update=UpdateConfig(auto_check=False,  # 测试不触发后台更新检查
                                token="upd-token-1234567890"),
            llm=LLMConfig(api_key="sk-e2e-secret-key-1234567890",
                          base_url="http://localhost:9", model="fake"),
            web=WebConfig(host="127.0.0.1", port=PORT, open_browser=False,
                          auth_password="secret-123"),
            source_path=cfg_file,
        )
        scheduler = Scheduler(cfg)
        state = AppState(cfg, scheduler)
        server = make_server(state, "127.0.0.1", PORT)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        # 1. 鉴权开启：未登录访问 API 一律 401，SPA 壳与 /api/auth 放行
        code, _, body = req(PORT, "/api/state")
        assert code == 401 and body.get("unauthorized") is True, (code, body)
        code, _, body = req(PORT, "/api/task", {"project": "x", "id": "y"})
        assert code == 401, (code, body)  # POST 同样被守卫
        code, _, _ = req(PORT, "/", read_body=False)
        assert code == 200, "SPA 壳应放行（登录界面由页内接管）"
        code, _, body = req(PORT, "/api/auth")
        assert code == 200 and body == {"ok": True, "enabled": True, "authed": False}, body
        print("ok: 未登录 401 守卫（GET/POST），/ 与 /api/auth 白名单放行")

        # 2. 登录：错密码 401，对密码 200 + HttpOnly 会话 Cookie
        code, _, body = req(PORT, "/api/login", {"password": "wrong-pass"})
        assert code == 401 and not body.get("ok"), (code, body)
        code, headers, _ = req(PORT, "/api/login", {"password": "secret-123"})
        assert "HttpOnly" in (headers.get("Set-Cookie") or ""), "会话 Cookie 必须 HttpOnly"
        ck = session_cookie(headers)
        assert ck
        print("ok: 错密码 401、对密码下发 HttpOnly 会话 Cookie")

        # 3. 带 Cookie 访问恢复正常；/api/auth 报告 authed
        code, _, body = req(PORT, "/api/state", cookie=ck)
        assert code == 200 and "projects" in body, (code, body)
        assert body["web_cfg"]["auth_enabled"] is True, "full_state 应回显鉴权开关"
        assert "auth_password" not in json.dumps(body["web_cfg"]), "绝不回显密码本身"
        code, _, body = req(PORT, "/api/auth", cookie=ck)
        assert body["authed"] is True, body
        print("ok: 带 Cookie 200，web_cfg.auth_enabled 回显且不含密码")

        # 3b. H2：/api/state 不回显明文密钥/令牌，只给掩码与「已配置」标记
        code, _, body = req(PORT, "/api/state", cookie=ck)
        blob = json.dumps(body, ensure_ascii=False)
        assert "api_key" not in body["llm"], "llm 不应再返回明文 api_key"
        assert body["llm"]["key_configured"] is True
        assert body["llm"]["api_key_masked"] == "sk-e…7890", body["llm"]["api_key_masked"]
        assert state.config.llm.api_key not in blob, "明文密钥不得出现在 /api/state"
        assert "token" not in body["update_cfg"], "update_cfg 不应再返回明文 token"
        assert body["update_cfg"]["token_configured"] is True
        assert body["update_cfg"]["token_masked"] == "upd-…7890", body["update_cfg"]
        assert state.config.update.token not in blob, "明文令牌不得出现在 /api/state"
        print("ok: /api/state 密钥/令牌仅掩码，响应全文无明文")

        # 3c. api_key / update.token 三态：不带键=保持；空串=清除；新值=更新
        llm_payload = {
            "provider": state.config.llm.provider, "model": state.config.llm.model,
            "base_url": state.config.llm.base_url,
            "temperature": state.config.llm.temperature,
            "max_tokens": state.config.llm.max_tokens,
            "max_context_tokens": state.config.llm.max_context_tokens,
            "enable_thinking": state.config.llm.enable_thinking,
            "timeout_seconds": state.config.llm.timeout_seconds,
        }
        original_key = state.config.llm.api_key
        code, _, body = req(PORT, "/api/config", {"llm": llm_payload}, cookie=ck)
        assert code == 200 and body["ok"] is True, body
        assert state.config.llm.api_key == original_key, "不带 api_key 键应保持原密钥"
        code, _, body = req(PORT, "/api/config",
                            {"llm": {**llm_payload, "api_key": ""}}, cookie=ck)
        assert code == 200 and state.config.llm.api_key == "", "空串应清除密钥"
        code, _, body = req(PORT, "/api/config",
                            {"llm": {**llm_payload, "api_key": "sk-new-key-0000000000"}},
                            cookie=ck)
        assert code == 200 and state.config.llm.api_key == "sk-new-key-0000000000", \
            state.config.llm.api_key
        original_token = state.config.update.token
        code, _, body = req(PORT, "/api/config",
                            {"update": {"repo": state.config.update.repo,
                                        "auto_check": False}}, cookie=ck)
        assert code == 200 and state.config.update.token == original_token, \
            "不带 token 键应保持原令牌"
        code, _, body = req(PORT, "/api/config",
                            {"update": {"repo": state.config.update.repo,
                                        "token": "", "auto_check": False}}, cookie=ck)
        assert code == 200 and state.config.update.token == "", "空串应清除令牌"
        print("ok: api_key/update.token 三态（保持/清除/更新）")

        # 4. 伪造/垃圾 Cookie 不放行
        code, _, _ = req(PORT, "/api/state", cookie="orcsy_session=forged-token")
        assert code == 401
        print("ok: 伪造 Cookie 被拒")

        # 5. 登出：会话即刻失效，Cookie 被清除
        code, headers, body = req(PORT, "/api/logout", {}, cookie=ck)
        assert code == 200 and body["ok"] is True, body
        assert "Max-Age=0" in (headers.get("Set-Cookie") or ""), "登出应清除浏览器 Cookie"
        code, _, _ = req(PORT, "/api/state", cookie=ck)
        assert code == 401, "登出后旧 Cookie 应立即失效"
        print("ok: 登出注销会话并清 Cookie")

        # 5b. 登录限速（L1）：同 IP 连续失败达阈值 → 锁定，锁定期内正确密码也拒
        for _ in range(state.LOGIN_MAX_FAILS):
            code, _, body = req(PORT, "/api/login", {"password": "definitely-wrong"})
            assert code == 401 and not body.get("ok"), (code, body)
        code, _, body = req(PORT, "/api/login", {"password": cfg.web.auth_password})
        assert code == 401 and "尝试次数过多" in (body.get("message") or ""), (code, body)
        state._clear_login_throttle()  # 解锁，不影响后续改密步骤
        print("ok: 连续失败 %d 次后锁定，锁定期内正确密码也被拒" % state.LOGIN_MAX_FAILS)

        # 6. 改密三态：改密 → 旧会话全灭、新密码可登；不带键 → 保持；空串 → 关闭鉴权
        ck = login(PORT, "secret-123")
        code, _, body = req(PORT, "/api/config", {"web": {"auth_password": "new-pass"}},
                            cookie=ck)
        assert code == 200 and body["ok"] is True, body
        code, _, _ = req(PORT, "/api/state", cookie=ck)
        assert code == 401, "改密后旧会话应全部失效"
        ck = login(PORT, "new-pass")
        code, _, body = req(PORT, "/api/config", {"web": {"port": PORT}}, cookie=ck)
        assert code == 200 and body["ok"] is True, body
        code, _, _ = req(PORT, "/api/state", cookie=ck)
        assert code == 200, "不带 auth_password 键应保持原密码且不动会话"
        saved_cfg_text = cfg_file.read_text(encoding="utf-8")
        assert "pbkdf2:sha256:" in saved_cfg_text, "新密码应以 PBKDF2 密文持久化到 config.yaml"
        assert "new-pass" not in saved_cfg_text, "明文密码绝不应出现在 config.yaml"
        code, _, body = req(PORT, "/api/config", {"web": {"auth_password": ""}}, cookie=ck)
        assert code == 200 and body["ok"] is True, body
        code, _, body = req(PORT, "/api/auth")
        assert body == {"ok": True, "enabled": False, "authed": True}, body
        code, _, body = req(PORT, "/api/state")
        assert code == 200 and "projects" in body, "关闭鉴权后应免登录（回归兼容）"
        print("ok: 改密三态（更新/保持/清除），关闭鉴权后免登录")

        # 7. M1：非回环绑定 + 无密码 → 保存被拒（400 可行动提示），配置不生效
        code, _, body = req(PORT, "/api/config",
                            {"web": {"host": "0.0.0.0", "port": PORT}})
        assert code == 400 and "auth_password" in (body.get("message") or ""), (code, body)
        assert state.config.web.host == "127.0.0.1", "被拒的配置不得落盘生效"
        print("ok: 非回环绑定且无密码的保存被拒（M1）")

        done = True
        server.shutdown()
        print("\n登录鉴权端到端全部通过 ✅")
        return 0
    except (ConnectionResetError, urllib.error.URLError):
        if not done:
            raise  # 断言途中连接重置 = 真失败（如 keep-alive 协议失步），不吞
        # shutdown 与在途请求的竞态，断言已全部通过
        print("\n登录鉴权端到端全部通过 ✅（收尾连接重置忽略）")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
