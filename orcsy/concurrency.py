"""按厂商配额自适应的进程级大模型请求并发预算（AIMD）。

厂商不提供「最大并发数」查询接口，能拿到的唯一可靠信号是 429 与
``Retry-After`` 响应头。因此这里用 AIMD（加性增 / 乘性减）在不被告知上限的
前提下逼近它：

- 连续成功 ``_GROW_AFTER`` 次 → 上限 +1，逐步爬回配置值（加性增）；
- 收到 429 → 立即对半砍（不低于 1），并让整个池静默 ``Retry-After`` 秒，
  避免几个子代理同时醒来再撞一次（乘性减）；
- 拿不到许可的调用方**阻塞排队而不是失败**：限流时表现为「更慢」，不会
  像过去那样让部分子代理直接 ⚠️ 掉。

许可池按 (base_url, api_key 哈希, model) 分池：同一密钥下不同模型通常各有
独立配额，不同网关/密钥则完全无关。池是进程级单例——多个项目、多个任务
（``scheduler.max_parallel_agents`` > 1）共享同一预算，否则每处各建一个池
等于没限。

关键约束：许可只在「HTTP 请求在途」期间持有（``LLMClient._post`` 内
acquire/release），工具执行期间不占用。因此主 Agent 与它派出的子代理共用
一个池不会互相死锁——主 Agent 等子代理返回时手上没有许可。
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Any

# 模块级默认值：由 config.py 加载/看板保存后经 configure_limiter 覆盖
_DEFAULT_MAX_PARALLEL = 4
_ADAPTIVE = True

# 连续成功多少次才 +1：太小会在真实上限附近来回抖动
_GROW_AFTER = 6
_MIN_LIMIT = 1
# 429 未给 Retry-After 时的默认静默秒数
_DEFAULT_COOLDOWN = 2.0
# 同一波 429 只砍一次：4 个并发请求常常在同一瞬间全部 429，
# 不能因此把 4 连砍到 1
_SHRINK_DEBOUNCE = 1.0
# 等待许可时的轮询粒度（同时决定停止信号的响应延迟）
_POLL_SECONDS = 0.2


class ConcurrencyBudget:
    """单个配额池的自适应许可控制器（线程安全）。"""

    def __init__(self, key: str, max_limit: int, adaptive: bool) -> None:
        self.key = key
        self._cond = threading.Condition()
        self._max_limit = max(_MIN_LIMIT, int(max_limit))
        self._adaptive = bool(adaptive)
        # 自适应时从「配置值」起步、撞墙才退：健康厂商的第一轮不该被强行
        # 降速；受限套餐付一次 429 的代价就能立刻收敛到真实上限
        self._limit = self._max_limit
        self._in_flight = 0
        self._ok_streak = 0
        self._cooldown_until = 0.0
        self._last_shrink_at = 0.0
        self._shrink_count = 0

    # ---- 只读状态（日志/测试/看板诊断用） ----

    @property
    def limit(self) -> int:
        with self._cond:
            return self._limit

    @property
    def max_limit(self) -> int:
        with self._cond:
            return self._max_limit

    @property
    def in_flight(self) -> int:
        with self._cond:
            return self._in_flight

    @property
    def shrink_count(self) -> int:
        with self._cond:
            return self._shrink_count

    def snapshot(self) -> dict[str, Any]:
        with self._cond:
            return {
                "key": self.key,
                "limit": self._limit,
                "max_limit": self._max_limit,
                "in_flight": self._in_flight,
                "adaptive": self._adaptive,
                "shrink_count": self._shrink_count,
            }

    # ---- 配置同步（看板改设置后即时生效） ----

    def set_max_limit(self, max_limit: int, adaptive: bool | None = None) -> None:
        with self._cond:
            new_max = max(_MIN_LIMIT, int(max_limit))
            if adaptive is not None:
                self._adaptive = bool(adaptive)
            was_at_max = self._limit >= self._max_limit
            self._max_limit = new_max
            if not self._adaptive:
                # 自适应关闭 = 用户指定固定并发
                self._limit = new_max
                self._ok_streak = 0
            elif self._limit > new_max or was_at_max:
                self._limit = new_max
            self._cond.notify_all()

    # ---- 许可控制 ----

    def acquire(self, stop_event: object | None = None) -> bool:
        """占用一个许可；返回 False 表示收到了强制停止信号。

        ``stop_event`` 只要求有 ``is_set()`` 方法（不 import llm，避免环）。
        """
        while True:
            with self._cond:
                while True:
                    if stop_event is not None and stop_event.is_set():
                        return False
                    now = time.monotonic()
                    if now < self._cooldown_until:
                        self._cond.wait(min(self._cooldown_until - now,
                                            _POLL_SECONDS))
                        continue
                    if self._in_flight < self._limit:
                        self._in_flight += 1
                        return True
                    # 池满：排队等释放（可被停止信号打断）
                    self._cond.wait(_POLL_SECONDS)

    def release(self) -> None:
        with self._cond:
            if self._in_flight > 0:
                self._in_flight -= 1
            self._cond.notify_all()

    def on_success(self) -> None:
        """请求成功：累计成功连击，够了就试探着 +1。"""
        with self._cond:
            if not self._adaptive or self._limit >= self._max_limit:
                self._ok_streak = 0
                return
            self._ok_streak += 1
            if self._ok_streak >= _GROW_AFTER:
                self._limit += 1
                self._ok_streak = 0
                self._cond.notify_all()

    def on_rate_limited(self, retry_after: float | None = None) -> None:
        """收到 429：乘性减 + 全池静默。"""
        with self._cond:
            now = time.monotonic()
            cooldown = _DEFAULT_COOLDOWN if retry_after is None else retry_after
            cooldown = max(0.5, min(float(cooldown), 120.0))
            if self._adaptive and now - self._last_shrink_at >= _SHRINK_DEBOUNCE:
                self._last_shrink_at = now
                self._ok_streak = 0
                self._shrink_count += 1
                if self._limit > _MIN_LIMIT:
                    self._limit = max(_MIN_LIMIT, self._limit // 2)
            self._cooldown_until = max(self._cooldown_until, now + cooldown)
            self._cond.notify_all()

    def cooldown_remaining(self) -> float:
        """当前静默期剩余秒数（重试退避据此对齐，避免醒来又撞墙）。"""
        with self._cond:
            return max(0.0, self._cooldown_until - time.monotonic())


# ---- 进程级池注册表 ----

_POOLS: dict[str, ConcurrencyBudget] = {}
_POOLS_LOCK = threading.Lock()


def _pool_key(base_url: str, api_key: str, model: str) -> str:
    # 密钥只以哈希入键：内存字典/日志里都不该出现明文
    digest = hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:16]
    return f"{(base_url or '').rstrip('/')}|{digest}|{model or ''}"


def get_budget(base_url: str, api_key: str, model: str) -> ConcurrencyBudget:
    """取（或惰性创建）该配额的共享许可池。"""
    key = _pool_key(base_url, api_key, model)
    with _POOLS_LOCK:
        pool = _POOLS.get(key)
        if pool is None:
            pool = ConcurrencyBudget(key, _DEFAULT_MAX_PARALLEL, _ADAPTIVE)
            _POOLS[key] = pool
        return pool


def configure_limiter(max_parallel: int, adaptive: bool = True) -> None:
    """应用配置：后续新建池用新默认值，已有池同步调整（不重置自适应状态）。"""
    global _DEFAULT_MAX_PARALLEL, _ADAPTIVE
    _DEFAULT_MAX_PARALLEL = max(_MIN_LIMIT, int(max_parallel))
    _ADAPTIVE = bool(adaptive)
    with _POOLS_LOCK:
        pools = list(_POOLS.values())
    for pool in pools:
        pool.set_max_limit(_DEFAULT_MAX_PARALLEL, _ADAPTIVE)


def snapshot_pools() -> list[dict[str, Any]]:
    """所有池的当前状态（诊断/测试用）。"""
    with _POOLS_LOCK:
        pools = list(_POOLS.values())
    return [p.snapshot() for p in pools]


def reset_pools() -> None:
    """清空进程级池（测试隔离用）。注意：不清会带上一次测试的限流状态。"""
    global _DEFAULT_MAX_PARALLEL, _ADAPTIVE
    _DEFAULT_MAX_PARALLEL = 4
    _ADAPTIVE = True
    with _POOLS_LOCK:
        _POOLS.clear()


__all__ = [
    "ConcurrencyBudget",
    "get_budget",
    "configure_limiter",
    "snapshot_pools",
    "reset_pools",
]

