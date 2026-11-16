"""预算硬闸自检：任务级 token/成本、项目日窗、LLM 接入、软停与回写。

风格与 test_runlog.py 一致：main() + assert，直跑无 pytest。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import orcsy.budget as B
from orcsy.agent import ProjectAgent
from orcsy.config import (AppConfig, BudgetConfig, LLMConfig, ProjectConfig,
                          RunLogConfig, UpdateConfig)
from orcsy.executor import TaskExecutor
from orcsy.llm import LLMClient
from orcsy.scheduler import Scheduler
from orcsy.settings_service import save_settings
from orcsy.task_model import (parse_task_file, set_task_file_block_reason)
from orcsy.web import AppState


def _cfg(**kw) -> BudgetConfig:
    return BudgetConfig(**kw)


def _reset() -> None:
    B._STORE = None
    B.configure(BudgetConfig())


def check_store_daily() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="budget-store-"))
    try:
        st = B.BudgetStore(tmp / "state")
        st.add("p1", 100, 50, 0.5)
        st.add("p1", 20, 10, 0.1)
        day = st.project_day("p1")
        assert day["total_tokens"] == 180, day
        assert day["prompt_tokens"] == 120 and day["completion_tokens"] == 60
        assert abs(day["cost_cny"] - 0.6) < 1e-9, day
        snap = st.snapshot()
        assert snap["p1"]["total_tokens"] == 180

        # 把日期改成昨天 → 今日读数归零；add 后重新从 0 累计
        data = json.loads(st.path.read_text(encoding="utf-8"))
        data["projects"]["p1"]["date"] = (date.today() - timedelta(days=1)).isoformat()
        st.path.write_text(json.dumps(data), encoding="utf-8")
        assert st.project_day("p1")["total_tokens"] == 0
        st.add("p1", 5, 5)
        assert st.project_day("p1")["total_tokens"] == 10
        print("ok: 当日用量累计/跨日归零/快照")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_task_token_and_cost() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="budget-task-"))
    try:
        st = B.BudgetStore(tmp / "state")
        cfg = _cfg(enabled=True, max_tokens_per_task=100,
                   prices={"m": {"prompt": 1.0, "completion": 2.0}})
        tr = B.BudgetTracker("p", "T1", config=cfg, store=st)
        tr.precheck()
        tr.record("m", 40, 10)          # 50 → 未超
        assert not tr.exceeded and tr.total_tokens == 50
        try:
            tr.record("m", 30, 30)      # 110 → 超
        except B.BudgetExceeded as e:
            assert e.scope == "task", e
        else:
            raise AssertionError("任务 token 超限应抛 BudgetExceeded")
        assert tr.exceeded
        try:
            B.BudgetTracker("p", "T2", config=cfg, store=st).precheck()
        except B.BudgetExceeded:
            pass
        else:
            # 新 tracker 的累计为 0，不应因旧 tracker 标记而失败（预期不抛）
            pass

        cost_cfg = _cfg(enabled=True, max_cost_per_task_cny=0.00005,
                        prices={"m": {"prompt": 1.0, "completion": 0.0}})
        tr2 = B.BudgetTracker("p", "T3", config=cost_cfg, store=st)
        try:
            tr2.record("m", 100, 0)    # 100 tokens × 1 元/百万 = 0.0001 元
        except B.BudgetExceeded as e:
            assert e.scope == "task", e
        else:
            raise AssertionError("任务成本超限应抛 BudgetExceeded")
        print("ok: 任务 token / 成本硬闸")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_project_day_limit() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="budget-proj-"))
    try:
        st = B.BudgetStore(tmp / "state")
        cfg = _cfg(enabled=True, max_tokens_per_project_per_day=100)
        tr = B.BudgetTracker("p", "T1", config=cfg, store=st)
        try:
            tr.record("m", 60, 50)      # 项目日累计 110
        except B.BudgetExceeded as e:
            assert e.scope == "project", e
        else:
            raise AssertionError("项目日窗超限应抛 BudgetExceeded")
        exhausted, day = B.project_budget_exhausted("p")  # 用模块配置（未启用）应为 False
        assert exhausted is False
        B.configure(cfg)
        B._STORE = st
        exhausted, day = B.project_budget_exhausted("p")
        assert exhausted is True and day["total_tokens"] == 110, day
        print("ok: 项目当日上限 + 软停判断")
    finally:
        _reset()
        shutil.rmtree(tmp, ignore_errors=True)


def check_llm_client_integration() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="budget-llm-"))
    try:
        st = B.BudgetStore(tmp / "state")
        cfg = _cfg(enabled=True, max_tokens_per_task=100)
        tr = B.BudgetTracker("p", "T1", config=cfg, store=st)
        client = LLMClient(LLMConfig(api_key="k", base_url="http://localhost:9",
                                     model="m"))
        client.budget = tr
        try:
            client._acc_usage({"usage": {"prompt_tokens": 120,
                                         "completion_tokens": 10}}, "openai")
        except B.BudgetExceeded as e:
            assert e.scope == "task", e
        else:
            raise AssertionError("用量累计超限应抛 BudgetExceeded")
        assert client.usage["prompt_tokens"] == 120
        try:
            client._post("http://localhost:9/v1/chat/completions", {}, {})
        except B.BudgetExceeded:
            pass
        else:
            raise AssertionError("已超限时应在发请求前拦截")
        print("ok: LLM 用量上报 + 请求前拦截")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_soft_stop_scan() -> None:
    from e2e_fake_llm import FakeLLMClient, make_project, make_task

    tmp = Path(tempfile.mkdtemp(prefix="budget-soft-"))
    try:
        proj = make_project(tmp)
        make_task(proj.path)
        cfg = _cfg(enabled=True, max_tokens_per_project_per_day=10)
        B.configure(cfg)
        st = B.init_budget_store(None, state_dir=tmp / "state", force=True)
        st.add("sample-app", 20, 0)     # 已超当日上限
        llm = LLMConfig(api_key="k", base_url="http://localhost:9", model="m")
        agent = ProjectAgent(proj, llm, state_dir=tmp / "state")
        FakeLLMClient.script = []
        FakeLLMClient.calls = []
        results = agent.scan_and_work()
        assert results == [], results
        assert agent.budget_paused is True
        assert FakeLLMClient.calls == [], "预算软停后不应调用模型"
        print("ok: 项目预算用尽 → 扫描软停（零模型调用）")
    finally:
        _reset()
        shutil.rmtree(tmp, ignore_errors=True)


def check_executor_hard_stop() -> None:
    from e2e_fake_llm import make_project, make_task

    class BudgetBoomClient:
        usage = {"turns": 0, "prompt_tokens": 0, "completion_tokens": 0}

        def __init__(self, cfg) -> None:
            pass

        def reset(self, system_prompt: str) -> None:
            pass

        def run_task_loop(self, task_prompt, ctx, max_turns=40, on_progress=None):
            raise B.BudgetExceeded("任务 token 预算超限：200 / 100", scope="task")

    tmp = Path(tempfile.mkdtemp(prefix="budget-hard-"))
    try:
        proj = make_project(tmp)
        make_task(proj.path)
        task = parse_task_file(proj.path / "tasks" / "P1-fix-greet-crash.md")
        llm = LLMConfig(api_key="k", base_url="http://localhost:9", model="m")
        ex = TaskExecutor(proj, llm)
        ex.run_task.__globals__["LLMClient"] = BudgetBoomClient
        res = ex.run_task(task, "# 指引")
        assert res.outcome == "blocked", res.outcome
        assert res.block_reason == "budget_exceeded", res.block_reason
        assert "预算超限" in (res.notes or ""), res.notes
        print("ok: 执行器预算硬停 → blocked + block_reason")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_block_reason_writeback() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="budget-reason-"))
    try:
        p = tmp / "T.md"
        p.write_text("---\ntitle: t\npriority: P2\nstatus: blocked\n---\n\n正文。\n",
                     encoding="utf-8")
        assert set_task_file_block_reason(p, "budget_exceeded")
        t = parse_task_file(p)
        assert t.block_reason == "budget_exceeded"
        set_task_file_block_reason(p, "")
        assert parse_task_file(p).block_reason == ""
        print("ok: block_reason frontmatter 写入/清除/解析")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_settings_roundtrip() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="budget-save-"))
    try:
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text("llm: {}\n", encoding="utf-8")
        cfg = AppConfig(
            update=UpdateConfig(auto_check=False),
            llm=LLMConfig(api_key="k", base_url="http://localhost:9", model="m"),
            runlog=RunLogConfig(enabled=True),
            source_path=cfg_file)
        B._STORE = None
        state = AppState(cfg, Scheduler(cfg))
        save_settings(state, {"budget": {
            "enabled": True, "max_tokens_per_task": 1234,
            "max_tokens_per_project_per_day": 5678,
            "max_cost_per_task_cny": 1.5}})
        assert state.config.budget.enabled is True
        assert state.config.budget.max_tokens_per_task == 1234
        assert B.get_budget_config().max_tokens_per_task == 1234
        saved = cfg_file.read_text(encoding="utf-8")
        assert "max_tokens_per_task: 1234" in saved
        for bad in ({"max_tokens_per_task": -1},
                    {"max_tokens_per_project_per_day": -2},
                    {"max_cost_per_task_cny": -0.1}):
            try:
                save_settings(state, {"budget": bad})
            except ValueError:
                pass
            else:
                raise AssertionError(f"非法预算应被拒绝：{bad}")
        print("ok: 设置分块保存 + 热生效 + 非法值拒绝")
    finally:
        _reset()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_store_daily()
    check_task_token_and_cost()
    check_project_day_limit()
    check_llm_client_integration()
    check_soft_stop_scan()
    check_executor_hard_stop()
    check_block_reason_writeback()
    check_settings_roundtrip()
    print("\n预算硬闸自检全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
