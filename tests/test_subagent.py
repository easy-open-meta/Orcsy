"""测试任务内动态只读子代理（Task Subagent Fan-Out）并发探索与 5 重安全熔断。"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import LLMConfig
from orcsy.llm import LLMClient
from orcsy.subagent import (
    DEFAULT_SUBAGENT_MAX_TURNS,
    MAX_SUBAGENTS_PER_CALL,
    SUBAGENT_ALLOWED_TOOLS,
    SubagentTask,
    _run_single_subagent,
    run_subagents_fan_out,
)
from orcsy.tools import ToolContext, dispatch_tool, tool_delegate_explore


def test_subagent_security_sandbox() -> None:
    """F1 深度熔断与只读沙盒：子代理禁止包含写工具，禁止再次派生子代理。"""
    assert "delegate_explore" not in SUBAGENT_ALLOWED_TOOLS
    assert "write_file" not in SUBAGENT_ALLOWED_TOOLS
    assert "run_command" not in SUBAGENT_ALLOWED_TOOLS
    assert "create_tasks" not in SUBAGENT_ALLOWED_TOOLS
    assert "finish" in SUBAGENT_ALLOWED_TOOLS
    assert "read_file" in SUBAGENT_ALLOWED_TOOLS

    with tempfile.TemporaryDirectory() as tmp:
        # 子代理上下文尝试再次调用 delegate_explore 必须被拒绝
        ctx = ToolContext(root=Path(tmp), is_subagent=True)
        r = tool_delegate_explore(ctx, {"subagents": [{"name": "nested", "focus": "xxx"}]})
        assert "严禁递归派生" in r
    print("ok: F1 递归深度熔断与只读工具沙盒校验")


def test_subagent_call_limits() -> None:
    """F2 扇出上限与频次熔断：单次最多 4 个，单任务最多 2 次。"""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = ToolContext(root=Path(tmp), llm_config=LLMConfig(api_key="k", base_url="http://localhost:9", model="m"))
        # 超过 4 个拒绝
        r_over = tool_delegate_explore(ctx, {"subagents": [
            {"name": f"sub_{i}", "focus": "test"} for i in range(5)
        ]})
        assert "最多允许派生 4 个子代理" in r_over

        # 空列表拒绝
        r_empty = tool_delegate_explore(ctx, {"subagents": []})
        assert "必须通过 subagents 数组指定" in r_empty

        # 频次熔断：调用 2 次后第 3 次拒绝
        ctx.subagent_call_count = 2
        r_freq = tool_delegate_explore(ctx, {"subagents": [{"name": "sub", "focus": "f"}]})
        assert "并发探索已达上限" in r_freq
    print("ok: F2 扇出上限（最多4个）与频次熔断（最多2批次）校验")


def test_subagent_fan_out_execution() -> None:
    """验证并发驱动与报告汇总，以及 Token 用量准确累加。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "api.py").write_text("def hello(): return 'world'\n", encoding="utf-8")
        (root / "model.py").write_text("class User: id = 1\n", encoding="utf-8")

        orig_post = LLMClient._post

        def mock_post(self, url: str, payload: dict, headers: dict) -> dict:
            # 模拟子代理一轮探索后直接 finish 输出结论
            model = payload.get("model", "")
            msgs = payload.get("messages", [])
            last = msgs[-1]["content"] if msgs else ""
            if "后端数据模型" in last:
                return {
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "finish",
                                    "arguments": json.dumps({
                                        "action": "completed",
                                        "summary": "发现 User 实体定义在 model.py",
                                        "notes": "核心类：User(id=1)",
                                    }, ensure_ascii=False),
                                },
                            }],
                        },
                    }],
                    "usage": {"prompt_tokens": 150, "completion_tokens": 35},
                }
            else:
                return {
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": "call_2",
                                "type": "function",
                                "function": {
                                    "name": "finish",
                                    "arguments": json.dumps({
                                        "action": "completed",
                                        "summary": "发现接口在 api.py",
                                        "notes": "路由函数：hello()",
                                    }, ensure_ascii=False),
                                },
                            }],
                        },
                    }],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 25},
                }

        LLMClient._post = mock_post
        try:
            llm_cfg = LLMConfig(api_key="k", base_url="http://localhost:9", model="mock")
            ctx = ToolContext(root=root, llm_config=llm_cfg)
            res_str = dispatch_tool(ctx, "delegate_explore", {
                "subagents": [
                    {"name": "调研后端模型", "focus": "了解后端数据模型", "paths": ["model.py"]},
                    {"name": "调研前端路由", "focus": "了解接口路由", "paths": ["api.py"]},
                ]
            })

            assert "动态子代理并发探索报告" in res_str
            assert "调研后端模型" in res_str
            assert "发现 User 实体" in res_str
            assert "调研前端路由" in res_str
            assert "发现接口在 api.py" in res_str

            # 验证 Token 用量累加
            assert ctx.subagent_usage["prompt_tokens"] == 150 + 120
            assert ctx.subagent_usage["completion_tokens"] == 35 + 25
            assert ctx.subagent_usage["turns"] == 2
            assert ctx.subagent_call_count == 1
        finally:
            LLMClient._post = orig_post
    print("ok: 并发子代理驱动、报告 Markdown 聚合与 Token 用量累加")


def test_subagent_stop_event_abort() -> None:
    """F4 强停信号打断：父任务触发 stop_event 时子代理能够立即中止。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        stop = threading.Event()
        stop.set()  # 预先置位模拟用户点击强制停止

        llm_cfg = LLMConfig(api_key="k", base_url="http://localhost:9", model="mock")
        ctx = ToolContext(root=root, stop_event=stop, llm_config=llm_cfg)

        task = SubagentTask(name="快速探索", focus="测试强停")
        res = _run_single_subagent(ctx, task, llm_cfg, max_turns=5)
        assert res.success is False
        assert "强制停止" in res.error or "已中断" in res.summary
    print("ok: F4 共享强停信号秒级打断验证")


def main() -> int:
    test_subagent_security_sandbox()
    test_subagent_call_limits()
    test_subagent_fan_out_execution()
    test_subagent_stop_event_abort()
    print("\n动态子代理并发探索全部测试通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
