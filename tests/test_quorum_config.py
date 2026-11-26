"""Phase 2 config: approval providers, approver emails and quorum policy."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import load_config, save_config
from orcsy.scheduler import Scheduler
from orcsy.web import AppState

PROVIDERS = {
    "fake1": {"type": "fake", "callback_secret": "secret-1"},
}
APPROVERS = {
    "alice@example.com": {"fake": "alice"},
    "bob@example.com": {"fake": "bob"},
}
RISK = {
    "enabled": True,
    "policy": {"low": "off", "medium": "off", "high": "quorum"},
    "provider": "fake1",
    "quorum": {"high": 2},
    "allowed_approvers": {
        "high": ["alice@example.com", "bob@example.com"],
    },
    "callback_url": "https://orcsy.example/api/risk-approval/callback/{provider}",
    "poll_interval_seconds": 20,
}


def _write(path: Path, extra: str = "") -> None:
    path.write_text(
        "config_version: 3\n"
        "projects: []\n"
        "approval_providers:\n"
        "  fake1:\n"
        "    type: fake\n"
        "    callback_secret: secret-1\n"
        "approvers:\n"
        "  alice@example.com:\n"
        "    fake: alice\n"
        "  bob@example.com:\n"
        "    fake: bob\n"
        "risk_approval:\n"
        "  enabled: true\n"
        "  policy:\n"
        "    high: quorum\n"
        "  provider: fake1\n"
        "  quorum:\n"
        "    high: 2\n"
        "  allowed_approvers:\n"
        "    high: [alice@example.com, bob@example.com]\n"
        '  callback_url: "https://orcsy.example/api/risk-approval/callback/{provider}"\n'
        + extra,
        encoding="utf-8")


def check_load_save() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "config.yaml"
        _write(path)
        cfg = load_config(path, require_api_key=False)
        assert cfg.risk_approval.policy["high"] == "quorum"
        assert cfg.risk_approval.quorum["high"] == 2
        assert cfg.risk_approval.allowed_approvers["high"] == [
            "alice@example.com", "bob@example.com"]
        assert cfg.approval_providers["fake1"].type == "fake"
        assert cfg.approvers["alice@example.com"]["fake"] == "alice"
        save_config(cfg)
        again = load_config(path, require_api_key=False)
        assert again.risk_approval == cfg.risk_approval
        assert again.approval_providers == cfg.approval_providers
        assert again.approvers == cfg.approvers


def check_invalid() -> None:
    cases = [
        # quorum 但没有 provider
        "risk_approval:\n  enabled: true\n  policy:\n    high: quorum\n",
        # provider 不存在
        "risk_approval:\n  enabled: true\n  policy:\n    high: quorum\n"
        "  provider: nope\n  quorum:\n    high: 2\n"
        "  allowed_approvers:\n    high: [alice@example.com, bob@example.com]\n",
        # quorum 只有 1 人
        "risk_approval:\n  enabled: true\n  policy:\n    high: quorum\n"
        "  provider: fake1\n  quorum:\n    high: 1\n"
        "  allowed_approvers:\n    high: [alice@example.com]\n",
        # email 不在 approvers 注册
        "risk_approval:\n  enabled: true\n  policy:\n    high: quorum\n"
        "  provider: fake1\n  quorum:\n    high: 2\n"
        "  allowed_approvers:\n    high: [alice@example.com, carol@example.com]\n",
        # callback_url 非 https/不带 {provider}
        "risk_approval:\n  enabled: true\n  callback_url: http://example.com/cb\n",
        "risk_approval:\n  enabled: true\n  callback_url: https://example.com/cb\n",
    ]
    for extra in cases:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.yaml"
            _write(path, extra)
            try:
                load_config(path, require_api_key=False)
                raise AssertionError(f"invalid quorum config loaded: {extra}")
            except ValueError:
                pass

    # 同一 Provider 身份映射两个 email
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "config.yaml"
        path.write_text(
            "config_version: 3\nprojects: []\n"
            "approvers:\n"
            "  alice@example.com:\n    fake: same\n"
            "  bob@example.com:\n    fake: same\n",
            encoding="utf-8")
        try:
            load_config(path, require_api_key=False)
            raise AssertionError("duplicate provider identity accepted")
        except ValueError as exc:
            assert "同时映射" in str(exc)


def check_settings_and_full_state() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "config.yaml"
        path.write_text("projects: []\n", encoding="utf-8")
        cfg = load_config(path, require_api_key=False)
        state = AppState(cfg, Scheduler(cfg))
        state.save_settings({
            "approval_providers": PROVIDERS,
            "approvers": APPROVERS,
            "risk_approval": RISK,
        })
        ra = state.config.risk_approval
        assert ra.provider == "fake1" and ra.quorum["high"] == 2
        fs = state.full_state()
        provider = fs["approval_providers"]["fake1"]
        assert provider["callback_secret_configured"] is True
        assert provider["callback_secret_masked"] != "secret-1"
        assert "callback_secret" not in provider
        assert fs["approvers"]["alice@example.com"]["fake"] == "alice"
        assert fs["risk_approval_cfg"]["provider"] == "fake1"
        assert fs["risk_approvals"] == []

        # secret 三态：省略保持；清空 secret 时若 callback_url 仍在必须失败
        state.save_settings({"approval_providers": {
            "fake1": {"type": "fake"}}})
        assert state.config.approval_providers["fake1"].callback_secret == "secret-1"
        try:
            state.save_settings({"approval_providers": {
                "fake1": {"type": "fake", "callback_secret": ""}}})
            raise AssertionError("callback_url 仍在时清空 secret 应失败")
        except ValueError:
            pass
        state.save_settings({
            "approval_providers": {
                "fake1": {"type": "fake", "callback_secret": ""}},
            "risk_approval": {"callback_url": ""},
        })
        assert state.config.approval_providers["fake1"].callback_secret == ""
        assert state.config.risk_approval.callback_url == ""

        # 删除被 quorum 引用的 Provider 必须失败且不污染配置
        before = state.config.approval_providers
        try:
            state.save_settings({"approval_providers": {}})
            raise AssertionError("removing referenced provider should fail")
        except ValueError:
            pass
        assert state.config.approval_providers == before

        save_config(state.config)
        loaded = load_config(path, require_api_key=False)
        assert loaded.risk_approval.provider == "fake1"
        assert loaded.approvers["bob@example.com"]["fake"] == "bob"


def main() -> None:
    check_load_save()
    check_invalid()
    check_settings_and_full_state()
    print("test_quorum_config: all checks passed")


if __name__ == "__main__":
    main()
