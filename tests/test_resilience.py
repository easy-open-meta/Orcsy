"""熔断停机（V1-R4）自检：连续失败、窗口失败率、人工/自动恢复、扫描软停与设置。

风格与 test_runlog.py 一致：main() + assert，直跑无 pytest。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import orcsy.resilience as R
from orcsy.agent import ProjectAgent
from orcsy.config import (AppConfig, LLMConfig, ProjectConfig,
                          ResilienceConfig, RunLogConfig, UpdateConfig)
from orcsy.scheduler import Scheduler
from orcsy.settings_service import save_settings
from orcsy.web import AppState


def _cfg(**kw) -> ResilienceConfig:
    base = dict(enabled=True, max_consecutive_failures=3, window_minutes=120,
                max_failure_rate=0.0, min_samples=5, auto_resume=False)
    base.update(kw)
    return ResilienceConfig(**base)


def _reset() -> None:
    R._STORE = None
    R.configure(ResilienceConfig())


def _fail(st, project="p", n=1, reason=""):
    for i in range(n):
        st.record(project, f"T{i}", outcome="blocked", block_reason=reason)


def check_consecutive_trip() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="res-consec-"))
    try:
        st = R.ResilienceStore(tmp / "state", _cfg())
        _fail(st, n=2)
        assert not st.status("p")["paused"]
        _fail(st, n=1)
        s = st.status("p")
        assert s["paused"] and "连续 3 次失败" in s["pause_reason"], s

        # 成功会重置连续计数
        st2 = R.ResilienceStore(tmp / "state2", _cfg())
        st2.record("p", "A", outcome="blocked")
        st2.record("p", "B", outcome="blocked")
        st2.record("p", "C", outcome="done")
        st2.record("p", "D", outcome="blocked")
        st2.record("p", "E", outcome="blocked")
        assert not st2.status("p")["paused"], st2.status("p")

        # 受控停止（预算/任务改写）不计入熔断
        st3 = R.ResilienceStore(tmp / "state3", _cfg())
        for i in range(5):
            st3.record("p", f"B{i}", outcome="blocked",
                       block_reason="budget_exceeded")
        _fail(st3, n=2, reason="stale_lease")
        assert not st3.status("p")["paused"], st3.status("p")
        print("ok: 连续失败触发 + 成功重置 + 受控停止不计入")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_rate_trip_and_window() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="res-rate-"))
    try:
        cfg = _cfg(max_consecutive_failures=0, max_failure_rate=0.6,
                   min_samples=5, window_minutes=60)
        st = R.ResilienceStore(tmp / "state", cfg)
        for i in range(3):
            st.record("p", f"F{i}", outcome="blocked")
        for i in range(2):
            st.record("p", f"O{i}", outcome="done")
        s = st.status("p")
        assert s["paused"], s
        assert s["window_samples"] == 5 and s["window_failures"] == 3, s
        assert "失败率 60%" in s["pause_reason"], s["pause_reason"]

        # 窗口外的旧失败不计入：把事件时间改到 2 小时前，再补 2 条成功
        data = json.loads(st.path.read_text(encoding="utf-8"))
        old = time.time() - 7200
        for e in data["projects"]["p"]["events"]:
            e["ts"] = old
        st.path.write_text(json.dumps(data), encoding="utf-8")
        st2 = R.ResilienceStore(tmp / "state", cfg)
        st2.record("p", "N1", outcome="done")
        st2.record("p", "N2", outcome="done")
        s2 = st2.status("p")
        assert s2["window_samples"] == 2, s2
        print("ok: 窗口失败率触发 + 窗口外旧事件忽略")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_resume() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="res-resume-"))
    try:
        st = R.ResilienceStore(tmp / "state", _cfg())
        _fail(st, n=3)
        assert st.status("p")["paused"]
        s = st.resume("p")
        assert not s["paused"], s
        st2 = st.status("p")
        assert not st2["paused"] and st2["consecutive_failures"] == 0, st2
        _fail(st, n=1)
        assert not st.status("p")["paused"], "人工恢复后应重新计数"

        st.pause("p", "手动停机", by="manual")
        assert st.status("p")["paused"]
        print("ok: 人工恢复清空失败史 + 手动暂停")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_auto_resume() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="res-auto-"))
    try:
        cfg = _cfg(auto_resume=True, window_minutes=1)
        st = R.ResilienceStore(tmp / "state", cfg)
        st.pause("p", "test")
        assert st.status("p")["paused"]
        # 把暂停时间拨到 61 秒前；下次 status 应自动恢复
        with st._mutate() as data:
            data["projects"]["p"]["paused_at"] = time.time() - 61
        s = st.status("p")
        assert not s["paused"], s
        print("ok: auto_resume 窗口结束后自动恢复")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_agent_soft_stop() -> None:
    from e2e_fake_llm import FakeLLMClient, make_project, make_task

    tmp = Path(tempfile.mkdtemp(prefix="res-scan-"))
    try:
        proj = make_project(tmp)
        make_task(proj.path)
        cfg = _cfg()
        R.configure(cfg)
        init = R.init_resilience_store(None, state_dir=tmp / "state", force=True)
        init.pause("sample-app", "测试停机")
        llm = LLMConfig(api_key="k", base_url="http://localhost:9", model="m")
        agent = ProjectAgent(proj, llm, state_dir=tmp / "state")
        FakeLLMClient.script = []
        FakeLLMClient.calls = []
        results = agent.scan_and_work()
        assert results == [], results
        assert "测试停机" in agent.pause_reason, agent.pause_reason
        assert FakeLLMClient.calls == [], "熔断后不应调用模型"
        print("ok: 熔断项目扫描软停（零模型调用）")
    finally:
        _reset()
        shutil.rmtree(tmp, ignore_errors=True)


def check_settings_and_resume_api() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="res-web-"))
    try:
        proj = tmp / "proj"
        proj.mkdir()
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text("llm: {}\n", encoding="utf-8")
        cfg = AppConfig(
            update=UpdateConfig(auto_check=False),
            llm=LLMConfig(api_key="k", base_url="http://localhost:9", model="m"),
            projects=[ProjectConfig(name="proj", path=proj)],
            runlog=RunLogConfig(enabled=True),
            source_path=cfg_file)
        R._STORE = None
        state = AppState(cfg, Scheduler(cfg))
        save_settings(state, {"resilience": {
            "enabled": True, "max_consecutive_failures": 4,
            "window_minutes": 30, "max_failure_rate": 0.5,
            "min_samples": 6, "auto_resume": True}})
        assert state.config.resilience.max_consecutive_failures == 4
        assert R.get_resilience_config().max_failure_rate == 0.5
        saved = cfg_file.read_text(encoding="utf-8")
        assert "max_consecutive_failures: 4" in saved

        st = R.get_resilience_store()
        st.pause("proj", "测试")
        res = state.resume_project({"project": "proj"})
        assert res["ok"] and not res["health"]["paused"], res
        for bad in ({"max_consecutive_failures": -1},
                    {"window_minutes": 0}, {"max_failure_rate": 2},
                    {"min_samples": 0}):
            try:
                save_settings(state, {"resilience": bad})
            except ValueError:
                pass
            else:
                raise AssertionError(f"非法熔断配置应被拒绝：{bad}")
        print("ok: 设置保存/热生效 + 人工恢复 API + 非法值拒绝")
    finally:
        _reset()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_consecutive_trip()
    check_rate_trip_and_window()
    check_resume()
    check_auto_resume()
    check_agent_soft_stop()
    check_settings_and_resume_api()
    print("\n熔断停机自检全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
