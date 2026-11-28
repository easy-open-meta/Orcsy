"""GitLab/GitHub issue-based approval adapters (no network)."""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.providers import (DingTalkProvider, FeishuProvider,
                             GitHubProvider, GitLabProvider, ProviderError,
                             build_provider)
from orcsy.config import ApprovalProviderConfig

MANIFEST = {
    "request_id": "req-1", "project": "p", "task_id": "t", "title": "风险任务",
    "risk": {"level": "high", "reasons": ["自更新"], "files": ["a.py"],
             "additions": 1, "deletions": 0},
    "commit": "abc", "digest": "digest-1", "required_approvals": 2,
    "allowed_approvers": ["alice@example.com", "bob@example.com"],
}


def check_gitlab() -> None:
    cfg = ApprovalProviderConfig(
        type="gitlab", base_url="https://gitlab.example", project_id="group/proj",
        token="t", callback_secret="s")
    provider = GitLabProvider("gl", cfg)
    with mock.patch("orcsy.providers.gitlab._http_json") as http:
        http.return_value = {"iid": 7, "web_url": "https://gitlab.example/i/7"}
        handle = provider.create(MANIFEST)
        assert handle.external_id == "7"
        assert "/projects/group%2Fproj/issues" in http.call_args.args[1]

        http.return_value = [
            {"system": True, "body": "/approve", "author": {"username": "bot"}},
            {"system": False, "body": "/approve\nlooks good",
             "author": {"username": "alice", "name": "Alice"},
             "created_at": "2026-01-01T00:00:00Z"},
            {"system": False, "body": "/reject not now",
             "author": {"username": "bob", "name": "Bob"},
             "created_at": "2026-01-01T00:01:00Z"},
            {"system": False, "body": "orcsy approve looks good",
             "author": {"username": "carol", "name": "Carol"},
             "created_at": "2026-01-01T00:02:00Z"},
        ]
        decision = provider.poll(handle)
        assert decision is not None
        assert decision.digest == "digest-1"
        assert [(a.id, a.decision) for a in decision.approvers] == [
            ("alice", "approved"), ("bob", "rejected"), ("carol", "approved")]

    payload = {
        "object_kind": "note",
        "user": {"username": "alice", "name": "Alice"},
        "issue": {"iid": 7},
        "object_attributes": {"note": "/approve", "created_at": "2026-01-01"},
    }
    raw = json.dumps(payload).encode()
    decision = provider.verify_callback({"X-Gitlab-Token": "s"}, raw)
    assert decision.external_id == "7"
    assert decision.approvers[0].id == "alice"
    try:
        provider.verify_callback({"X-Gitlab-Token": "bad"}, raw)
        raise AssertionError("错误 GitLab token 应拒绝")
    except ProviderError:
        pass


def check_github() -> None:
    cfg = ApprovalProviderConfig(
        type="github", repo="owner/name", token="t", callback_secret="s")
    provider = GitHubProvider("gh", cfg)
    with mock.patch("orcsy.providers.github._http_json") as http:
        http.return_value = {"number": 12, "html_url": "https://github.com/o/n/issues/12"}
        handle = provider.create(MANIFEST)
        assert handle.external_id == "12"
        assert "/repos/owner/name/issues" in http.call_args.args[1]

        http.return_value = [
            {"body": "/approve", "user": {"login": "alice", "name": "Alice"},
             "created_at": "2026-01-01T00:00:00Z"},
            {"body": "hello", "user": {"login": "bob"}},
        ]
        decision = provider.poll(handle)
        assert decision is not None and decision.digest == "digest-1"
        assert [(a.id, a.decision) for a in decision.approvers] == [
            ("alice", "approved")]

    payload = {
        "action": "created",
        "issue": {"number": 12},
        "comment": {"body": "/approve", "created_at": "2026-01-01",
                    "user": {"login": "alice"}},
    }
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"s", raw, hashlib.sha256).hexdigest()
    decision = provider.verify_callback({"X-Hub-Signature-256": sig}, raw)
    assert decision.external_id == "12"
    assert decision.approvers[0].id == "alice"
    try:
        provider.verify_callback({"X-Hub-Signature-256": "sha256=bad"}, raw)
        raise AssertionError("错误 GitHub 签名应拒绝")
    except ProviderError:
        pass


