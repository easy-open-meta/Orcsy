"""HTTP callback endpoint: HMAC verification, quorum wake-up and auth bypass."""

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

from orcsy.approval_providers import sign_callback
from orcsy.audit import get_audit_log
from orcsy.claims import claims_state_dir
from orcsy.config import load_config
from orcsy.quorum_approval import get_manager
from orcsy.scheduler import Scheduler
from orcsy.web import AppState, make_server

SECRET = "secret-1"
MAP = {
    "alice@example.com": {"fake": "alice"},
    "bob@example.com": {"fake": "bob"},
}
ALLOWED = ["alice@example.com", "bob@example.com"]


def _post(port: int, path: str, body: bytes,
          headers: dict[str, str] | None = None) -> tuple[int, dict]:
    last: Exception | None = None
    for _ in range(5):
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}", data=body, method="POST",
                headers={"Content-Type": "application/json", **(headers or {})})
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            last = exc
            time.sleep(0.3)
    raise last  # type: ignore[misc]


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        path = root / "config.yaml"
        path.write_text(
            "projects: []\n"
            "approval_providers:\n"
            "  fake1:\n"
            "    type: fake\n"
            f"    callback_secret: {SECRET}\n"
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
            "  callback_url: \"https://orcsy.example/api/risk-approval/callback/{provider}\"\n",
            encoding="utf-8")
        cfg = load_config(path, require_api_key=False)
        state = AppState(cfg, Scheduler(cfg))
        mgr = get_manager(claims_state_dir(cfg))
        mgr.clear()
        server = make_server(state, "127.0.0.1", 0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            box: dict = {}

            def worker():
                box["outcome"] = mgr.request(
                    project="p", task_id="t", title="风险任务",
                    risk_level="high", reasons=["自更新逻辑"],
                    files=["orcsy/updater.py"], additions=1, deletions=1,
                    branch="task", commit="abc", digest="digest-web",
                    provider_name="fake1",
                    provider_cfg=cfg.approval_providers["fake1"],
                    approver_map=MAP, allowed_approvers=ALLOWED, required=2,
                    requested_by="agent:backend", timeout_seconds=10,
                    poll_interval_seconds=5,
                    callback_url=cfg.risk_approval.callback_url)

            t = threading.Thread(target=worker)
            t.start()
            deadline = time.time() + 5
            pending = []
            while time.time() < deadline:
                pending = mgr.pending()
                if pending:
                    break
                time.sleep(0.02)
            assert pending, "请求未创建"
            req = pending[0]
            rid, ext = req["id"], req["external_id"]

            # full_state 暴露 quorum 进度
            fs = state.full_state()
            items = [a for a in fs["risk_approvals"] if a.get("mode") == "quorum"]
            assert items and items[0]["required"] == 2

            payload = {
                "decision": "approved", "request_id": rid,
                "external_id": ext, "digest": "digest-web",
                "approvers": [
                    {"provider": "fake", "id": "alice", "name": "Alice",
                     "decision": "approved"},
                    {"provider": "fake", "id": "bob", "name": "Bob",
                     "decision": "approved"},
                ],
            }
            raw = json.dumps(payload).encode()
            headers, _ = sign_callback(SECRET, raw)
            status, body = _post(port, "/api/risk-approval/callback/fake1",
                                 raw, headers)
            assert status == 200 and body.get("ok"), (status, body)
            t.join(5)
            assert not t.is_alive()
            assert box["outcome"].approved
            assert box["outcome"].request.approved_count == 2

            # 错误签名：401，不改变请求
            payload["approvers"] = []
            bad_raw = json.dumps(payload).encode()
            bad_headers, _ = sign_callback("wrong", bad_raw)
            status, body = _post(port, "/api/risk-approval/callback/fake1",
                                 bad_raw, bad_headers)
            assert status == 401 and not body.get("ok"), (status, body)

            # 回调端点免登录：无 Cookie / 无 API Key 也能到达（由签名保证安全）
            assert state.auth_enabled is False
            entries = get_audit_log().tail(limit=20,
                                           action="risk_approval.approve")
            assert entries, "缺少 risk_approval.approve 审计"
        finally:
            mgr.clear()
            server.shutdown()
            server.server_close()
    print("test_quorum_callback_web: all checks passed")


if __name__ == "__main__":
    main()
