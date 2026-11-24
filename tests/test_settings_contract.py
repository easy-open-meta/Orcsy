"""Direct settings-save evidence; does not replace test_api_external.py's HTTP test.

Run: uv run python tests/test_settings_contract.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.auth import hash_password
from orcsy.config import AppConfig, LLMConfig, ReviewConfig, UpdateConfig, WebConfig, load_config, save_config
from orcsy.scheduler import Scheduler
from orcsy.web import AppState, mask_secret

TEST_KEY_1 = "sk-orcsy-test-key-alpha-12345"
TEST_KEY_2 = "sk-orcsy-test-key-beta-67890"


def _assert_masked_output(state: AppState) -> None:
    output = state.full_state()
    serialized = json.dumps(output, ensure_ascii=False)
    for secret in (TEST_KEY_1, TEST_KEY_2, "sk-test", "review-secret", "update-secret", "test-login-password"):
        assert secret not in serialized, "full_state leaked a configured secret"
    web = output["web_cfg"]
    assert web["auth_enabled"] is True
    assert web["api_keys_configured"] is True
    assert "api_keys" not in web
    assert web["api_keys_masked"] == [mask_secret(TEST_KEY_1), mask_secret(TEST_KEY_2)]
    for masked in web["api_keys_masked"]:
        assert masked not in (TEST_KEY_1, TEST_KEY_2)
        assert "••••" in masked or "…" in masked
    assert "api_key" not in output["llm"]
    assert output["llm"]["key_configured"] is True
    assert output["llm"]["api_key_masked"] == mask_secret("sk-test")
    assert "api_key" not in output["review_cfg"]
    assert output["review_cfg"]["model_configured"] is True
    assert output["review_cfg"]["api_key_masked"] == mask_secret("review-secret")
    assert "token" not in output["update_cfg"]
    assert output["update_cfg"]["token_configured"] is True
    assert output["update_cfg"]["token_masked"] == mask_secret("update-secret")


def test_save_settings_section_and_secret_contract() -> None:
    """Save the HTTP regression's four payloads directly, checking every round trip."""
    with tempfile.TemporaryDirectory(prefix="orcsy-test-settings-contract-") as directory:
        root = Path(directory)
        config = AppConfig(
            llm=LLMConfig(provider="openai", api_key="sk-test", base_url="http://test/v1", model="test"),
            web=WebConfig(
                host="127.0.0.1",
                # HTTP uses port=0 for ephemeral binding; save_settings requires 1..65535.
                port=8080,
                # Normalize upfront so unrelated-section equality is not affected by hashing.
                auth_password=hash_password("test-login-password"),
                api_keys=[TEST_KEY_1, TEST_KEY_2],
                cors_origins=["*"],
            ),
            projects=[],
            update=UpdateConfig(repo="https://example.com/releases", token="update-secret", auto_check=False),
            review=ReviewConfig(
                llm=LLMConfig(provider="openai", api_key="review-secret",
                              base_url="https://example.com/v1", model="review-model"),
            ),
            source_path=root / "config.yaml",
        )
        config.llm.extra_body = {"provider_options": {"audit": True}}
        save_config(config)
        state = AppState(config, Scheduler(config), log_path=str(root / "orcsy.log"))
        _assert_masked_output(state)

        payloads = [
            {"notify": {"sound": False, "system": True}},
            {"update": {"repo": "https://example.com/new", "auto_check": False}},
            {"llm": {
                "provider": "openai", "model": "test-new", "base_url": "https://example.com/v1",
                "temperature": 0.4, "max_tokens": 4096, "max_context_tokens": 131072,
                "enable_thinking": False, "timeout_seconds": 120,
                "extra_body": {"provider_options": {"audit": True}}, "tool_protocol": "native",
            }},
            {"review": {
                "enabled": True,
                "llm": {"provider": "openai", "model": "review-new", "base_url": "https://example.com/v1"},
                "max_turns": 12,
            }},
        ]
        for payload in payloads:
            section = next(iter(payload))
            before_memory = asdict(state.config)
            before_disk = asdict(load_config(config.source_path, require_api_key=False))
            result = state.save_settings(payload)
            assert result.get("ok") is True, (section, result)
            persisted = load_config(config.source_path, require_api_key=False)
            after_memory = asdict(state.config)
            after_disk = asdict(persisted)
            for name in before_memory:
                if name != section:
                    assert after_memory[name] == before_memory[name], (section, name, "memory changed")
                    assert after_disk[name] == before_disk[name], (section, name, "disk changed")
            assert after_memory[section] == after_disk[section], (section, "round trip changed saved section")
            for current in (state.config, persisted):
                assert current.web.api_keys == [TEST_KEY_1, TEST_KEY_2]
                assert current.llm.api_key == "sk-test"
                assert current.llm.extra_body == {"provider_options": {"audit": True}}
                assert current.update.token == "update-secret"
                assert current.review.llm is not None
                assert current.review.llm.api_key == "review-secret"
            _assert_masked_output(state)
            print(f"PASS: direct {section} save; unrelated sections intact in memory/on disk; secrets preserved/masked")

        assert persisted.notify.sound is False and persisted.notify.system is True
        assert persisted.update.repo == "https://example.com/new"
        assert persisted.update.auto_check is False
        assert persisted.llm.model == "test-new"
        assert persisted.llm.temperature == 0.4
        assert persisted.llm.max_tokens == 4096
        assert persisted.llm.timeout_seconds == 120
        assert persisted.review.enabled is True
        assert persisted.review.llm.model == "review-new"
        assert persisted.review.max_turns == 12
        refreshed = state.full_state()
        assert refreshed["notify_cfg"] == {"sound": False, "system": True}
        assert refreshed["review_cfg"]["model"] == "review-new"


if __name__ == "__main__":
    test_save_settings_section_and_secret_contract()
    print("PASS: direct AppState.save_settings contract (1 test, 4 section saves); HTTP outcome unchanged")
