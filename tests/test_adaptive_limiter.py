"""厂商配额自适应并发预算自检（orcsy/concurrency.py + llm.py 接入）。

厂商不公开「最大并发数」，唯一可靠信号是 429 与 Retry-After。这里验证 AIMD
的四条红线：
①4→2→1 乘性减、同一波 429 只砍一次（4 个并发同时 429 不能连砍）；
②成功后加性增爬回配置上限，关闭 adaptive 时固定不变；
③许可池按 (base_url, api_key 哈希, model) 分池且进程内共享，密钥不出现在键里；
④拿不到许可的请求与限流静默期都是「排队等待」而不是失败，且可被强制停止打断。

运行：uv run python tests/test_adaptive_limiter.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path
if hasattr(sys.stdout, "reconfigure"):  # GBK 控制台打不出 ✅ 等字符
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from orcsy import concurrency
from orcsy.concurrency import (ConcurrencyBudget, configure_limiter,
                               get_budget, reset_pools)
from orcsy.config import LLMConfig, load_config
from orcsy.llm import LLMClient, _parse_server_delay


class _Resp:
    """最小响应桩：headers 可选（厂商限流头的载体）。"""

    def __init__(self, status: int, headers: dict | None = None) -> None:
        self.status_code = status
        self.text = f"body-{status}"
        self.headers = headers or {}

    def json(self) -> dict:
        return {"ok": 1}


def check_shrink_then_grow() -> None:
    pool = ConcurrencyBudget("t-shrink", 4, adaptive=True)
    assert pool.limit == 4 and pool.max_limit == 4
    pool.on_rate_limited(0.1)
    assert pool.limit == 2, pool.limit
    # 同一波限流（1s 内）只砍一次：4 个并发同时 429 不该直接砍到 1
    pool.on_rate_limited(0.1)
    pool.on_rate_limited(0.1)
    assert pool.limit == 2, pool.limit
    time.sleep(concurrency._SHRINK_DEBOUNCE + 0.05)
    pool.on_rate_limited(0.1)
    assert pool.limit == 1, pool.limit
    # 下限 1：再砍也不会到 0
    time.sleep(concurrency._SHRINK_DEBOUNCE + 0.05)
    pool.on_rate_limited(0.1)
    assert pool.limit == 1, pool.limit
    assert pool.shrink_count == 3, pool.shrink_count
    # 加性增：每 6 次成功 +1，封顶 max_limit
    for _ in range(6):
        pool.on_success()
    assert pool.limit == 2, pool.limit
    for _ in range(6 * 5):
        pool.on_success()
    assert pool.limit == 4, pool.limit
    print("ok: 429 乘性减 4→2→1（同波去抖、下限 1），成功后加性增爬回 4")


def check_non_adaptive_fixed() -> None:
    pool = ConcurrencyBudget("t-fixed", 3, adaptive=False)
    pool.on_rate_limited(0.1)
    assert pool.limit == 3, pool.limit
    for _ in range(30):
        pool.on_success()
    assert pool.limit == 3, pool.limit
    # 配置调小时同步钳制（看板改设置即时生效）
    pool.set_max_limit(2)
    assert pool.limit == 2 and pool.max_limit == 2
    print("ok: adaptive=false 时并发固定 3，改配置即时钳到 2")


def check_pool_keying_and_sharing() -> None:
    reset_pools()
    secret = "sk-SECRET-DO-NOT-LEAK"
    a = get_budget("https://api.example.com/v1", secret, "model-a")
    b = get_budget("https://api.example.com/v1/", secret, "model-a")
    c = get_budget("https://api.example.com/v1", secret, "model-b")
    d = get_budget("https://other.example.com/v1", secret, "model-a")
    assert a is b, "同一 base_url（仅尾斜杠不同）必须是同一个池"
    assert a is not c and a is not d, "不同 model / 网关应各自分池"
    assert secret not in a.key, "密钥不得以明文出现在池键里"
    print("ok: 按 (base_url, key 哈希, model) 分池，同键共享、密钥不明文")


def check_acquire_for_stop_and_limits() -> None:
    pool = ConcurrencyBudget("t-acquire", 1, adaptive=False)
    assert pool.acquire() is True
    assert pool.in_flight == 1
    stop = threading.Event()
    out: dict = {}
    t0 = time.monotonic()

    def waiter() -> None:
        out["ok"] = pool.acquire(stop)
        out["dt"] = time.monotonic() - t0

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.4)
    assert "ok" not in out, "池满时第二次 acquire 必须排队等待"
    stop.set()
    t.join(3)
    assert out.get("ok") is False, out
    assert out["dt"] < 1.5, f"停止信号应秒级打断排队，实际 {out['dt']:.2f}s"
    pool.release()
    assert pool.in_flight == 0
    assert pool.acquire() is True
    pool.release()
    print("ok: 池满时阻塞排队，停止信号秒级打断（不占许可）")


def check_cooldown_gate() -> None:
    pool = ConcurrencyBudget("t-cooldown", 4, adaptive=False)
    pool.on_rate_limited(0.6)
    t0 = time.monotonic()
    assert pool.acquire() is True
    waited = time.monotonic() - t0
    pool.release()
    assert waited >= 0.5, f"限流静默期未生效（等待 {waited:.2f}s）"
    assert waited < 2.0, f"静默期过长（{waited:.2f}s）"
    print(f"ok: 429 后全池静默 {waited:.2f}s（Retry-After 0.6s）再放行")


def check_parse_server_delay() -> None:
    def d(headers: dict | None):
        return _parse_server_delay(_Resp(429, headers))

    assert d({"retry-after": "3"}) == 3.0
    assert d({"retry-after": "0.8"}) == 0.8
    # Anthropic/OpenAI 兼容网关的「距重置还有多久」时长串
    assert d({"x-ratelimit-reset-requests": "6m0s"}) == 120.0  # 6*60 被 120 封顶
    assert abs(d({"anthropic-ratelimit-requests-reset": "1.5s"}) - 1.5) < 1e-6
    assert abs(d({"x-ratelimit-reset-tokens": "250ms"}) - 0.5) < 1e-6  # 下限 0.5
    assert d({}) is None and d(None) is None
    # 秒数极小值归一为 0.5，避免 0 等待空转
    assert d({"retry-after": "0"}) == 0.5
    print("ok: Retry-After / *-reset-* 时长串解析（含上下限封顶）")


def check_post_uses_retry_after() -> None:
    """429 带 Retry-After：退避按厂商窗口走，而不是本地 2/4/8。"""
    reset_pools()
    calls = {"n": 0}

    def flaky(url, content=None, headers=None, timeout=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(429, {"retry-after": "0.6"})
        return _Resp(200)

    orig = httpx.post
    httpx.post = flaky
    try:
        c = LLMClient(LLMConfig(api_key="k-ra", base_url="http://ra.local",
                                model="m-ra", timeout_seconds=30))
        c.retry_backoff = (0, 0, 0)  # 本地退避归零，单独验证厂商窗口
        t0 = time.monotonic()
        assert c._post("http://ra.local/x", {}, {}) == {"ok": 1}
        elapsed = time.monotonic() - t0
        assert calls["n"] == 2, calls
        assert 0.5 <= elapsed < 2.0, f"应按 Retry-After 0.6s 等待，实际 {elapsed:.2f}s"
    finally:
        httpx.post = orig
    print("ok: _post 429 时按厂商 Retry-After 等待并发起重试")


def check_requests_are_actually_capped() -> None:
    """4 个子代理式并发请求 + 上限 1 = 串行跑完、全部成功（不失败不丢）。"""
    reset_pools()
    configure_limiter(1, adaptive=False)
    stats = {"cur": 0, "peak": 0}
    lock = threading.Lock()

    def slow(url, content=None, headers=None, timeout=None, **kw):
        with lock:
            stats["cur"] += 1
            stats["peak"] = max(stats["peak"], stats["cur"])
        time.sleep(0.05)
        with lock:
            stats["cur"] -= 1
        return _Resp(200)

    cfg = LLMConfig(api_key="k-cap", base_url="http://cap.local",
                    model="m-cap", timeout_seconds=30)
    orig = httpx.post
    httpx.post = slow
    try:
        results: list[dict] = []
        rlock = threading.Lock()

        def worker() -> None:
            out = LLMClient(cfg)._post("http://cap.local/x", {}, {})
            with rlock:
                results.append(out)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert len(results) == 4 and all(r == {"ok": 1} for r in results), results
        assert stats["peak"] == 1, f"上限 1 时应串行，实际峰值 {stats['peak']}"
    finally:
        httpx.post = orig
        reset_pools()
        configure_limiter(4, True)
    print("ok: 4 个并发请求被压到 1（串行）但全部成功——限流退化为变慢")


def check_config_roundtrip() -> None:
    reset_pools()
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "config.yaml"
        p.write_text("""
