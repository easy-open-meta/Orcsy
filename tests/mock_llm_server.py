"""本地 OpenAI 兼容模型服务，供真实 LLMClient 协议测试。

第一轮返回 edit_file 工具调用，收到 tool 结果后返回 finish；只实现
`POST /v1/chat/completions`，用于验证 Orcsy 侧请求构造、tool_calls 解析与
多轮工具循环，不代表真实模型能力。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _response(model: str, tool_calls: list[dict]) -> dict:
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": None,
                        "tool_calls": tool_calls},
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                  "total_tokens": 15},
    }


def _tool_call(call_id: str, name: str, args: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name,
                     "arguments": json.dumps(args, ensure_ascii=False)},
    }


class Handler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, fmt, *args) -> None:
        return

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.endswith("/chat/completions"):
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._send(400, {"error": "bad json"})
        Handler.requests.append(payload)
        model = str(payload.get("model") or "mock")
        has_tool_result = any(
            isinstance(m, dict) and m.get("role") == "tool"
            for m in payload.get("messages") or [])
        if has_tool_result:
            calls = [_tool_call("call-finish", "finish", {
                "action": "completed",
                "summary": "将 version 改为 2",
                "files_changed": ["orcsy/updater.py"],
                "commit_message": "chore: bump version",
                "notes": "",
            })]
        else:
            calls = [_tool_call("call-edit", "edit_file", {
                "path": "orcsy/updater.py",
                "old_string": "version = 1\n",
                "new_string": "version = 2\n",
            })]
        return self._send(200, _response(model, calls))


def start_server() -> tuple[ThreadingHTTPServer, str]:
    Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/v1"
