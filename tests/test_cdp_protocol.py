"""Mock WebSocket 验证 CDP 协议命令配对、事件派发与导航拦截逻辑。

运行：uv run python tests/test_cdp_protocol.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

from websockets.sync.server import serve
from orcsy.browser.cdp import CdpClient


def mock_cdp_server(websocket) -> None:
    for raw in websocket:
        msg = json.loads(raw)
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params", {})

        # 应答特定命令
        if method == "Page.enable":
            websocket.send(json.dumps({"id": msg_id, "result": {}}))
        elif method == "Runtime.enable":
            websocket.send(json.dumps({"id": msg_id, "result": {}}))
        elif method == "Log.enable":
            websocket.send(json.dumps({"id": msg_id, "result": {}}))
        elif method == "Network.enable":
            websocket.send(json.dumps({"id": msg_id, "result": {}}))
        elif method == "Fetch.enable":
            websocket.send(json.dumps({"id": msg_id, "result": {}}))
        elif method == "Browser.getVersion":
            websocket.send(json.dumps({
                "id": msg_id,
                "result": {"product": "MockChrome/120.0.0.0"}
            }))
        elif method == "Runtime.evaluate":
            expr = params.get("expression", "")
            if "error_expr" in expr:
                websocket.send(json.dumps({
                    "id": msg_id,
                    "result": {"exceptionDetails": {"text": "ReferenceError: foo is not defined"}}
                }))
            else:
                websocket.send(json.dumps({
                    "id": msg_id,
                    "result": {"result": {"value": "mock_eval_res"}}
                }))
        elif method in ("Fetch.continueRequest", "Fetch.failRequest"):
            # 记录收到的拦截响应
            MockCdpState.last_fetch_action = (method, params)
        else:
            websocket.send(json.dumps({"id": msg_id, "result": {}}))


class MockCdpState:
    last_fetch_action: tuple[str, dict] | None = None


def test_cdp_client_protocol() -> None:
    # 启动本地 Mock WebSocket 服务
    server = serve(mock_cdp_server, "127.0.0.1", 0)
    server_port = server.socket.getsockname()[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    ws_url = f"ws://127.0.0.1:{server_port}/mock"
    client = CdpClient(ws_url, port=server_port, allowed_hosts=["localhost", "127.0.0.1"])
    client.connect()

    try:
        # 1. 基础命令往返配对
        ver = client.send("Browser.getVersion")
        assert ver.get("product") == "MockChrome/120.0.0.0"

        # 2. evaluate 正常返回
        val = client.evaluate("1 + 1")
        assert val == "mock_eval_res"

        # 3. 模拟接收控制台事件
        server_ws = next(iter(server.connections))
        server_ws.send(json.dumps({
            "method": "Runtime.consoleAPICalled",
            "params": {"type": "log", "args": [{"value": "hello from console"}]}
        }))
        time.sleep(0.3)
        logs = client.console_logs()
        assert "hello from console" in logs

        # 4. 模拟顶层导航安全拦截测试 (Fetch.requestPaused)
        # 4a. 合法白名单主机 Document 请求 -> continueRequest
        MockCdpState.last_fetch_action = None
        server_ws.send(json.dumps({
            "method": "Fetch.requestPaused",
            "params": {
                "requestId": "req-1",
                "resourceType": "Document",
                "request": {"url": "http://127.0.0.1:8080/app"}
            }
        }))
        time.sleep(0.3)
        assert MockCdpState.last_fetch_action is not None
        assert MockCdpState.last_fetch_action[0] == "Fetch.continueRequest"
        assert MockCdpState.last_fetch_action[1]["requestId"] == "req-1"

        # 4b. 非白名单主机 Document 请求 -> failRequest
        MockCdpState.last_fetch_action = None
        server_ws.send(json.dumps({
            "method": "Fetch.requestPaused",
            "params": {
                "requestId": "req-2",
                "resourceType": "Document",
                "request": {"url": "http://192.168.1.100/admin"}
            }
        }))
        time.sleep(0.3)
        assert MockCdpState.last_fetch_action is not None
        assert MockCdpState.last_fetch_action[0] == "Fetch.failRequest"
        assert MockCdpState.last_fetch_action[1]["errorReason"] == "AccessDenied"

        # 4c. 页面子资源（如 script/image）-> 直接放行
        MockCdpState.last_fetch_action = None
        server_ws.send(json.dumps({
            "method": "Fetch.requestPaused",
            "params": {
                "requestId": "req-3",
                "resourceType": "Script",
                "request": {"url": "https://cdn.example.com/lib.js"}
            }
        }))
        time.sleep(0.3)
        assert MockCdpState.last_fetch_action is not None
        assert MockCdpState.last_fetch_action[0] == "Fetch.continueRequest"

        print("CDP 协议同步客户端连接、请求配对、事件收集与导航拦截自检 ✅")
    finally:
        client.close()
        server.shutdown()


def main() -> None:
    test_cdp_client_protocol()
    print("\nAll CDP protocol mock tests passed! ✨")


if __name__ == "__main__":
    main()
