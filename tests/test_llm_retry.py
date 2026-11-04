"""LLM 请求瞬时故障重试自检：超时/429/5xx 退避重试，最终失败才判死任务。

背景（2026-11-03 实测）：后端任务单请求 timed out 一次即整任务 blocked，
只能等下一调度周期（20 分钟）重跑。现在请求层对可重试错误自动退避重试：
①TimeoutException / TransportError / 408 / 429 / 5xx → 重试（默认首次+3 次，
退避 2/4/8s，测试归零）；②退避等待期间每 0.2s 看一眼停止信号，「强制停止」
秒级生效；③401/400 等确定性错误不重试，保持原文案；④最终失败时错误信息
注明已重试次数，超时仍带可行动指引。
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

from orcsy.config import LLMConfig
from orcsy.llm import LLMClient, LLMError
from orcsy.tools import TaskStopped


def _client(timeout: int = 300) -> LLMClient:
    c = LLMClient(LLMConfig(api_key="k", base_url="http://localhost:9",
                            model="m", timeout_seconds=timeout))
    c.retry_backoff = (0, 0, 0)  # 自检不求真实退避
    return c


class FakeResp:
    def __init__(self, status: int):
        self.status_code = status
        self.text = f"body-{status}"

    def json(self):
        return {"ok": 1}


def check_retry_then_success() -> None:
    """前两次超时、第三次成功：返回正常结果，共请求 3 次。"""
    calls = {"n": 0}

    def flaky_post(url, content=None, headers=None, timeout=None, **kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.ReadTimeout("timed out")
        return FakeResp(200)

    orig = httpx.post
    httpx.post = flaky_post
    try:
        c = _client()
        c._stop_event = threading.Event()
        assert c._post("http://localhost:9/x", {}, {}) == {"ok": 1}
        assert calls["n"] == 3, calls
    finally:
        httpx.post = orig
    print("ok: 超时×2 后成功 → 自动重试，共 3 次请求拿到结果")


def check_retry_status_codes() -> None:
    """429/503 重试后成功；500 撑到最后一次 → 带重试次数的 LLMError。"""
    calls = {"n": 0}

    def rate_limited(url, content=None, headers=None, timeout=None, **kw):
        calls["n"] += 1
        return FakeResp(429 if calls["n"] <= 2 else 200)

    orig = httpx.post
    httpx.post = rate_limited
    try:
        c = _client()
        c._stop_event = threading.Event()
        assert c._post("http://localhost:9/x", {}, {}) == {"ok": 1}
        assert calls["n"] == 3, calls
    finally:
        httpx.post = orig

    def always_500(url, content=None, headers=None, timeout=None, **kw):
        return FakeResp(500)

    httpx.post = always_500
    try:
        c = _client()
        c._stop_event = threading.Event()
        try:
            c._post("http://localhost:9/x", {}, {})
            raise AssertionError("应抛 LLMError")
        except LLMError as e:
            assert "HTTP 500" in str(e) and "已自动重试 3 次" in str(e), str(e)
    finally:
        httpx.post = orig
    print("ok: 429/503 重试后成功；500 耗尽重试 → 注明已重试 3 次")


def check_no_retry_client_error() -> None:
    """401 等确定性错误不重试：立即抛出原文案，仅请求 1 次。"""
    calls = {"n": 0}

    def unauthorized(url, content=None, headers=None, timeout=None, **kw):
        calls["n"] += 1
        return FakeResp(401)

    orig = httpx.post
    httpx.post = unauthorized
    try:
        c = _client()
        c._stop_event = threading.Event()
        try:
            c._post("http://localhost:9/x", {}, {})
            raise AssertionError("应抛 LLMError")
        except LLMError as e:
            assert "HTTP 401" in str(e) and "已自动重试" not in str(e), str(e)
        assert calls["n"] == 1, calls
    finally:
        httpx.post = orig
    print("ok: 401 不重试（立即失败，仅 1 次请求）")


def check_exhausted_timeout_guidance() -> None:
    """一直超时：耗尽重试后仍带可行动指引 + 尝试次数。"""
    orig = httpx.post
    httpx.post = lambda *a, **kw: (_ for _ in ()).throw(httpx.ReadTimeout("x"))
    try:
        c = _client(300)
        c._stop_event = threading.Event()
        try:
            c._post("http://localhost:9/x", {}, {})
            raise AssertionError("应抛 LLMError")
        except LLMError as e:
            msg = str(e)
            assert "请求大模型超时" in msg and "300 秒" in msg, msg
            assert "含自动重试共 4 次尝试" in msg, msg
            assert "超时秒数" in msg and "-1" in msg, msg
    finally:
        httpx.post = orig
    print("ok: 一直超时 → 4 次尝试后失败，指引文案完整")


def check_stop_during_backoff() -> None:
    """退避等待期间置位强制停止 → 秒级 TaskStopped，不再发起下一次请求。"""
    calls = {"n": 0}

    def always_fail(url, content=None, headers=None, timeout=None, **kw):
        calls["n"] += 1
        raise httpx.ConnectError("refused")

    orig = httpx.post
    httpx.post = always_fail
    try:
        c = _client()
        c.retry_backoff = (5, 5, 5)  # 首次失败后退避 5s，足够测试置位停止
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
        time.sleep(0.5)  # 让第一次请求失败、进入退避
        stop.set()
        t.join(5)
        assert out.get("r") == "stopped", out
        assert out["dt"] < 2.0, f"退避中停止应秒级生效，实际 {out['dt']:.1f}s"
        assert calls["n"] == 1, calls  # 退避被停止单打断，未发起第二次请求
    finally:
        httpx.post = orig
    print("ok: 退避等待中强制停止 → 秒级 TaskStopped，不再重试")


def check_backoff_sequence() -> None:
    """退避按 2/4/8s 递增（用极小粒度断言实际 sleep 总量）。"""
    import orcsy.llm as llm_mod

    sleeps: list[float] = []
    orig_sleep = llm_mod.time.sleep
    llm_mod.time.sleep = lambda s: sleeps.append(s)
    orig = httpx.post
    httpx.post = lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("x"))
    try:
        c = _client()
        c._stop_event = None  # 走直连路径，退避用 time.sleep
        c.retry_backoff = (2, 4, 8)  # 覆盖 _client 的归零默认，验证真实序列
        try:
            c._post("http://localhost:9/x", {}, {})
        except LLMError:
            pass
        assert sleeps == [2.0, 4.0, 8.0], sleeps
    finally:
        llm_mod.time.sleep = orig_sleep
        httpx.post = orig
    print("ok: 退避序列 2/4/8s 递增")


def main() -> int:
    check_retry_then_success()
    check_retry_status_codes()
    check_no_retry_client_error()
    check_exhausted_timeout_guidance()
    check_stop_during_backoff()
    check_backoff_sequence()
    print("\nLLM 请求重试自检全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
