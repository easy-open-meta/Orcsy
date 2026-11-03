"""模型用量台账自检：记录/聚合/逐日分桶/持久化重载/截断/Web 层曝光。

风格与 test_project_llm.py 一致：main() + assert，直跑无 pytest。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import orcsy.usage as U
from orcsy.config import AppConfig, UpdateConfig, LLMConfig
from orcsy.scheduler import Scheduler
from orcsy.usage import UsageStore, get_usage_store, init_usage_store
from orcsy.web import AppState


def check_record_and_summary() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="usage-"))
    try:
        st = UsageStore(tmp / "usage.jsonl")
        st.record("proj-a", "T1", "done", 5, 1000, 200, "deepseek-chat")
        st.record("proj-a", "T2", "blocked", 3, 500, 80, "deepseek-chat")
        st.record("proj-b", "T3", "done", 8, 4000, 900, "glm-4-plus")
        s = st.summary()
        assert s["total"] == {"tasks": 3, "turns": 16,
                              "prompt_tokens": 5500, "completion_tokens": 1180}, s["total"]
        # 项目聚合按总 tokens 降序
        assert [p["name"] for p in s["projects"]] == ["proj-b", "proj-a"]
        pa = s["projects"][1]
        assert pa["tasks"] == 2 and pa["turns"] == 8
        assert pa["prompt_tokens"] == 1500 and pa["completion_tokens"] == 280
        assert pa["last_at"], "应带最近使用时间"
        # 最近记录倒序、上限 20
        assert [r["task_id"] for r in s["recent"]] == ["T3", "T2", "T1"]
        # None/脏值安全
        st.record("proj-c", "T4", "done", None, None, None, "")
        s2 = st.summary()
        assert s2["total"]["tasks"] == 4 and s2["total"]["turns"] == 16
        print("ok: record/summary（总体/项目降序/倒序最近/脏值安全）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_days_bucket() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="usage-days-"))
    try:
        st = UsageStore(tmp / "usage.jsonl")
        st.record("p", "T1", "done", 1, 100, 10, "m")
        s = st.summary(days=14)
        assert len(s["days"]) == 14, "近 14 天窗口含零值天"
        today = time.strftime("%Y-%m-%d")
        last = s["days"][-1]
        assert last["date"] == today and last["tasks"] == 1
        assert last["prompt_tokens"] == 100 and last["completion_tokens"] == 10
        assert all(d["tasks"] == 0 for d in s["days"][:-1])
        # 昨天的记录落进昨天的桶（直接改内存条目 ts 模拟）
        st._entries[0]["ts"] -= 86400
        st._entries[0]["at"] = time.strftime("%Y-%m-%d %H:%M",
                                             time.localtime(st._entries[0]["ts"]))
        s = st.summary(days=14)
        assert s["days"][-2]["tasks"] == 1 and s["days"][-1]["tasks"] == 0
        print("ok: 逐日分桶（14 天窗口/零值天/跨天归桶）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_persistence_and_truncation() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="usage-persist-"))
    path = tmp / "logs" / "usage.jsonl"
    try:
        st = UsageStore(path)
        for i in range(3):
            st.record("p", f"T{i}", "done", 1, 10, 5, "m")
        assert path.is_file(), "应自动创建 logs 目录并落盘"
        # 重载：新实例从盘读回
        st2 = UsageStore(path)
        assert st2.summary()["total"]["tasks"] == 3
        # 脏行容忍
        with open(path, "a", encoding="utf-8") as f:
            f.write("{半行损坏\nnot json\n")
        st3 = UsageStore(path)
        assert st3.summary()["total"]["tasks"] == 3, "脏行应跳过不炸"
        # 截断：上限内只保留最新
        old_max = U._MAX_ENTRIES
        U._MAX_ENTRIES = 5
        try:
            st4 = UsageStore(tmp / "trunc.jsonl")
            for i in range(8):
                st4.record("p", f"T{i}", "done", 1, 1, 1, "m")
            s = st4.summary()
            assert s["total"]["tasks"] == 5
            assert [r["task_id"] for r in s["recent"]][0] == "T7"  # recent 倒序，最新在前
            assert [r["task_id"] for r in s["recent"]][-1] == "T3"  # 最旧的 T0~T2 已截断
            # 盘上同步截断
            st5 = UsageStore(tmp / "trunc.jsonl")
            assert st5.summary()["total"]["tasks"] == 5
        finally:
            U._MAX_ENTRIES = old_max
        print("ok: 持久化重载/脏行容忍/超上限截断（内存+磁盘同步）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_singleton_and_web() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="usage-web-"))
    try:
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text("llm: {}\n", encoding="utf-8")
        cfg = AppConfig(
        update=UpdateConfig(auto_check=False),  # 测试不触发后台更新检查
            llm=LLMConfig(api_key="fake", base_url="http://localhost:9", model="fake"),
            source_path=cfg_file)
        # init 幂等 + 路径锚定到配置目录 logs/
        st = init_usage_store(cfg)
        assert st is init_usage_store(cfg)
        assert st.path == tmp / "logs" / "usage.jsonl"
        st.record("webproj", "T9", "done", 4, 777, 88, "fake")
        # AppState 曝光：full_state.usage 与台账一致
        state = AppState(cfg, Scheduler(cfg))
        u = state.full_state()["usage"]
        assert u["total"]["tasks"] == 1
        assert u["projects"][0]["name"] == "webproj"
        assert u["projects"][0]["prompt_tokens"] == 777
        assert u["recent"][0]["task_id"] == "T9"
        assert len(u["days"]) == 84  # 热力图窗口：12 周逐日（含零值天）
        # get_usage_store 返回同一单例
        assert get_usage_store() is st
        print("ok: 单例锚定配置目录 + full_state.usage 曝光")
    finally:
        # 还原单例，避免污染同进程其它测试
        U._STORE = None
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_record_and_summary()
    check_days_bucket()
    check_persistence_and_truncation()
    check_singleton_and_web()
    print("\n模型用量台账自检全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