llm:
  api_key: sk-task
  model: gpt-4o
subagent:
  max_parallel: 2
  adaptive: false
projects:
  - name: a
    path: .
""", encoding="utf-8")
        cfg = load_config(p, require_api_key=False)
        assert cfg.subagent.max_parallel == 2
        assert cfg.subagent.adaptive is False
        # load_config 应已把配置同步给进程级配额池
        pool = get_budget(cfg.llm.base_url, cfg.llm.api_key, cfg.llm.model)
        assert pool.max_limit == 2 and pool.limit == 2, pool.snapshot()

    # 缺省节：4 / 自适应开（与 delegate_explore 的 4 个熔断值一致）
    reset_pools()
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "config.yaml"
        p.write_text("""
llm:
  api_key: sk-task
  model: gpt-4o
projects:
  - name: a
    path: .
""", encoding="utf-8")
        cfg = load_config(p, require_api_key=False)
        assert cfg.subagent.max_parallel == 4
        assert cfg.subagent.adaptive is True

    # 越界与类型错误必须拒启动（不静默回退）
    for bad, needle in ((0, "1~16"), (17, "1~16"), ("x", "必须是整数")):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.yaml"
            p.write_text(f"""
llm:
  api_key: sk-task
  model: gpt-4o
subagent:
  max_parallel: {bad}
