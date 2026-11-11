"""测试 max_turns=-1 真正不限轮数（不得被任何隐式硬上限截断）。

回归背景：为防 1788 轮死循环，曾在 unlimited 模式加过 120 轮硬上限
sanity_ceiling，导致用户显式配置 -1 的 QA 全流程 E2E 在第 121 轮被误杀。
修复后 -1 只有 finish / 停止信号 / 具体防死循环护栏能结束循环，无整体轮数墙。
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import LLMConfig
from orcsy.llm import LLMClient, TaskStopped, finish_response
from orcsy.tools import FinishSignal, ToolContext


class _ManyTurnsLLM(LLMClient):
    """替身：连续 N 轮调用 run_command（写操作，不触发只读熔断），最后 finish。"""

    def __init__(self, cfg: LLMConfig) -> None:
        super().__init__(cfg)
        self._scripted_turn = 0

    def _step_openai(self, use_tools: bool):  # type: ignore[override]
        self._scripted_turn += 1
        self.usage["turns"] = self._scripted_turn
        if self._scripted_turn < 130:
            return (
                {"role": "assistant", "content": None,
                 "tool_calls": [{"id": f"c{self._scripted_turn}", "type": "function",
                                 "function": {"name": "run_command",
                                              "arguments": '{"command": ["echo", "x"]}'}}]},
                [(f"c{self._scripted_turn}", "run_command", '{"command": ["echo", "x"]}')],
                "",
            )
        return (
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "cf", "type": "function",
                             "function": {"name": "finish",
                                          "arguments": json.dumps(
                                              {"action": "completed", "summary": "完成"})}}]},
            [("cf", "finish", json.dumps({"action": "completed", "summary": "完成"}))],
            "",
        )


def test_unlimited_not_capped_at_120() -> None:
    """max_turns=-1：跑到第 130 轮仍能继续（不被 120 硬墙截断），最终正常 finish。"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        ctx = ToolContext(root=Path(tmp), enabled_tools=frozenset({"run_command", "finish"}))
        client = _ManyTurnsLLM(LLMConfig(api_key="k", base_url="http://localhost:9",
                                         model="m"))
        client.reset("sys")
        # -1 = 不限轮数：130 轮后应仍能 finish（而非 120 轮被强制收尾 blocked）
        try:
            resp = client.run_task_loop("do it", ctx, max_turns=-1)
        except FinishSignal as sig:  # 与 executor 一致：捕获后转结构化结论
            resp = finish_response(sig.payload)
        assert resp.action == "completed", (resp.action, resp.summary)
        assert client.usage["turns"] >= 130, client.usage["turns"]
    print("ok: max_turns=-1 跑到 130 轮不被截断，正常 finish")


def test_finite_max_turns_still_caps() -> None:
    """有限轮数（如 10）仍然严格生效（保持既有语义）。"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        ctx = ToolContext(root=Path(tmp), enabled_tools=frozenset({"run_command", "finish"}))
        client = _ManyTurnsLLM(LLMConfig(api_key="k", base_url="http://localhost:9",
                                         model="m"))
        client.reset("sys")
        resp = client.run_task_loop("do it", ctx, max_turns=10)
        # 10 轮后被强制收尾（fallback/blocked），不会跑到 130
        assert resp.action != "completed", resp.action
        assert client.usage["turns"] <= 11, client.usage["turns"]
    print("ok: 有限轮数 max_turns=10 仍严格生效")


def test_stop_event_breaks_unlimited() -> None:
    """unlimited + 强停信号：能被打断（不会永久挂死）。"""
    import tempfile
    import threading
    with tempfile.TemporaryDirectory() as tmp:
        stop = threading.Event()
        stop.set()
        ctx = ToolContext(root=Path(tmp), stop_event=stop,
                          enabled_tools=frozenset({"run_command", "finish"}))
        client = _ManyTurnsLLM(LLMConfig(api_key="k", base_url="http://localhost:9",
                                         model="m"))
        client.reset("sys")
        try:
            client.run_task_loop("do it", ctx, max_turns=-1)
            raise AssertionError("强停信号应抛出 TaskStopped")
        except TaskStopped:
            pass
    print("ok: unlimited 模式强停信号仍可秒级打断")


def test_read_loop_still_guarded_under_unlimited() -> None:
    """-1 不限轮数下，「永远重读同一文件」仍被护栏熔断（不会真的无限跑）。

    回归背景：护栏 1（同文件 >5 次拦截）的 continue 曾跳过护栏 2 的连读计数，
    导致被拦截的重读不累计，-1 模式下模型永远达不到 20 轮熔断阈值而无限空转。
    修复后拦截也计入连读轮次，第 20 轮强制熔断。
    """
    import tempfile

    class _ReadLoopLLM(LLMClient):
        def __init__(self, cfg: LLMConfig) -> None:
            super().__init__(cfg)
            self.n = 0

        def _step_openai(self, use_tools: bool):  # type: ignore[override]
            self.n += 1
            self.usage["turns"] = self.n
            args = json.dumps({"path": "target.py"})
            return (
                {"role": "assistant", "content": None,
                 "tool_calls": [{"id": f"c{self.n}", "type": "function",
                                 "function": {"name": "read_file", "arguments": args}}]},
                [(f"c{self.n}", "read_file", args)], "",
            )

    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "target.py").write_text("x = 1\n", encoding="utf-8")
        ctx = ToolContext(root=Path(tmp), produces_code=True,
                          enabled_tools=frozenset({"read_file", "finish"}))
        client = _ReadLoopLLM(LLMConfig(api_key="k", base_url="http://localhost:9",
                                        model="m"))
        client.reset("sys")
        try:
            resp = client.run_task_loop("loop", ctx, max_turns=-1)
        except FinishSignal as sig:
            resp = finish_response(sig.payload)
        assert client.usage["turns"] < 40, \
            f"同文件重读应被护栏提前熔断，实际 {client.usage['turns']} 轮"
        assert resp.action == "blocked"
        # 熔断文案不得出现「最大轮数 -1」误导
        assert "-1" not in (resp.notes or ""), resp.notes
        assert "只读死循环" in (resp.notes or ""), resp.notes
    print("ok: -1 模式下同文件重读死循环被护栏提前熔断（文案无 -1 误导）")


def main() -> int:
    test_unlimited_not_capped_at_120()
    test_finite_max_turns_still_caps()
    test_stop_event_breaks_unlimited()
    test_read_loop_still_guarded_under_unlimited()
    print("\nmax_turns=-1 真正不限轮数全部测试通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
