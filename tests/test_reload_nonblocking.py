"""测试任务执行中保存设置不再阻塞（reload_config 与 run_cycle 锁竞争修复）。

回归背景：reload_config 原先与 run_cycle 抢同一把 _cycle_lock（阻塞式获取）。
任务一跑数小时时，看板「保存设置」的 HTTP 请求会一直挂在锁上，前端永远
loading。修复后 config/mirror_runtime 原子先行切换 + Agent 列表后台重建，
保存请求毫秒级返回。
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import AppConfig, LLMConfig, ProjectConfig, SchedulerConfig
from orcsy.scheduler import Scheduler


def _make_cfg(tmp: Path, model: str = "m1") -> AppConfig:
    proj_dir = tmp / "proj"
    proj_dir.mkdir(exist_ok=True)
    return AppConfig(
        llm=LLMConfig(api_key="k", base_url="http://localhost:9", model=model),
        scheduler=SchedulerConfig(interval_seconds=60),
        projects=[ProjectConfig(name="p", path=proj_dir, enabled=True)],
        source_path=tmp / "config.yaml",
    )


def test_reload_returns_immediately_during_running_cycle() -> None:
    """一轮执行中调用 reload_config：必须毫秒级返回，绝不阻塞等锁。"""
    with tempfile.TemporaryDirectory() as tmp:
        sched = Scheduler(_make_cfg(Path(tmp)))
        gate = threading.Event()
        release = threading.Event()

        # 模拟一个长时间执行的调度轮（持 _cycle_lock）
        def slow_cycle() -> None:
            gate.set()
            acquired = sched._cycle_lock.acquire(timeout=5)
            assert acquired, "模拟轮应拿到锁"
            try:
                release.wait(timeout=10)  # 模拟任务跑了很久
            finally:
                sched._cycle_lock.release()

        t = threading.Thread(target=slow_cycle, daemon=True)
        t.start()
        gate.wait(timeout=5)
        time.sleep(0.1)  # 确保 slow_cycle 已持锁

        # 关键断言：任务执行中保存配置，reload_config 必须立即返回
        started = time.monotonic()
        sched.reload_config(_make_cfg(Path(tmp), model="m2"))
        elapsed = time.monotonic() - started
        release.set()  # 让模拟轮结束
        assert elapsed < 1.0, f"reload_config 阻塞了 {elapsed:.2f}s（应毫秒级返回）"
        # 新 config 已先行原子切换
        assert sched.config.llm.model == "m2"
        assert sched._agents_stale is True, "Agent 列表应标记待重建"
        t.join(timeout=5)
    print("ok: 一轮执行中 reload_config 毫秒级返回（不再阻塞保存请求）")


def test_agents_rebuilt_when_idle() -> None:
    """空闲时 reload_config：后台线程很快完成重建，_agents_stale 清除。"""
    with tempfile.TemporaryDirectory() as tmp:
        sched = Scheduler(_make_cfg(Path(tmp), model="m1"))
        sched.reload_config(_make_cfg(Path(tmp), model="m2"))
        # 后台重建通常 <1s；最多等 5s
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not sched._agents_stale:
                break
            time.sleep(0.05)
        assert sched._agents_stale is False, "空闲时后台应完成重建"
        assert len(sched.agents) == 1
        assert sched.agents[0].executor.llm.model == "m2", "重建后 Agent 应使用新模型"
    print("ok: 空闲时后台重建 Agent 列表并使用新配置")


def test_sync_agents_if_stale_before_cycle() -> None:
    """一轮执行中改配置：下一轮 run_cycle 开始前由 _sync_agents_if_stale 补建。"""
    with tempfile.TemporaryDirectory() as tmp:
        sched = Scheduler(_make_cfg(Path(tmp), model="m1"))
        gate = threading.Event()
        release = threading.Event()

        def slow_cycle() -> None:
            gate.set()
            sched._cycle_lock.acquire(timeout=5)
            try:
                release.wait(timeout=10)
            finally:
                sched._cycle_lock.release()

        t = threading.Thread(target=slow_cycle, daemon=True)
        t.start()
        gate.wait(timeout=5)
        time.sleep(0.1)
        sched.reload_config(_make_cfg(Path(tmp), model="m2"))
        release.set()
        t.join(timeout=5)

        # 下一轮开始：应先补建 Agent 名单再扫描
        stats = sched.run_cycle()
        assert sched._agents_stale is False, "run_cycle 开头应消费 stale 标记"
        assert sched.agents[0].executor.llm.model == "m2", "下轮应使用重建后的新 Agent"
        assert stats == {"p": 0}, "空任务项目正常扫描返回 0"
    print("ok: 执行中变更配置，下一轮 run_cycle 开头自动补建并生效")


def main() -> int:
    test_reload_returns_immediately_during_running_cycle()
    test_agents_rebuilt_when_idle()
    test_sync_agents_if_stale_before_cycle()
    print("\n保存设置不再阻塞（配置热重载异步化）全部测试通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
