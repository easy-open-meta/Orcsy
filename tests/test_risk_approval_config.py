"""Risk approval config persistence/validation and web decision endpoint."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.audit import get_audit_log
from orcsy.config import load_config, save_config
from orcsy.risk_approval import get_manager as get_risk_manager
from orcsy.scheduler import Scheduler
from orcsy.web import AppState, make_server


def _post(port: int, path: str, body: dict) -> tuple[int, dict]:
    last: Exception | None = None
    for _ in range(5):
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}",
                data=json.dumps(body).encode(), method="POST",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last = e
            time.sleep(0.3)
    raise last  # type: ignore[misc]


def _make_state(root: Path) -> tuple[AppState, Path]:
    path = root / "config.yaml"
    path.write_text("projects: []\n", encoding="utf-8")
    cfg = load_config(path, require_api_key=False)
    return AppState(cfg, Scheduler(cfg)), path


def check_config() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        state, path = _make_state(root)
        state.save_settings({"risk_approval": {
            "enabled": True,
            "policy": {"high": "single", "medium": "off", "low": "off"},
            "timeout_minutes": 30,
            "extra_high_risk_paths": ["src/billing/**"],
        }})
        cfg = state.config.risk_approval
        assert cfg.enabled is True
        assert cfg.policy["high"] == "single"
        assert cfg.timeout_minutes == 30
        assert cfg.extra_high_risk_paths == ["src/billing/**"]
        fs = state.full_state()
        assert fs["risk_approval_cfg"]["enabled"] is True
        assert fs["risk_approval_cfg"]["timeout_minutes"] == 30
        assert fs["risk_approvals"] == []
        save_config(state.config)
        loaded = load_config(path, require_api_key=False).risk_approval
        assert loaded == cfg

        # 部分更新：只改开关，策略/超时/路径保持
        state.save_settings({"risk_approval": {"enabled": False}})
        cfg2 = state.config.risk_approval
        assert cfg2.enabled is False
        assert cfg2.policy["high"] == "single"
        assert cfg2.timeout_minutes == 30
        assert cfg2.extra_high_risk_paths == ["src/billing/**"]

        bad_payloads = [
            {"policy": {"high": "quorum"}},
            {"policy": {"high": "two"}},
            {"policy": {"critical": "single"}},
            {"policy": "single"},
            {"timeout_minutes": 0},
            {"timeout_minutes": 1441},
            {"timeout_minutes": True},
            {"timeout_minutes": "60"},
            {"extra_high_risk_paths": [""]},
            {"extra_high_risk_paths": ["bad\x00path"]},
            {"extra_high_risk_paths": [f"p{i}" for i in range(51)]},
            "not-a-dict",
        ]
        before = state.config.risk_approval
        for bad in bad_payloads:
            before = state.config.risk_approval
            try:
                state.save_settings({"risk_approval": bad})
                raise AssertionError(f"invalid risk_approval accepted: {bad}")
            except ValueError:
                pass
            assert state.config.risk_approval == before, f"失败保存污染配置：{bad}"

        # load_config 同样拒绝手工写入的非法策略
        path.write_text(
            "projects: []\nrisk_approval:\n  policy:\n    high: quorum\n",
            encoding="utf-8")
        try:
            load_config(path, require_api_key=False)
            raise AssertionError("invalid risk_approval loaded")
        except ValueError as exc:
            assert "多人会签" in str(exc) or "quorum" in str(exc)


def check_web_decisions() -> None:
    with tempfile.TemporaryDirectory() as td:
        state, _ = _make_state(Path(td))
        mgr = get_risk_manager()
        mgr.clear()
        try:
            box: dict = {}

            def worker():
                box["outcome"] = mgr.request(
                    project="p", task_id="t", title="风险", risk_level="high",
                    reasons=["自更新逻辑：orcsy/updater.py"],
                    files=["orcsy/updater.py"], additions=2, deletions=1,
                    branch="task", commit="abc", digest="digest-1",
                    timeout_seconds=10, requested_by="agent:backend")

            t = threading.Thread(target=worker)
            t.start()
            deadline = time.time() + 5
            pending = []
            while time.time() < deadline:
                pending = state.full_state()["risk_approvals"]
                if pending:
                    break
                time.sleep(0.02)
            assert pending, "full_state 未暴露 pending 风险审批"
            assert pending[0]["risk_level"] == "high"
            rid = pending[0]["id"]

            # API Key 不可代批
            try:
                state.risk_approval_respond(
                    {"id": rid, "approved": True}, actor="api_key:abcd")
                raise AssertionError("API Key 不应能审批")
            except ValueError as exc:
                assert "API Key" in str(exc)
            assert state.full_state()["risk_approvals"], "API Key 被拒后请求不应消失"

            # 会话批准
            r = state.risk_approval_respond(
                {"id": rid, "approved": True, "comment": "ok"},
                actor="user", ip="127.0.0.1")
            assert r == {"ok": True}
            t.join(5)
            assert not t.is_alive()
            assert box["outcome"].approved
            assert box["outcome"].request.decided_by == "user"

            # 未知/已处理 id
            try:
                state.risk_approval_respond({"id": "nope", "approved": True},
                                            actor="user")
                raise AssertionError("未知 id 应失败")
            except ValueError:
                pass
            try:
                state.risk_approval_respond({"id": rid, "approved": True},
                                            actor="user")
                raise AssertionError("已处理 id 应失败")
            except ValueError:
                pass
            try:
                state.risk_approval_respond({"id": "x", "approved": "yes"},
                                            actor="user")
                raise AssertionError("approved 非布尔应失败")
            except ValueError:
                pass

            # 审计：批准决定有 actor/target/digest
            entries = get_audit_log().tail(limit=20, action="risk_approval.approve")
            assert entries, "缺少 risk_approval.approve 审计"
            detail = entries[0]["detail"]
            assert entries[0]["actor"] == "user"
            assert "p/t" == entries[0]["target"]
            assert "digest-1" in detail

            # HTTP 路由：/api/risk-approval 与 actor 透传
            server = make_server(state, "127.0.0.1", 0)
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                box.clear()
                t = threading.Thread(target=worker)
                t.start()
                deadline = time.time() + 5
                pending = []
                while time.time() < deadline:
                    pending = state.full_state()["risk_approvals"]
                    if pending:
                        break
                    time.sleep(0.02)
                assert pending, "HTTP 测试前 pending 未出现"
                status, body = _post(port, "/api/risk-approval",
                                     {"id": pending[0]["id"], "approved": True})
                assert status == 200 and body.get("ok"), (status, body)
                t.join(5)
                assert not t.is_alive()
                assert box["outcome"].approved
            finally:
                server.shutdown()
                server.server_close()
        finally:
            mgr.clear()


def main() -> None:
    check_config()
    check_web_decisions()
    print("test_risk_approval_config: all checks passed")


if __name__ == "__main__":
    main()
