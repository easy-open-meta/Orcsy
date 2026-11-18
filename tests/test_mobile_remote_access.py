"""移动端远程访问自检（docs/design-mobile-remote-access.md §11.1）。

覆盖：
- 受信任代理解析（X-Forwarded-For 合法/伪造链，防反代下误锁 127.0.0.1 的 DoS）；
- Cookie + Bearer Token 双轨鉴权（登录响应体下发 token，Bearer 访问全量 API）；
- /api/system/endpoints 免登录连接向导（无凭据泄露）+ PWA manifest/icon 端点；
- 任务乐观锁：base_version 不符 409 Conflict 且数据未被冲掉，force_overwrite 放行。

运行：uv run python tests/test_mobile_remote_access.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault('NO_PROXY', '127.0.0.1,localhost')  # 本机回环不走系统代理
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import AppConfig, LLMConfig, ProjectConfig, SchedulerConfig, UpdateConfig, WebConfig
from orcsy.scheduler import Scheduler
from orcsy.web import (
    AppState,
    _is_ip_trusted,
    get_client_ip,
    make_server,
    qr_svg,
)

PWD = "correct-password"


class Hd(dict):
    """极简请求头替身（与 BaseHTTPRequestHandler.headers 同构的 get 语义）。"""

    def get(self, key, default=None):  # noqa: D102 — 与 http.client.HTTPMessage 一致
        return dict.get(self, key, default)


def check_trusted_proxies_resolution() -> None:
    # 名单语义：单 IP / CIDR / 非法值
    assert _is_ip_trusted("127.0.0.1", ["127.0.0.1", "::1"])
    assert _is_ip_trusted("::1", ["127.0.0.1", "::1"])
    assert not _is_ip_trusted("8.8.8.8", ["127.0.0.1"])
    assert _is_ip_trusted("172.17.0.9", ["172.17.0.0/16"])
    assert not _is_ip_trusted("not-an-ip", ["127.0.0.1"])
    assert not _is_ip_trusted("127.0.0.1", ["bad-entry", ""])

    # 直连（对端不受信任）：一律用对端 IP，不采信 XFF（可伪造）
    assert get_client_ip("192.168.1.5", Hd({"X-Forwarded-For": "1.2.3.4"}),
                         ["127.0.0.1", "::1"]) == "192.168.1.5"
    assert get_client_ip("192.168.1.5", Hd({"X-Real-IP": "5.5.5.5"}),
                         ["127.0.0.1", "::1"]) == "192.168.1.5"

    # 同机反代（对端 = 127.0.0.1 受信任）：取 XFF 链中最右侧不受信任的 IP
    assert get_client_ip("127.0.0.1", Hd({"X-Forwarded-For": "1.2.3.4"}),
                         ["127.0.0.1", "::1"]) == "1.2.3.4"
    assert get_client_ip("127.0.0.1", Hd({"X-Forwarded-For": "1.2.3.4, 10.0.0.9"}),
                         ["127.0.0.1", "::1"]) == "10.0.0.9"
    # 伪造全受信任链：回退链首（不让攻击者把限速打到任意内网 IP 上）
    assert get_client_ip("127.0.0.1", Hd({"X-Forwarded-For": "127.0.0.1, ::1"}),
                         ["127.0.0.1", "::1"]) == "127.0.0.1"
    # 反代没带转发头：退回 X-Real-IP，再退回对端
    assert get_client_ip("127.0.0.1", Hd({"X-Real-IP": "5.5.5.5"}),
                         ["127.0.0.1", "::1"]) == "5.5.5.5"
    assert get_client_ip("127.0.0.1", Hd({}), ["127.0.0.1", "::1"]) == "127.0.0.1"
    print("ok: 受信任代理解析（合法链/伪造链/缺头回退）")


def make_state(tmp: Path, trusted: list[str] | None = None,
               host: str = "127.0.0.1",
               source_path: Path | None = None) -> AppState:
    proj = tmp / "mobproj"
    proj.mkdir(exist_ok=True)
    (proj / "AGENT.md").write_text("# 指引\n", encoding="utf-8")
    (proj / "tasks").mkdir(exist_ok=True)
    cfg = AppConfig(
        llm=LLMConfig(api_key="k", base_url="http://localhost:9", model="m"),
        update=UpdateConfig(auto_check=False),
        scheduler=SchedulerConfig(run_once=True),
        projects=[ProjectConfig(name="mobproj", path=proj)],
        web=WebConfig(host=host, port=5216, open_browser=False,
                      auth_password=PWD, trusted_proxies=trusted or ["127.0.0.1", "::1"]),
    )
    if source_path:
        cfg.source_path = source_path
    state = AppState(cfg, Scheduler(cfg))
    state.LOGIN_FAIL_DELAY = 0.01
    return state


def _serve(state: AppState) -> tuple[int, ThreadingHTTPServer]:
    server = make_server(state, "127.0.0.1", 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return port, server


def _raw(port: int, path: str, method: str = "GET", body: dict | None = None,
         headers: dict | None = None) -> tuple[int, dict, dict]:
    """返回 (status, json体, 响应头)；HTTPError 也解析成响应体。"""
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            hdrs = dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        hdrs = dict(e.headers)
    try:
        payload = json.loads(raw.decode())
    except (ValueError, UnicodeDecodeError):
        payload = {}
    return hdrs.get("X-Http-Status") or 0, payload, hdrs


def _status(port: int, path: str, method: str = "GET", body: dict | None = None,
            headers: dict | None = None) -> tuple[int, dict, dict]:
    """返回 (状态码, JSON 体, 响应头)。urllib 把 4xx 抛 HTTPError 且拿不到状态码
    对象——用自建 opener 兜一层；Windows 本机回环偶发 ConnectionResetError
    （快速连断竞态，e2e_web 同款），重试三次。"""
    class NoRaise(urllib.request.HTTPErrorProcessor):
        def http_response(self, request, response):
            return response

        https_response = http_response

    opener = urllib.request.build_opener(NoRaise)
    import time as _time
    last_err: Exception | None = None
    for attempt in range(6):
        url = f"http://127.0.0.1:{port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Connection": "close",
                                              "Content-Type": "application/json",
                                              **(headers or {})})
        try:
            with opener.open(req, timeout=15) as resp:
                raw = resp.read()
                hdrs = dict(resp.headers)
                code = resp.status
            break
        except (ConnectionResetError, TimeoutError, OSError) as e:
            last_err = e
            _time.sleep(0.4 * (attempt + 1))
            continue
    else:
        raise last_err  # type: ignore[misc]
    try:
        payload = json.loads(raw.decode())
    except (ValueError, UnicodeDecodeError):
        payload = {}
    return code, payload, hdrs


def _read_raw(port: int, path: str, headers: dict | None = None) -> tuple[str, bytes]:
    """读非 JSON 响应（图标/二维码），返回 (content_type, body)。
    Windows 本机回环偶发 ConnectionResetError，重试三次（e2e_web 同款）。"""
    last_err: Exception | None = None
    for _ in range(3):
        url = f"http://127.0.0.1:{port}{path}"
        req = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.headers["Content-Type"], resp.read()
        except ConnectionResetError as e:
            last_err = e
            continue
    raise last_err  # type: ignore[misc]


def check_dual_auth(tmp: Path) -> None:
    state = make_state(tmp)
    port, server = _serve(state)
    try:
        # 未登录：401
        code, payload, _ = _status(port, "/api/state")
        assert code == 401 and payload.get("unauthorized"), (code, payload)

        # 登录：响应体下发 token + Cookie 双轨
        code, payload, hdrs = _status(port, "/api/login", "POST", {"password": PWD})
        assert code == 200 and payload.get("ok"), (code, payload)
        token = payload.get("token")
        assert token, payload
        assert "orcsy_session=" in (hdrs.get("Set-Cookie") or ""), hdrs

        # Bearer 双轨：Authorization 头访问全量 API
        code, payload, _ = _status(port, "/api/state",
                                   headers={"Authorization": f"Bearer {token}"})
        assert code == 200 and payload.get("version"), (code, payload.get("version"))
        # Cookie 轨依旧有效
        code, payload, _ = _status(port, "/api/state",
                                   headers={"Cookie": f"orcsy_session={token}"})
        assert code == 200 and payload.get("version"), (code, payload)
        # 无效 token：401
        code, _, _ = _status(port, "/api/state",
                             headers={"Authorization": "Bearer bogus"})
        assert code == 401, code

        # remember_me：长有效期会话
        code, payload, hdrs = _status(port, "/api/login", "POST",
                                      {"password": PWD, "remember_me": True})
        assert code == 200 and payload.get("expires_in") == 30 * 24 * 3600, payload
        assert "Max-Age=2592000" in (hdrs.get("Set-Cookie") or ""), hdrs

        # 免登录白名单：连接向导 + PWA 资源
        code, payload, _ = _status(port, "/api/system/endpoints")
        assert code == 200 and payload.get("ok") and payload.get("auth_required"), payload
        urls = [e["url"] for e in payload.get("endpoints", [])]
        assert any("127.0.0.1" in u for u in urls), urls
        assert not any("token" in u.lower() or PWD in u for u in urls), urls
        # 仅监听回环（本测试服务绑定 127.0.0.1）：局域网候选地址列出但标记
        # 未开放（active=false），并回 all_loopback=true + 开放操作指引，
        # 前端据此展示指引而不是渲染打不开的 127.0.0.1 二维码
        assert payload.get("all_loopback") is True, payload.get("all_loopback")
        assert "0.0.0.0" in (payload.get("hint") or ""), payload.get("hint")
        lan_eps = [e for e in payload.get("endpoints", [])
                   if "127.0.0.1" not in e["url"]]
        assert lan_eps, "回环绑定时也应列出局域网候选地址（供指引展示）"
        assert all(e.get("active") is False for e in lan_eps), lan_eps
        code, payload, hdrs = _status(port, "/manifest.webmanifest")
        assert code == 200 and payload.get("name") and payload.get("icons"), payload
        icon = _read_raw(port, "/appicon.png?size=192")
        assert icon[0] == "image/png" and len(icon[1]) > 500, icon[0]
        # 二维码端点（需登录）
        qr = _read_raw(port, f"/api/system/qr?url=http://127.0.0.1:{port}",
                       headers={"Authorization": f"Bearer {token}"})
        assert "image/svg+xml" in qr[0] and b"<svg" in qr[1], qr[0]
        # 摘要轮询：无 log_tail、无归档；全量模式正常带日志
        code, payload, _ = _status(port, "/api/state?summary=true",
                                   headers={"Authorization": f"Bearer {token}"})
        assert code == 200 and payload.get("log_tail") == "", payload.get("log_tail")
        assert all(p.get("archived") == [] for p in payload.get("projects", []))
        code, payload, _ = _status(port, "/api/state",
                                   headers={"Authorization": f"Bearer {token}"})
        assert isinstance(payload.get("log_tail"), str), payload.keys()
        print("ok: Bearer 双轨鉴权 / 免登录连接向导 / PWA 资源 / 摘要轮询")
    finally:
        state._clear_login_throttle()
        server.shutdown()


def check_login_throttle_no_loopback_lockout(tmp: Path) -> None:
    """反代场景爆破锁定真实外网 IP 时，本机回环（不带 XFF 的直连用户）不受牵连。"""
    state = make_state(tmp)
    port, server = _serve(state)
    try:
        # 经受信任反代（对端 127.0.0.1 + XFF 3.3.3.3）连续爆破 5 次
        for _ in range(5):
            code, payload, _ = _status(
                port, "/api/login", "POST", {"password": "wrong"},
                headers={"X-Forwarded-For": "3.3.3.3"})
            assert code == 401, (code, payload)
        # 第 6 次仍走 3.3.3.3：锁定（429/401 均拒）
        code, payload, _ = _status(
            port, "/api/login", "POST", {"password": PWD},
            headers={"X-Forwarded-For": "3.3.3.3"})
        assert code == 401 and "锁定" in payload.get("message", ""), (code, payload)
        # 本机回环直连（无 XFF）：不被牵连，正常登录
        code, payload, _ = _status(port, "/api/login", "POST", {"password": PWD})
        assert code == 200 and payload.get("ok"), (code, payload)
        print("ok: 反代爆破锁定真实 IP，本机回环畅通（无 DoS）")
    finally:
        state._clear_login_throttle()
        server.shutdown()


def check_task_concurrency_conflict_409(tmp: Path) -> None:
    state = make_state(tmp)
    port, server = _serve(state)
    try:
        code, payload, _ = _status(port, "/api/login", "POST", {"password": PWD})
        token = payload["token"]
        auth = {"Authorization": f"Bearer {token}"}

        # 新建任务：响应带回新版本快照
        code, payload, _ = _status(port, "/api/task", "POST", {
            "project": "mobproj", "id": "P2-fix-login", "title": "修复登录超时",
            "priority": "P2", "status": "open", "description": "原始描述",
        }, headers=auth)
        assert code == 200 and payload.get("ok") and payload.get("version_token"), payload

        # 读取 → version_token 与保存响应一致
        code, payload, _ = _status(
            port, "/api/task?project=mobproj&id=P2-fix-login", headers=auth)
        assert code == 200 and payload.get("version_token"), payload
        base_version = payload["version_token"]

        # 后台 Agent 修改了任务文件（模拟并发写）
        task_file = (tmp / "mobproj" / "tasks" / "P2-fix-login.md")
        time.sleep(0.02)  # mtime_ns 粒度保护：确保签名必变
        text = task_file.read_text(encoding="utf-8").replace("open", "in_progress", 1)
        task_file.write_text(text, encoding="utf-8")

        # 手机端带旧 base_version 保存 → 409 Conflict + 最新任务内容，数据未被冲掉
        code, payload, _ = _status(port, "/api/task", "POST", {
            "project": "mobproj", "id": "P2-fix-login", "title": "手机上的修改",
            "priority": "P2", "status": "open", "description": "手机编辑",
            "base_version": base_version,
        }, headers=auth)
        assert code == 409 and payload.get("conflict"), (code, payload)
        latest = payload.get("latest_task") or {}
        assert latest.get("status") == "in_progress", latest
        on_disk = task_file.read_text(encoding="utf-8")
        assert "in_progress" in on_disk and "手机编辑" not in on_disk, on_disk

        # force_overwrite=true：明确意图下放行覆盖
        code, payload, _ = _status(port, "/api/task", "POST", {
            "project": "mobproj", "id": "P2-fix-login", "title": "手机上的修改",
            "priority": "P2", "status": "in_progress", "description": "手机编辑",
            "base_version": base_version, "force_overwrite": True,
        }, headers=auth)
        assert code == 200 and payload.get("ok"), (code, payload)
        on_disk = task_file.read_text(encoding="utf-8")
        assert "手机编辑" in on_disk, on_disk

        # 无 base_version 的旧客户端：行为不变（直接保存）
        code, payload, _ = _status(port, "/api/task", "POST", {
            "project": "mobproj", "id": "P2-another", "title": "另一个任务",
            "priority": "P3", "status": "open", "description": "x",
        }, headers=auth)
        assert code == 200, (code, payload)
        print("ok: 任务乐观锁（409 Conflict + 数据不冲掉 + force_overwrite 放行）")
    finally:
        server.shutdown()


def check_one_click_enable(tmp: Path) -> None:
    """向导「一键开放手机访问」的服务端语义（前端勾选后发 POST /api/config）：
    host=0.0.0.0 + 新密码 → 配置落盘（密码只存 PBKDF2 密文）、旧会话失效、
    连接向导转为可扫码状态（局域网地址 active=true）。"""
    state = make_state(tmp)
    cfg_file = tmp / "config.yaml"
    state.config.source_path = cfg_file  # save_settings 落盘目标
    port, server = _serve(state)
    try:
        code, payload, _ = _status(port, "/api/login", "POST", {"password": PWD})
        token = payload["token"]
        auth = {"Authorization": f"Bearer {token}"}

        # 打勾开启：host=0.0.0.0 + 设置新密码（沿用现有密码时前端不带 auth_password 键）
        # 双执行容忍：第一发改密成功后旧会话立即全灭，重试的第二发带旧 token
        # 收到 401——改密实际已生效
        code, payload, _ = _status(port, "/api/config", "POST", {
            "web": {"host": "0.0.0.0", "auth_password": "brand-new-pwd"},
        }, headers=auth)
        assert code == 200 and payload.get("ok") \
            or code == 401, (code, payload)

        # 改密后旧会话全部失效：旧密码 401、新密码可登录
        code, payload, _ = _status(port, "/api/login", "POST", {"password": PWD})
        assert code == 401, (code, payload)
        code, payload, _ = _status(port, "/api/login", "POST",
                                   {"password": "brand-new-pwd"})
        assert code == 200 and payload.get("ok"), (code, payload)

        # 保存后未重启：监听 socket 仍在回环（bound_host 不随保存热更），
        # 向导必须继续报「未开放」而不是把还没监听的局域网地址标成可扫码
        code, payload, _ = _status(port, "/api/system/endpoints")
        assert payload.get("all_loopback") is True, payload
        assert "尚未重启" in (payload.get("hint") or ""), payload.get("hint")
        assert all(e.get("active") is False for e in payload["endpoints"]
                   if "127.0.0.1" not in e["url"]), payload["endpoints"]

        # 重启生效（模拟 make_server 重新绑定 0.0.0.0）：向导转可扫码
        state.bound_host = "0.0.0.0"
        code, payload, _ = _status(port, "/api/system/endpoints")
        assert payload.get("all_loopback") is False, payload
        assert payload.get("password_configured") is True, payload
        lan_active = [e for e in payload["endpoints"]
                      if "127.0.0.1" not in e["url"] and e.get("active")]
        assert lan_active, payload["endpoints"]

        # 密码只存密文：落盘的 config.yaml 无明文，host 已写为 0.0.0.0
        cfg_text = cfg_file.read_text(encoding="utf-8")
        assert "host: 0.0.0.0" in cfg_text, cfg_text[:400]
        assert "brand-new-pwd" not in cfg_text, "明文密码不得落盘"
        assert "pbkdf2:sha256:" in cfg_text, cfg_text[:400]
        print("ok: 一键开放手机访问（配置落盘密文化 + 向导转可扫码）")
    finally:
        state._clear_login_throttle()
        server.shutdown()


def check_enable_without_password(tmp: Path) -> None:
    """零凭据实例（auth_password 空、无 api_keys）的一键开启。

    存量 bug：save_settings 三态逻辑用 verify_password(新密码, '') 判「与现值
    是否一致」，而空密文在免密语义下恒真——首次设密码被静默跳过，随后绑定
    守卫以「0.0.0.0 无凭据」拒绝，向导一键开启与设置页首次设密码都会踩中。"""
    state = make_state(tmp)
    state.config.web = WebConfig(host="127.0.0.1", port=5216, open_browser=False,
                                 auth_password="")  # 零凭据：免密模式
    cfg_file = tmp / "config-nopwd.yaml"
    state.config.source_path = cfg_file
    port, server = _serve(state)
    try:
        code, payload, _ = _status(port, "/api/config", "POST", {
            "web": {"host": "0.0.0.0", "auth_password": "mobile-pwd-9"},
        })
        assert code == 200 and payload.get("ok"), (code, payload)
        # 新密码立即生效：可登录（免密态就此关闭）
        code, payload, _ = _status(port, "/api/login", "POST",
                                   {"password": "mobile-pwd-9"})
        assert code == 200 and payload.get("ok"), (code, payload)
        # 重启生效后向导转为可扫码（模拟 make_server 重新绑定）
        state.bound_host = "0.0.0.0"
        code, payload, _ = _status(port, "/api/system/endpoints")
        assert payload.get("all_loopback") is False, payload
        assert payload.get("password_configured") is True, payload
        # 密文落盘：config.yaml 无明文密码
        cfg_text = cfg_file.read_text(encoding="utf-8")
        assert "host: 0.0.0.0" in cfg_text, cfg_text[:400]
        assert "mobile-pwd-9" not in cfg_text, "明文密码不得落盘"
        assert "pbkdf2:sha256:" in cfg_text, cfg_text[:400]
        print("ok: 零凭据实例一键开启（首次设密码生效，不再被误判为与现值一致）")
    finally:
        state._clear_login_throttle()
        server.shutdown()


def check_session_persistence(tmp: Path) -> None:
    """登录会话落盘持久化：进程重启（重建 AppState）后 30 天免登录依旧有效。
    此前的会话只存内存，部署重建容器就把免登录清空——用户每次更新都要重登。"""
    cfg_file = tmp / "config.yaml"
    state = make_state(tmp, source_path=cfg_file)
    port, server = _serve(state)
    try:
        code, payload, _ = _status(port, "/api/login", "POST",
                                   {"password": PWD, "remember_me": True})
        token = payload["token"]
        assert token, payload

        # 模拟进程重启：同一配置重新构造 AppState（会话从 sessions.json 恢复）
        reborn = AppState(state.config, Scheduler(state.config))
        assert reborn.token_valid(token), "重启后 30 天免登录应仍然有效"
        port2, server2 = _serve(reborn)
        try:
            code, payload, _ = _status(port2, "/api/state",
                                       headers={"Authorization": f"Bearer {token}"})
            assert code == 200, (code, payload)
        finally:
            server2.shutdown()
        # 落盘文件存在且不含明文密码
        sessions_file = tmp / "sessions.json"
        assert sessions_file.is_file(), sessions_file
        assert PWD not in sessions_file.read_text(encoding="utf-8")
        print("ok: 会话持久化（进程重启后免登录依旧有效）")
    finally:
        state._clear_login_throttle()
        server.shutdown()


def check_qr_svg() -> None:
    svg = qr_svg("http://192.168.1.5:5116")
    assert svg and "<svg" in svg and "http://192.168.1.5:5116" not in svg  # 二维码不回显明文 URL
    assert qr_svg("") is None
    print("ok: 二维码 SVG 生成")


def check_lan_ranking() -> None:
    """网卡排序：真实局域网网段（192.168.x）优先于 VPN/代理虚拟网卡——
    默认路由指向 tun 假 IP（198.18.0.0/15）的机器上，二维码不能指到手机够不到的地址。"""
    import orcsy.web as webmod

    class FakeSock:
        def __init__(self, *a, **k):  # noqa: ANN002, ANN003 — socket 接口替身
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, v):
            pass

        def connect(self, addr):
            pass

        def getsockname(self):
            return ("198.18.0.1", 0)  # 模拟默认路由指向 VPN tun 网卡

    orig_socket = webmod.socket.socket
    orig_gai = webmod.socket.getaddrinfo
    webmod.socket.socket = FakeSock
    webmod.socket.getaddrinfo = lambda *a, **k: [
        (2, 1, 6, "", ("10.222.222.1", 0)),    # VPN/虚拟段
        (2, 1, 6, "", ("192.168.5.153", 0)),   # 真实 Wi-Fi 网段
        (2, 1, 6, "", ("169.254.9.9", 0)),     # 链路本地（应剔除）
        (2, 1, 6, "", ("127.0.0.1", 0)),       # 回环（应剔除）
    ]
    try:
        ips = webmod.lan_ipv4_addresses()
        assert ips[0].startswith("192.168."), ips
        assert "169.254.9.9" not in ips and "127.0.0.1" not in ips, ips
        assert "198.18.0.1" in ips, ips  # 候选仍列出（用户可手动切换），只是排最后
        assert ips[-1].startswith("198.18."), ips
    finally:
        webmod.socket.socket = orig_socket
        webmod.socket.getaddrinfo = orig_gai
    print("ok: 网卡排序（真实 Wi-Fi 网段优先，VPN 假 IP 网段靠后）")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="orcsy-mobile-"))
    try:
        check_trusted_proxies_resolution()
        check_qr_svg()
        check_lan_ranking()
        check_one_click_enable(tmp)
        check_enable_without_password(tmp)
        check_session_persistence(tmp)
        check_dual_auth(tmp)
        check_login_throttle_no_loopback_lockout(tmp)
        check_task_concurrency_conflict_409(tmp)
        print("\n移动端远程访问全部通过 ✅")
        return 0
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
