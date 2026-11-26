"""Keep-alive POST body must be consumed per request (no request-line desync)."""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import load_config
from orcsy.scheduler import Scheduler
from orcsy.web import AppState, make_server


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "config.yaml"
        path.write_text("projects: []\n", encoding="utf-8")
        cfg = load_config(path, require_api_key=False)
        state = AppState(cfg, Scheduler(cfg))
        server = make_server(state, "127.0.0.1", 0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            def post(body: dict) -> tuple[int, dict]:
                raw = json.dumps(body).encode()
                conn.request("POST", "/api/config", body=raw,
                             headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read().decode())

            status, data = post({"logging": {"retention_days": 3}})
            assert status == 200 and data.get("ok"), (status, data)
            # 同一 keep-alive 连接上的第二个 POST：body 必须重新读取
            status, data = post({"logging": {"retention_days": 4}})
            assert status == 200 and data.get("ok"), (status, data)
            # 用新连接读结果，避免测试自身在 keep-alive 连接上的关闭竞态
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/api/state")
            resp = conn.getresponse()
            body = json.loads(resp.read().decode())
            assert resp.status == 200, resp.status
            assert body["logging"]["retention_days"] == 4
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
    print("test_web_keepalive_post: all checks passed")


if __name__ == "__main__":
    main()
