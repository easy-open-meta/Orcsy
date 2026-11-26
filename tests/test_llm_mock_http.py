"""真实 LLMClient 协议测试：对接本地 OpenAI 兼容服务跑完两轮工具循环。"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_llm_server import start_server
from orcsy.config import LLMConfig
from orcsy.llm import LLMClient
from orcsy.tools import FinishSignal, ToolContext


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="orcsy-mock-llm-"))
    (root / "orcsy").mkdir()
    target = root / "orcsy" / "updater.py"
    target.write_text("version = 1\n", encoding="utf-8")

    server, base = start_server()
    # 回环模型服务不能被系统代理接管
    old_no_proxy = os.environ.get("NO_PROXY")
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    try:
        cfg = LLMConfig(api_key="k", base_url=base, model="mock",
                        timeout_seconds=10)
        client = LLMClient(cfg)
        client.retry_backoff = (0, 0, 0)
        client.reset("你是测试助手。")
        try:
            client.run_task_loop(
                "把 orcsy/updater.py 的 version 改为 2。",
                ToolContext(root=root), max_turns=6)
            raise AssertionError("应通过 finish 结束任务")
        except FinishSignal as sig:
            payload = sig.payload
        assert payload["action"] == "completed", payload
        assert target.read_text(encoding="utf-8") == "version = 2\n"

        # 回环 RST 可能触发请求层重试，只要求至少两轮且最后一轮带回工具结果
        assert len(server.RequestHandlerClass.requests) >= 2
        first = server.RequestHandlerClass.requests[0]
        second = server.RequestHandlerClass.requests[-1]
        # 第一轮下发工具 schema；最后一轮带回 tool 结果
        assert any(t.get("function", {}).get("name") == "edit_file"
                   for t in first.get("tools") or [])
        assert any(m.get("role") == "tool"
                   for m in second.get("messages") or [])
    finally:
        if old_no_proxy is None:
            os.environ.pop("NO_PROXY", None)
        else:
            os.environ["NO_PROXY"] = old_no_proxy
        server.shutdown()
        server.server_close()
        shutil.rmtree(root, ignore_errors=True)
    print("test_llm_mock_http: all checks passed")


if __name__ == "__main__":
    main()