def check_feishu() -> None:
    cfg = ApprovalProviderConfig(
        type="feishu", app_id="app", app_secret="sec", approval_code="code",
        originator_user_id="ou_origin", form_widget_id="widget")
    provider = FeishuProvider("fs", cfg)
    calls: list[str] = []
    forms: list[str] = []

    def fake(method: str, url: str, **kwargs):
        calls.append(url)
        if "tenant_access_token" in url:
            return {"tenant_access_token": "tok", "expire": 7200}
        if url.endswith("/instances"):
            assert kwargs["headers"]["Authorization"] == "Bearer tok"
            assert kwargs["body"]["uuid"] == "req-1"
            forms.append(kwargs["body"]["form"])
            return {"data": {"instance_code": "inst-1"}}
        return {"data": {"task_list": [
            {"user_id": "ou_a", "status": "APPROVED"},
            {"user_id": "ou_b", "status": "REJECTED"},
            {"user_id": "ou_c", "status": "PENDING"},
        ]}}

    with mock.patch("orcsy.providers.feishu._http_json", side_effect=fake):
        handle = provider.create(MANIFEST)
        assert handle.external_id == "inst-1"
        decision = provider.poll(handle)
    assert decision is not None and decision.digest == "digest-1"
    assert [(a.id, a.decision) for a in decision.approvers] == [
        ("ou_a", "approved"), ("ou_b", "rejected")]
    assert len(calls) == 3

    # 真实飞书新版自定义审批要求 form 为“控件数组”的 JSON 字符串；
    # 旧实现的 map / "{}" 会返回 1390001 表单格式不正确。
    form = json.loads(forms[0])
    assert isinstance(form, list) and form, form
    assert form[0]["id"] == "widget" and form[0]["type"] == "input"
    assert "p/t" in form[0]["value"] and "high" in form[0]["value"]

    # 未配置控件 ID 时提交空数组，而不是旧实现的 "{}"
    cfg_no_widget = ApprovalProviderConfig(
        type="feishu", app_id="app", app_secret="sec", approval_code="code",
        originator_user_id="ou_origin")
    with mock.patch("orcsy.providers.feishu._http_json", side_effect=fake):
        FeishuProvider("fs2", cfg_no_widget).create(MANIFEST)
    assert forms[1] == "[]"

    try:
        provider.verify_callback({}, b"{}")
        raise AssertionError("飞书回调应提示使用轮询")
    except ProviderError as exc:
        assert "轮询" in str(exc)


def check_dingtalk() -> None:
    cfg = ApprovalProviderConfig(
        type="dingtalk", app_key="key", app_secret="sec", process_code="proc",
        originator_user_id="user-1", dept_id=42,
        form_component_values=[{"name": "单行输入框", "value": "审批测试"}])
    provider = DingTalkProvider("dt", cfg)

    def fake(method: str, url: str, **kwargs):
        if "/gettoken" in url:
            return {"access_token": "tok"}
        if "/topapi/processinstance/create" in url:
            assert kwargs["body"]["originator_user_id"] == "user-1"
            assert kwargs["body"]["dept_id"] == 42
            # 钉钉模板通常有表单控件，空数组会被 820001 拒绝
            assert kwargs["body"]["form_component_values"] == [
                {"name": "单行输入框", "value": "审批测试"}]
            return {"process_instance_id": "pi-1"}
        return {"process_instance": {"operation_records": [
            # 真实钉钉字段是 operation_result；result 为旧 mock 兼容路径
            {"userid": "u1", "operation_result": "AGREE", "date": "2026-01-01"},
            {"userid": "u2", "result": "REFUSE", "date": "2026-01-02"},
            {"userid": "u3", "operation_result": "AGREE", "date": "2026-01-03"},
        ]}}

    with mock.patch("orcsy.providers.dingtalk._http_json", side_effect=fake):
        handle = provider.create(MANIFEST)
        assert handle.external_id == "pi-1"
        decision = provider.poll(handle)
    assert decision is not None and decision.digest == "digest-1"
    assert [(a.id, a.decision) for a in decision.approvers] == [
        ("u1", "approved"), ("u2", "rejected"), ("u3", "approved")]
    try:
        provider.verify_callback({}, b"{}")
        raise AssertionError("钉钉回调应提示使用轮询")
    except ProviderError as exc:
        assert "轮询" in str(exc)