projects:
  - name: a
    path: .
""", encoding="utf-8")
            try:
                load_config(p, require_api_key=False)
                raise AssertionError(f"subagent.max_parallel={bad} 应被拒绝")
            except ValueError as e:
                assert needle in str(e), str(e)
    reset_pools()
    configure_limiter(4, True)
    print("ok: config 往返（2/false）、缺省 4/true、越界拒启动")


def check_web_save_roundtrip() -> None:
    """看板保存 → config.yaml 落盘 → 重新加载：字段不丢、运行时池即时同步。"""
    from orcsy.config import AppConfig, ProjectConfig
    from orcsy.scheduler import Scheduler
    from orcsy.web import AppState

    reset_pools()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cfg = AppConfig(
            llm=LLMConfig(api_key="sk-web", base_url="http://web.local",
                          model="m-web"),
            projects=[ProjectConfig(name="p", path=root)],
            source_path=root / "config.yaml",
        )
        state = AppState(cfg, Scheduler(cfg), log_path=str(root / "orcsy.log"))
        out = state.save_settings(
            {"subagent": {"max_parallel": 2, "adaptive": False}})
        assert out.get("ok") is True, out
        assert state.config.subagent.max_parallel == 2
        assert state.config.subagent.adaptive is False
        # 运行时池立即跟随（不用等重启/下一轮调度）
        pool = get_budget("http://web.local", "sk-web", "m-web")
        assert pool.limit == 2 and pool.max_limit == 2, pool.snapshot()
        # full_state 回传给看板：配置值 + 运行时快照都在（卡片显示「当前实际并发」）
        fs = state.full_state()
        assert fs["subagent_cfg"]["max_parallel"] == 2
        assert fs["subagent_cfg"]["adaptive"] is False
        assert isinstance(fs["subagent_cfg"]["pools"], list)
        assert "sk-web" not in json.dumps(fs, ensure_ascii=False), \
            "full_state 不得泄露明文密钥（池键只存哈希）"

        text = (root / "config.yaml").read_text(encoding="utf-8")
        assert "subagent:" in text and "max_parallel: 2" in text, text
        assert "pools" not in text, "运行时池快照不得写进 config.yaml"
        reloaded = load_config(root / "config.yaml", require_api_key=False)
        assert reloaded.subagent.max_parallel == 2
        assert reloaded.subagent.adaptive is False

        # 越界保存必须 400（ValueError 由 web 层转成错误响应），且不落盘
        try:
            state.save_settings({"subagent": {"max_parallel": 99}})
            raise AssertionError("max_parallel=99 应被拒绝")
        except ValueError as e:
            assert "1~16" in str(e), str(e)
        assert state.config.subagent.max_parallel == 2
    reset_pools()
    configure_limiter(4, True)
    print("ok: 看板保存落盘（2/false）可重新加载，运行时池即时同步，越界拒绝")


def main() -> int:
    check_shrink_then_grow()
    check_non_adaptive_fixed()
    check_pool_keying_and_sharing()
    check_acquire_for_stop_and_limits()
    check_cooldown_gate()
    check_parse_server_delay()
    check_post_uses_retry_after()
    check_requests_are_actually_capped()
    check_config_roundtrip()
    check_web_save_roundtrip()
    print("\n厂商配额自适应并发预算自检全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
