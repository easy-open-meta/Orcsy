"""Project premerge gate configuration preserves old settings and rejects unsafe inputs."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orcsy.config import load_config, save_config
from orcsy.scheduler import Scheduler
from orcsy.web import AppState


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        path = root / "config.yaml"
        path.write_text("projects: []\n", encoding="utf-8")
        cfg = load_config(path, require_api_key=False)
        state = AppState(cfg, Scheduler(cfg))
        project = root / "project"
        project.mkdir()
        payload = {
            "name": "demo", "path": str(project),
            "premerge_test_enabled": True,
            "premerge_test_command": ["python", "-c", "print('ok')"],
        }
        state.save_project(payload)
        state._save_config(state.config)
        loaded = load_config(path, require_api_key=False)
        assert loaded.projects[0].premerge_test_enabled
        assert loaded.projects[0].premerge_test_command == payload["premerge_test_command"]
        assert state.full_state()["projects"][0]["premerge_test_enabled"] is True
        assert state.full_state()["projects"][0]["premerge_test_command"] == payload["premerge_test_command"]
        for value in ([], ["not-whitelisted"], "python test.py"):
            try:
                state.save_project({**payload, "original_name": "demo", "premerge_test_command": value})
                raise AssertionError(f"invalid command accepted: {value}")
            except ValueError:
                pass
        assert state.config.projects[0].premerge_test_command == payload["premerge_test_command"]
        state.save_project({**payload, "original_name": "demo", "premerge_test_enabled": False})
        save_config(state.config)
        disabled = load_config(path, require_api_key=False).projects[0]
        assert disabled.premerge_test_enabled is False
        assert disabled.premerge_test_command == payload["premerge_test_command"]
        state.save_project({**payload, "original_name": "demo", "premerge_test_enabled": False,
                            "premerge_test_command": None})
        assert state.config.projects[0].premerge_test_enabled is False
        save_config(state.config)
        assert not load_config(path, require_api_key=False).projects[0].premerge_test_enabled
    print("test_premerge_config: all checks passed")


if __name__ == "__main__":
    main()