def check_approval_command() -> None:
    from orcsy.providers.issue import _parse_approval_command

    assert _parse_approval_command("/approve") == ("approved", "")
    assert _parse_approval_command("/reject 太危险") == ("rejected", "太危险")
    # GitLab：/approve 会被 quick action 拦截，使用无斜杠前缀
    assert _parse_approval_command("orcsy approve looks good") == (
        "approved", "looks good")
    assert _parse_approval_command("Orcsy REJECT 不行") == (
        "rejected", "不行")
    assert _parse_approval_command("orcsy") == ("", "")
    assert _parse_approval_command("orcsy maybe") == ("", "")
    print("审批评论命令解析（含 GitLab 安全写法）✅")


def check_provider_form_values() -> None:
    from orcsy.config import parse_approval_providers, provider_to_yaml

    raw = {"dt": {"type": "dingtalk", "app_key": "k", "app_secret": "s",
                  "process_code": "p", "originator_user_id": "u",
                  "form_component_values": [
                      {"name": "单行输入框", "value": "v"}]}}
    cfg = parse_approval_providers(raw)["dt"]
    expected = [{"name": "单行输入框", "value": "v"}]
    assert cfg.form_component_values == expected
    assert provider_to_yaml(cfg)["form_component_values"] == expected
    for bad in (
        {"form_component_values": "x"},
        {"form_component_values": [{"value": "v"}]},
        {"form_component_values": [{"name": "n", "value": "x" * 2001}]},
        {"form_component_values": [{"name": "n", "value": "v"}] * 51},
    ):
        payload = {"type": "dingtalk", "app_key": "k", "app_secret": "s",
                   "process_code": "p", "originator_user_id": "u", **bad}
        try:
            parse_approval_providers({"dt": payload})
            raise AssertionError(f"invalid form_component_values accepted: {bad}")
        except ValueError:
            pass
    print("Provider form_component_values 解析与序列化 ✅")


def check_build_provider() -> None:
    assert isinstance(build_provider("x", ApprovalProviderConfig(type="gitlab",
                               project_id="1", token="t")), GitLabProvider)
    assert isinstance(build_provider("x", ApprovalProviderConfig(type="github",
                               repo="o/n", token="t")), GitHubProvider)
    fake = build_provider("x", ApprovalProviderConfig(type="fake"))
    assert fake.type == "fake"

    # 注册表与 config 的合法类型清单必须一致（新增厂商只改一处会在此失败）
    from orcsy.config import APPROVAL_PROVIDER_TYPES
    from orcsy.providers import PROVIDER_TYPES
    assert set(APPROVAL_PROVIDER_TYPES) == set(PROVIDER_TYPES)
    for ptype in PROVIDER_TYPES:
        cfg = ApprovalProviderConfig(type=ptype, project_id="1", token="t",
                                     repo="o/n")
        built = build_provider(f"p-{ptype}", cfg)
        assert built.type == ptype, built.type

    # 旧路径 shim 必须继续 re-export 同一个工厂（兼容外部导入）
    import orcsy.approval_providers as legacy
    assert legacy.build_provider is build_provider


def check_loopback_proxy_bypass() -> None:
    """本机 Provider 不走系统代理，公网 Provider 仍走。"""
    from orcsy.providers.base import _http_json, _is_loopback_url

    assert _is_loopback_url("http://127.0.0.1:8000/x")
    assert _is_loopback_url("http://127.8.9.10/x")
    assert _is_loopback_url("http://localhost:3000/x")
    assert _is_loopback_url("http://[::1]:9000/x")
    assert not _is_loopback_url("https://gitlab.com/x")
    assert not _is_loopback_url("https://127.example.com/x")

    seen: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"ok": True}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, *args, **kwargs):
            return FakeResponse()

    with mock.patch("orcsy.providers.base.httpx.Client", FakeClient), \
            mock.patch("orcsy.egress.check"):
        _http_json("GET", "http://127.0.0.1:9/x", headers={})
        assert seen["trust_env"] is False
        _http_json("GET", "https://gitlab.example/x", headers={})
        assert seen["trust_env"] is True


def main() -> None:
    check_gitlab()
    check_github()
    check_feishu()
    check_dingtalk()
    check_approval_command()
    check_provider_form_values()
    check_build_provider()
    check_loopback_proxy_bypass()
    print("test_approval_adapters: all checks passed")


if __name__ == "__main__":
    main()
