"""模型请求超时自检：-1 = 不超时、请求级强制停止感知、超时可行动指引。

背景：真实跑批中思考型模型单轮生成超过 timeout_seconds=300 被掐（用户只能
看到裸的 timed out）。现在：①超时可设 -1 不超时（与轮数上限 -1 同语义）；
②HTTP 请求挪到子线程执行、主线程 0.2s 轮询停止信号——请求挂起期间看板
「强制停止」秒级生效（此前要等满超时）；③超时错误带可行动指引。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from orcsy.config import LLMConfig, load_config
from orcsy.llm import LLMClient, LLMError
from orcsy.tools import TaskStopped


def _client(timeout: int) -> LLMClient:
    return LLMClient(LLMConfig(api_key="k", base_url="http://localhost:9",
                               model="m", timeout_seconds=timeout))


def check_timeout_value() -> None:
    captured: dict = {}

    def fake_post(url, content=None, headers=None, timeout=None, **kw):
        captured["timeout"] = timeout
        raise httpx.ConnectError("stop-here")

    orig = httpx.post
    httpx.post = fake_post
    try:
        for ts, expect in ((300, 300), (900, 900), (-1, None), (0, None)):
            captured.clear()
            try:
                _client(ts)._post("http://localhost:9/x", {}, {})
            except LLMError:
                pass
            assert captured["timeout"] == expect, (ts, captured)
    finally:
        httpx.post = orig
    print("ok: httpx 超时参数（正值原样 / -1 与 0 → None 不超时）")


def check_stop_interrupts() -> None:
    """不超时（-1）+ 请求无限挂起：置位强制停止 → 秒级 TaskStopped。"""
    started = threading.Event()

    def slow_post(url, content=None, headers=None, timeout=None, **kw):
        started.set()
        time.sleep(10)  # 模拟无限挂起的响应
        raise httpx.ConnectError("should-not-reach")

    orig = httpx.post
    httpx.post = slow_post
    try:
        c = _client(-1)
        stop = threading.Event()
        c._stop_event = stop
        out: dict = {}

        def call() -> None:
            t0 = time.monotonic()
            try:
                c._post("http://localhost:9/x", {}, {})
                out["r"] = "returned"
            except TaskStopped:
                out["r"] = "stopped"
            except BaseException as e:  # noqa: BLE001
                out["r"] = f"other:{type(e).__name__}"
            out["dt"] = time.monotonic() - t0

        t = threading.Thread(target=call)
        t.start()
        assert started.wait(3), "假 httpx.post 未被调用"
        time.sleep(0.3)
        stop.set()
        t.join(5)
        assert out.get("r") == "stopped", out
        assert out["dt"] < 2.0, f"停止应秒级生效，实际 {out['dt']:.1f}s"
    finally:
        httpx.post = orig
    print("ok: 请求挂起中强制停止 → 秒级 TaskStopped（-1 不超时场景）")


def check_worker_paths() -> None:
    """停止感知路径的三类结果：正常响应 / 超时→指引文案 / 连接错误→原文案。"""

    class FakeResp:
        status_code = 200
        text = "{}"

        def json(self):
            return {"ok": 1}

    orig = httpx.post
    try:
        # 正常响应穿过子线程回来
        httpx.post = lambda *a, **kw: FakeResp()
        c = _client(-1)
        c._stop_event = threading.Event()  # 未置位
        assert c._post("http://localhost:9/x", {}, {}) == {"ok": 1}

        # worker 内超时 → 主线程转可行动指引
        def timeout_post(url, content=None, headers=None, timeout=None, **kw):
            time.sleep(0.1)
            raise httpx.ReadTimeout("timed out")
        httpx.post = timeout_post
        c = _client(300)
        c._stop_event = threading.Event()
        try:
            c._post("http://localhost:9/x", {}, {})
            raise AssertionError("应抛 LLMError")
        except LLMError as e:
            msg = str(e)
            assert "请求大模型超时" in msg and "300 秒" in msg, msg
            assert "超时秒数" in msg and "-1" in msg and "强制停止" in msg, msg

        # worker 内连接错误 → 原文案（不误挂超时指引）
        def conn_post(url, content=None, headers=None, timeout=None, **kw):
            raise httpx.ConnectError("connection refused")
        httpx.post = conn_post
        c = _client(-1)
        c._stop_event = threading.Event()
        try:
            c._post("http://localhost:9/x", {}, {})
            raise AssertionError("应抛 LLMError")
        except LLMError as e:
            assert "请求大模型失败" in str(e) and "超时秒数" not in str(e), str(e)
    finally:
        httpx.post = orig
    print("ok: 停止感知路径三类结果（正常/超时指引/连接错误原文案）")


def check_run_task_loop_wiring() -> None:
    """run_task_loop 把 ctx.stop_event 传进 HTTP 层（集成级接线验证）。"""
    from orcsy.tools import ToolContext

    started = threading.Event()

    def slow_post(url, content=None, headers=None, timeout=None, **kw):
        started.set()
        time.sleep(10)
        raise httpx.ConnectError("x")

    orig = httpx.post
    httpx.post = slow_post
    tmp = Path(tempfile.mkdtemp(prefix="llmto-loop-"))
    try:
        c = _client(-1)
        c.reset("sys")
        stop = threading.Event()
        ctx = ToolContext(root=tmp, stop_event=stop)
        out: dict = {}

        def run() -> None:
            try:
                c.run_task_loop("任务", ctx)
                out["r"] = "returned"
            except TaskStopped:
                out["r"] = "stopped"
            except BaseException as e:  # noqa: BLE001
                out["r"] = f"{type(e).__name__}:{e}"

        t = threading.Thread(target=run)
        t.start()
        assert started.wait(3), "假 httpx.post 未被调用"
        time.sleep(0.2)
        stop.set()
        t.join(5)
        assert out.get("r") == "stopped", out
    finally:
        httpx.post = orig
        shutil.rmtree(tmp, ignore_errors=True)
    print("ok: run_task_loop 把 stop_event 传进 HTTP 层（请求挂起也能被打断）")


def check_config_and_web() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="llmto-cfg-"))
    try:
        # YAML 解析：-1 保留；0 归一 -1；缺省 300
        for text, expect in (("llm:\n  api_key: k\n  model: m\n  timeout_seconds: -1\n", -1),
                             ("llm:\n  api_key: k\n  model: m\n  timeout_seconds: 0\n", -1),
                             ("llm:\n  api_key: k\n  model: m\n  timeout_seconds: 900\n", 900),
                             ("llm:\n  api_key: k\n  model: m\n", 300)):
            f = tmp / f"c{expect}{len(text)}.yaml"
            f.write_text(text, encoding="utf-8")
            got = load_config(f, require_api_key=False).llm.timeout_seconds
            assert got == expect, (text, got)
        # Web 保存：-1/0 roundtrip 落盘 + full_state 回显
        from orcsy.config import AppConfig
        from orcsy.scheduler import Scheduler
        from orcsy.web import AppState
        cfg_file = tmp / "web.yaml"
        cfg_file.write_text("llm: {}\n", encoding="utf-8")
        cfg = AppConfig(llm=LLMConfig(api_key="fake", base_url="http://localhost:9",
                                      model="fake"), source_path=cfg_file)
        st = AppState(cfg, Scheduler(cfg))
        llm_block = {"provider": "openai", "model": "m2",
                     "base_url": "http://localhost:9", "api_key": "k"}
        st.save_settings({"llm": {**llm_block, "timeout_seconds": -1}})
        assert st.config.llm.timeout_seconds == -1
        assert st.full_state()["llm"]["timeout_seconds"] == -1
        assert load_config(cfg_file, require_api_key=False).llm.timeout_seconds == -1
        st.save_settings({"llm": {**llm_block, "timeout_seconds": 0}})
        assert st.config.llm.timeout_seconds == -1  # 0 显式归一为不超时
        print("ok: 配置解析/Web 保存（-1 保留、0 归一、缺省 300、落盘 roundtrip）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_probe_clamps() -> None:
    """探活/拉模型列表是一次性短请求：-1 不透传「不超时」，按 30s 兜底。"""
    from orcsy.llm import list_models, test_connection

    cap: dict = {}
    orig_get, orig_post = httpx.get, httpx.post

    def fake_get(url, headers=None, timeout=None, **kw):
        cap["get"] = timeout
        raise httpx.ConnectError("stop")

    def fake_post(url, content=None, headers=None, timeout=None, **kw):
        cap["post"] = timeout
        raise httpx.ConnectError("stop")

    httpx.get, httpx.post = fake_get, fake_post
    try:
        for ts in (-1, 600):
            cap.clear()
            try:
                list_models("openai", "http://localhost:9/v1", "k", ts)
            except LLMError:
                pass
            assert cap["get"] == 30, (ts, cap)
        cap.clear()
        try:
            test_connection("openai", "http://localhost:9/v1", "k", "m", -1)
        except LLMError:
            pass
        assert cap["post"] == 30, cap
    finally:
        httpx.get, httpx.post = orig_get, orig_post
    print("ok: 探活/拉模型列表 -1 时按 30s 兜底（不透传不超时）")


def main() -> int:
    check_timeout_value()
    check_stop_interrupts()
    check_worker_paths()
    check_run_task_loop_wiring()
    check_config_and_web()
    check_probe_clamps()
    print("\n模型请求超时自检全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
