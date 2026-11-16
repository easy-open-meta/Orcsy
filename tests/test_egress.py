"""统一出口白名单（V1-R6）自检：判定矩阵/审计/配置推导/各出网点接入。"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import orcsy.audit as A
import orcsy.egress as E
import orcsy.issues as issues_mod
import orcsy.llm as llm_mod
import orcsy.mirrors as mirrors_mod
import orcsy.updater as updater_mod
from orcsy.config import (AppConfig, EgressConfig, LLMConfig, ProjectConfig,
                          RunLogConfig, UpdateConfig, WebConfig, load_config,
                          save_config)
from orcsy.llm import LLMClient, LLMError, list_models
from orcsy.scheduler import Scheduler
from orcsy.tools_net import tool_web_fetch
from orcsy.web import AppState, make_server


def check_policy_matrix() -> None:
    """20 个 URL 的 allow/deny 矩阵：模型端点 / 通配 / 端口 / 回环 / 拒绝。"""
    policy = E.EgressPolicy(
        mode="allowlist",
        extra_hosts=["*.example.com", "port.example.net:8443",
                     "docs.python.org", "*.corp.internal"],
        derived_hosts=["https://api.deepseek.com/v1"],
    )
    cases: list[tuple[str, bool]] = [
        # 配置推导（模型端点）
        ("https://api.deepseek.com/v1/chat/completions", True),
        ("https://api.deepseek.com/models", True),
        # 回环：不是出网
        ("http://127.0.0.1:5116/api/state", True),
        ("http://127.0.0.1:9222/json/version", True),
        ("http://localhost:8080/ready", True),
        ("http://[::1]:5116/", True),
        # 精确主机与通配（通配同时含主域）
        ("https://docs.python.org/3/", True),
        ("https://example.com/", True),
        ("https://sub.example.com/a", True),
        ("https://a.b.example.com/a", True),
        # 端口限定
        ("https://port.example.net:8443/x", True),
        ("https://port.example.net/x", False),
        ("https://port.example.net:9443/x", False),
        # 未登记主机拒绝
        ("https://evil.example/x", False),
        ("https://example.org/x", False),
        ("https://api.openai.com/v1/chat/completions", False),
        ("http://10.0.0.5/internal", False),
        ("http://169.254.169.254/latest/meta-data", False),
        ("https://api.deepseek.com.evil.example/v1", False),
        ("https://notexample.com/x", False),
        ("https://corp.internal/x", True),
    ]
    assert len(cases) >= 20
    for url, want in cases:
        got = policy.evaluate(url)["allowed"]
        assert got == want, f"{url} 期望 {want} 实际 {got}"

    # off 模式全放行（排障）
    off = E.EgressPolicy(mode="off")
    assert off.evaluate("https://evil.example/x")["allowed"] is True

    # 快照只读字段完整
    snap = policy.snapshot()
    assert snap["mode"] == "allowlist"
    assert snap["allowed"] == 0 and snap["denied"] == 0
    print("ok: 20+ URL 白名单判定 / off 模式 / 快照")


def check_deny_audit() -> None:
    """拒绝写审计：action=egress.deny，query 里的 token 不落盘，计数递增。"""
    tmp = Path(tempfile.mkdtemp(prefix="egress-audit-"))
    try:
        cfg = AppConfig(egress=EgressConfig(mode="allowlist", audit=True),
                        source_path=tmp / "config.yaml")
        E.reset_for_tests()
        A._STORE = None
        A.init_audit_log(cfg, force=True)
        policy = E.init_egress(cfg)
        secret = "sk-" + "egress" * 6
        try:
            policy.check(f"https://evil.example/v1/chat?access_token={secret}",
                         purpose="model_chat")
            raise AssertionError("未登记主机必须被拒绝")
        except E.EgressDenied as e:
            assert "evil.example" in str(e)
            assert "extra_hosts" in str(e)
        entries = A.get_audit_log().tail(limit=10, action="egress.deny")
        assert len(entries) == 1, entries
        assert entries[0]["actor"] == "system:egress"
        assert entries[0]["target"] == "evil.example"
        raw = (tmp / "logs" / "audit.jsonl").read_text(encoding="utf-8")
        assert secret not in raw, "审计不得落 access_token 原文"
        assert "model_chat" in raw
        snap = policy.snapshot()
        assert snap["denied"] == 1 and snap["allowed"] == 0
        assert snap["last_denied"][0]["host"] == "evil.example"
        print("ok: 拒绝写审计 / 敏感 query 不落盘 / 计数")
    finally:
        A._STORE = None
        E.reset_for_tests()
        shutil.rmtree(tmp, ignore_errors=True)


def check_config_and_derivation() -> None:
    """配置读写 + 白名单自动推导：模型 / Issue / 更新源 / 内置与自定义镜像。"""
    tmp = Path(tempfile.mkdtemp(prefix="egress-cfg-"))
    try:
        cfg_file = tmp / "config.yaml"
        doc = {
            "llm": {"api_key": "k", "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-chat"},
            "projects": [{
                "name": "demo", "path": str(tmp / "demo"),
                "repo": {"url": "https://github.com/acme/demo"},
            }],
            "update": {"repo": "https://gitee.com/acme/orcsy", "auto_check": False},
            "mirrors": {"custom": {"goproxy": ["https://goproxy.internal.example/"]}},
            "egress": {"mode": "allowlist", "audit": False,
                       "extra_hosts": ["docs.internal.example", "*.corp.example:8443"]},
        }
        cfg_file.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        cfg = load_config(cfg_file, require_api_key=False)
        assert cfg.egress.mode == "allowlist"
        assert cfg.egress.audit is False
        assert cfg.egress.extra_hosts == ["docs.internal.example", "*.corp.example:8443"]

        policy = E.init_egress(cfg)
        snap = policy.snapshot()
        derived = set(snap["derived_hosts"])
        for host in ("api.deepseek.com", "github.com", "gitee.com",
                     "goproxy.cn", "goproxy.internal.example"):
            assert host in derived, (host, sorted(derived))
        # 额外主机与通配生效
        assert policy.evaluate("https://docs.internal.example/a")["allowed"]
        assert policy.evaluate("https://x.corp.example:8443/a")["allowed"]
        assert not policy.evaluate("https://x.corp.example/a")["allowed"]

        # 保存回写不丢 egress（防看板任意保存静默重置）
        cfg.egress.extra_hosts.append("later.example")
        save_config(cfg)
        cfg2 = load_config(cfg_file, require_api_key=False)
        assert cfg2.egress.extra_hosts == ["docs.internal.example",
                                           "*.corp.example:8443", "later.example"]
        assert cfg2.egress.audit is False

        # 校验：带 scheme 的条目拒绝
        try:
            E.validate_extra_hosts(["https://example.com"])
            raise AssertionError("extra_hosts 不应接受带 scheme 的 URL")
        except ValueError as e:
            assert "不要带" in str(e)
        print("ok: 配置读写 / 自动推导 / extra_hosts 校验")
    finally:
        E.reset_for_tests()
        shutil.rmtree(tmp, ignore_errors=True)


class _FakeResp:
    status_code = 200
    text = ""

    def json(self) -> dict:
        return {"ok": True}


def check_llm_and_tools() -> None:
    """模型调用/模型列表/网页抓取/镜像测速/更新检查/Issues 都过同一阀门。"""
    E.reset_for_tests()
    cfg = AppConfig(
        llm=LLMConfig(api_key="k", base_url="https://api.allowed.example/v1"),
        egress=EgressConfig(mode="allowlist", audit=False),
    )
    E.init_egress(cfg)
    orig_post, orig_get = llm_mod.httpx.post, llm_mod.httpx.get
    calls = {"post": 0, "get": 0}
    try:
        def fake_post(*_a, **_k):
            calls["post"] += 1
            return _FakeResp()

        def fake_get(*_a, **_k):
            calls["get"] += 1
            return _FakeResp()

        llm_mod.httpx.post = fake_post
        llm_mod.httpx.get = fake_get

        # 放行主机：正常发出
        client = LLMClient(LLMConfig(api_key="k", model="m",
                                     base_url="https://api.allowed.example/v1"))
        out = client._post("https://api.allowed.example/v1/chat/completions",
                           {"model": "m"}, {})
        assert out == {"ok": True} and calls["post"] == 1

        # 未登记主机：LLMError，且没有发请求
        bad = LLMClient(LLMConfig(api_key="k", model="m",
                                  base_url="https://evil.example/v1"))
        try:
            bad._post("https://evil.example/v1/chat/completions", {"model": "m"}, {})
            raise AssertionError("未登记模型端点必须被拒绝")
        except LLMError as e:
            assert "出网白名单拦截" in str(e)
        assert calls["post"] == 1, "拒绝后不得发起 HTTP 请求"

        # 拉取模型列表同样过闸
        try:
            list_models("openai", "https://evil.example/v1", "k")
            raise AssertionError("未登记主机拉取模型列表必须被拒绝")
        except LLMError as e:
            assert "出网白名单拦截" in str(e)
        assert calls["get"] == 0

        # web_fetch：拒绝时返回错误文案，不联网
        msg = tool_web_fetch(None, {"url": "https://evil.example/doc"})  # type: ignore[arg-type]
        assert "出网白名单拦截" in msg, msg
        assert calls["get"] == 0

        # 镜像测速：拒绝 → None（批量测速不因单源配置错误中断）
        assert mirrors_mod.speedtest_one("https://evil.example/pkg", timeout=0.2) is None

        # 更新检查 / Issues：领域错误里带白名单提示
        try:
            updater_mod.check_update({"repo": "https://evil.example/o/r"}, "0.1.0")
            raise AssertionError("未登记更新源必须被拒绝")
        except updater_mod.UpdateError as e:
            assert "出网白名单拦截" in str(e)
        try:
            issues_mod._get_json("https://evil.example/api/issues")
            raise AssertionError("未登记 Issue 主机必须被拒绝")
        except issues_mod.IssueError as e:
            assert "出网白名单拦截" in str(e)
        print("ok: 模型/模型列表/网页抓取/镜像/更新/Issues 统一过闸")
    finally:
        llm_mod.httpx.post, llm_mod.httpx.get = orig_post, orig_get
        E.reset_for_tests()


def check_runtime_allow() -> None:
    """更新包 CDN 主机来自已放行的发布 API：运行时放行且可审计。"""
    tmp = Path(tempfile.mkdtemp(prefix="egress-runtime-"))
    try:
        cfg = AppConfig(egress=EgressConfig(mode="allowlist", audit=True),
                        source_path=tmp / "config.yaml")
        E.reset_for_tests()
        A._STORE = None
        A.init_audit_log(cfg, force=True)
        policy = E.init_egress(cfg)
        assert not policy.evaluate("https://cdn.example.net/pkg.zip")["allowed"]
        E.allow_runtime_host("https://cdn.example.net/pkg.zip", purpose="update_asset")
        assert policy.evaluate("https://cdn.example.net/pkg.zip")["allowed"]
        entries = A.get_audit_log().tail(limit=5, action="egress.runtime_allow")
        assert len(entries) == 1 and entries[0]["target"] == "cdn.example.net"
        # 新策略仍保留本次进程的运行时条目（设置保存重建策略不丢）
        policy2 = E.init_egress(cfg)
        assert policy2.evaluate("https://cdn.example.net/other.zip")["allowed"]
        print("ok: 更新包 CDN 运行时放行 / 审计 / 重建保留")
    finally:
        A._STORE = None
        E.reset_for_tests()
        shutil.rmtree(tmp, ignore_errors=True)


def _serve(state: AppState):
    server = make_server(state, "127.0.0.1", 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def _call(port: int, path: str, *, body: dict | None = None,
          tries: int = 5) -> tuple[int, dict]:
    data = None
    headers = {"Connection": "close"}
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    last: Exception | None = None
    for _ in range(tries):
        try:
            req = Request(f"http://127.0.0.1:{port}{path}", data=data,
                          headers=headers,
                          method="POST" if body is not None else "GET")
            with urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 — 回环抖动手工重试
            last = e
            time.sleep(0.2)
    raise last  # type: ignore[misc]


def check_web_api() -> None:
    """看板 API：GET /api/egress 只读状态 + POST /api/config 保存并热生效。"""
    tmp = Path(tempfile.mkdtemp(prefix="egress-web-"))
    try:
        proj = tmp / "proj"
        (proj / "tasks").mkdir(parents=True)
        (proj / "AGENT.md").write_text("# 指引\n", encoding="utf-8")
        cfg_file = tmp / "config.yaml"
        cfg = AppConfig(
            update=UpdateConfig(auto_check=False),
            llm=LLMConfig(api_key="sk-test", base_url="http://localhost:9/v1",
                          model="m"),
            projects=[ProjectConfig(name="proj", path=proj)],
            runlog=RunLogConfig(enabled=False),
            web=WebConfig(host="127.0.0.1", port=5228, open_browser=False),
            egress=EgressConfig(mode="allowlist", audit=True),
            source_path=cfg_file,
        )
        E.reset_for_tests()
        state = AppState(cfg, Scheduler(cfg))
        server, port = _serve(state)
        try:
            code, payload = _call(port, "/api/egress")
            assert code == 200 and payload.get("ok"), (code, payload)
            assert payload["egress"]["mode"] == "allowlist"
            assert "localhost" in payload["egress"]["derived_hosts"]

            code, payload = _call(port, "/api/config", body={
                "egress": {"mode": "allowlist", "audit": False,
                           "extra_hosts": ["docs.example.net", "*.corp.example"]}})
            assert code == 200, (code, payload)
            assert state.config.egress.extra_hosts == ["docs.example.net",
                                                       "*.corp.example"]
            assert state.config.egress.audit is False
            # 热生效：策略已重建并放行新主机
            assert E.get_egress().evaluate("https://docs.example.net/a")["allowed"]

            code, payload = _call(port, "/api/config", body={
                "egress": {"extra_hosts": ["https://bad.example"]}})
            assert code == 400, (code, payload)
            assert "不要带" in str(payload.get("message") or payload), payload
            print("ok: /api/egress 只读状态 + /api/config 保存热生效 + 非法条目 400")
        finally:
            server.shutdown()
    finally:
        A._STORE = None
        E.reset_for_tests()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_policy_matrix()
    check_deny_audit()
    check_config_and_derivation()
    check_llm_and_tools()
    check_runtime_allow()
    check_web_api()
    print("test_egress: 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
