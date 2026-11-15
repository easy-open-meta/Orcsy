"""运行台账（V1-R1）自检：事件、工件、脱敏、查询、保留期清理与单例。

风格与 test_usage.py 一致：main() + assert，直跑无 pytest。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import (AppConfig, LLMConfig, RunLogConfig, UpdateConfig)
from orcsy.runlog import (ARTIFACTS, RunStore, init_run_store, redact_text,
                          runs_root)
from orcsy.scheduler import Scheduler
from orcsy.web import AppState


def _read_events(run_dir: Path) -> list[dict]:
    text = (run_dir / "events.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _http_get(url: str, tries: int = 5) -> bytes:
    """本机回环偶发 RST（Windows 环境抖动）：GET 读取做有限重试。

    显式 Connection: close：不让服务端在响应后保持半开连接，规避回环复用
    连接被重置的抖动。
    """
    last: Exception | None = None
    for _ in range(tries):
        try:
            req = Request(url, headers={"Connection": "close"})
            with urlopen(req, timeout=10) as r:
                return r.read()
        except HTTPError:
            raise
        except Exception as e:  # noqa: BLE001 — 仅测试辅助
            last = e
            time.sleep(0.3)
    raise last  # type: ignore[misc]


def check_events_artifacts_and_redaction() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="runlog-"))
    try:
        store = RunStore(tmp / "runs", retention_days=30)
        rec = store.new_run("proj-a", "T-1", role="backend",
                            agent_name="be", model="deepseek-chat")
        assert rec is not None
        fake_key = "sk-" + "test" * 6          # 合成假密钥（27 位，命中 sk- 规则）
        rec.progress("第1轮 · read_file · src/app.py")
        rec.model_event("deepseek-chat", 1200, 80, turn=1)
        rec.tool_event("read_file", ok=True, ms=12, preview="src/app.py")
        # 工具参数里带密钥：台账必须落占位符而不是原文
        rec.tool_event("run_command", ok=True, ms=30,
                       preview=f"curl -H 'Authorization: Bearer {fake_key}'")
        rec.artifact("diff.patch", "--- a/x\n+++ b/x\n+hello\n")
        rec.record_command_output("pytest -q", "1 passed in 0.1s")
        rec.record_command_output("ls -la", "不应写入测试日志")
        rec.finish("done", "completed", commit="abc1234")

        run_dir = store.run_dir("proj-a", rec.run_id)
        assert (run_dir / "meta.json").is_file()
        assert (run_dir / "events.jsonl").is_file()
        assert (run_dir / "diff.patch").read_text(encoding="utf-8").endswith("+hello\n")
        test_log = (run_dir / "test.log").read_text(encoding="utf-8")
        assert "1 passed" in test_log and "不应写入" not in test_log, test_log

        events = _read_events(run_dir)
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1)), events
        assert events[0]["type"] == "run_start"
        assert events[-1]["type"] == "run_end"
        assert all(e["type"] in {
            "run_start", "claim", "turn", "model_call", "tool_call", "progress",
            "review_start", "review_verdict", "commit", "merge", "run_end",
        } for e in events)

        raw = (run_dir / "events.jsonl").read_text(encoding="utf-8")
        assert fake_key not in raw, "密钥原文不得写入台账"
        assert "[REDACTED" in raw, "应留下脱敏占位符"

        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        assert meta["outcome"] == "done" and meta["exit_reason"] == "completed"
        assert meta["commit"] == "abc1234"
        assert meta["event_count"] == len(events)

        loaded = store.load_run("proj-a", rec.run_id)
        assert loaded["meta"]["run_id"] == rec.run_id
        assert loaded["event_total"] == len(events)
        names = {a["name"] for a in loaded["artifacts"]}
        assert {"meta.json", "events.jsonl", "diff.patch", "test.log"} <= names, names

        page = store.load_run("proj-a", rec.run_id, offset=2, limit=3)
        assert [e["seq"] for e in page["events"]] == [3, 4, 5], page["events"]
        print("ok: 事件序号/工件/脱敏/回放分页")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_list_and_filters() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="runlog-list-"))
    try:
        store = RunStore(tmp / "runs")
        a = store.new_run("proj-a", "T-1", role="pm")
        b = store.new_run("proj-a", "T-2", role="backend")
        store.new_run("proj-b", "T-1", role="qa")
        a.finish("done", "completed")
        b.finish("blocked", "review_fail")
        runs = store.list_runs()
        assert len(runs) == 3, runs
        assert store.list_runs(project="proj-a") and len(store.list_runs(project="proj-a")) == 2
        only = store.list_runs(project="proj-a", task_id="T-2")
        assert len(only) == 1 and only[0]["role"] == "backend", only
        assert only[0]["exit_reason"] == "review_fail"
        assert store.list_runs(limit=1) and len(store.list_runs(limit=1)) == 1
        print("ok: 列表/项目/任务过滤/last limit")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_prune_by_age() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="runlog-prune-"))
    try:
        store = RunStore(tmp / "runs", retention_days=1)
        rec = store.new_run("proj-a", "T-1")
        rec.finish("done", "completed")
        run_dir = store.run_dir("proj-a", rec.run_id)
        old = time.time() - 3 * 86400
        for p in [run_dir, run_dir / "meta.json", run_dir / "events.jsonl"]:
            os.utime(p, (old, old))
        store.prune()
        assert not run_dir.exists(), "超过保留期的 run 应被清理"
        print("ok: 保留期清理")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_invalid_ids_and_disabled() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="runlog-bad-"))
    try:
        store = RunStore(tmp / "runs")
        rec = store.new_run("proj-a", "T-1")
        rec.finish("done", "completed")
        for bad in ("../escape", "a/b", "", "x" * 200):
            try:
                store.load_run("proj-a", bad)
            except (ValueError, FileNotFoundError):
                pass
            else:
                raise AssertionError(f"非法 run_id 应被拒绝：{bad!r}")
        try:
            store.read_artifact("proj-a", rec.run_id, "../meta.json")
        except ValueError:
            pass
        else:
            raise AssertionError("工件名必须走白名单")

        off = RunStore(tmp / "off", enabled=False)
        assert off.new_run("p", "T") is None
        assert off.list_runs() == []
        print("ok: 非法 ID/工件白名单/关闭开关")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_config_and_singleton() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="runlog-cfg-"))
    try:
        cfg = AppConfig(source_path=tmp / "config.yaml",
                        runlog=RunLogConfig(enabled=True, retention_days=7,
                                            max_total_mb=100))
        assert runs_root(cfg) == tmp / "runs"
        st = init_run_store(cfg, force=True)
        assert st.root == tmp / "runs" and st.enabled
        assert init_run_store(cfg) is st, "未 force 时应幂等返回同一实例"

        cfg2 = AppConfig(source_path=tmp / "config.yaml",
                         runlog=RunLogConfig(enabled=False))
        st2 = init_run_store(cfg2, force=True)
        assert st2 is not st and not st2.enabled
        assert runs_root(AppConfig(source_path=tmp / "cfg.yaml",
                                   runlog=RunLogConfig(dir=str(tmp / "custom")))) == tmp / "custom"

        # 脱敏不改变占位符形态（多次调用稳定）
        fake = "sk-" + "test" * 6
        sample = f"token={fake}"
        first, second = redact_text(sample), redact_text(sample)
        assert fake not in first and first == second, (first, second)
        print("ok: 配置解析/单例/自定义目录/脱敏稳定")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_web_api() -> None:
    """看板只读接口：list / detail / artifact 与错误参数。"""
    import orcsy.runlog as RL

    tmp = Path(tempfile.mkdtemp(prefix="runlog-web-"))
    try:
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text("llm: {}\n", encoding="utf-8")
        cfg = AppConfig(
            update=UpdateConfig(auto_check=False),
            llm=LLMConfig(api_key="fake", base_url="http://localhost:9", model="fake"),
            runlog=RunLogConfig(enabled=True), source_path=cfg_file)
        state = AppState(cfg, Scheduler(cfg))
        store = RL.get_run_store()
        assert store.root == tmp / "runs"
        rec = store.new_run("webproj", "T9", role="pm")
        rec.finish("done", "completed")

        lst = state.list_runs({"project": "webproj"})
        assert lst["ok"] and len(lst["runs"]) == 1
        assert lst["runs"][0]["run_id"] == rec.run_id

        det = state.get_run({"project": "webproj", "run_id": rec.run_id, "limit": 50})
        assert det["ok"] and det["meta"]["outcome"] == "done"
        assert det["events"][-1]["type"] == "run_end"

        data, mime = state.run_artifact(
            {"project": "webproj", "run_id": rec.run_id, "name": "events.jsonl"})
        assert b"run_end" in data and "text" in mime

        # HTTP 路由层：只读 GET 端点（鉴权未开启时本机可直连）
        from orcsy.web import make_server
        server = make_server(state, "127.0.0.1", 0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        try:
            body = json.loads(_http_get(f"{base}/api/runs?project=webproj").decode("utf-8"))
            assert body["ok"] and body["runs"][0]["run_id"] == rec.run_id, body

            body = json.loads(_http_get(
                f"{base}/api/runs/detail?project=webproj"
                f"&run_id={rec.run_id}&limit=50").decode("utf-8"))
            assert body["ok"] and body["events"][-1]["type"] == "run_end", body

            assert b"run_end" in _http_get(
                f"{base}/api/runs/artifact?project=webproj"
                f"&run_id={rec.run_id}&name=events.jsonl")

            # 越界工件名 → 400（回环偶发 RST，按期望状态码重试；HTTPError
            # 本身就是「连接成功并返回 4xx」，不需要再重试）
            code = None
            for _ in range(5):
                try:
                    urlopen(f"{base}/api/runs/artifact?project=webproj"
                            f"&run_id={rec.run_id}&name=../meta.json", timeout=10)
                except HTTPError as e:
                    code = e.code
                    break
                except Exception:  # noqa: BLE001 — 回环抖动，重试
                    time.sleep(0.3)
                else:
                    raise AssertionError("工件名越界应返回 400")
            assert code == 400, code
        finally:
            server.shutdown()

        for bad in ({"project": "", "run_id": rec.run_id},
                    {"project": "webproj", "run_id": ""}):
            try:
                state.get_run(bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"缺参数应报错：{bad}")
        print("ok: Web 只读接口（列表/详情/工件/参数校验）")
    finally:
        RL._STORE = None       # 还原单例，避免污染同进程其它测试
        shutil.rmtree(tmp, ignore_errors=True)


def _wait_migration(timeout: float = 10) -> None:
    """目录迁移是后台线程：等它结束再继续保存（避免「迁移中拒绝修改」）。"""
    import orcsy.runlog as RL
    end = time.time() + timeout
    while RL.get_run_store().is_migrating() and time.time() < end:
        time.sleep(0.02)
    assert not RL.get_run_store().is_migrating(), "迁移超时未完成"


def check_settings_save_and_hot_reload() -> None:
    """看板保存 runlog 分块：落盘 + 单例热重建 + 非法值拒绝。"""
    import yaml

    import orcsy.runlog as RL
    from orcsy.settings_service import save_settings

    tmp = Path(tempfile.mkdtemp(prefix="runlog-save-"))
    try:
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text("llm: {}\n", encoding="utf-8")
        cfg = AppConfig(
            update=UpdateConfig(auto_check=False),
            llm=LLMConfig(api_key="fake", base_url="http://localhost:9", model="fake"),
            runlog=RunLogConfig(enabled=True), source_path=cfg_file)
        state = AppState(cfg, Scheduler(cfg))
        assert RL.get_run_store().enabled is True

        save_settings(state, {"runlog": {
            "enabled": False, "retention_days": 0, "max_total_mb": 0}})
        assert state.config.runlog.enabled is False
        assert state.config.runlog.retention_days == 0
        assert RL.get_run_store().enabled is False, "保存后应重建台账单例（热生效）"

        saved = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        assert saved["runlog"] == {
            "enabled": False, "retention_days": 0, "max_total_mb": 0}, saved

        # 自定义目录：保存即热切换，空串回默认；相对路径 / 文件路径拒绝
        save_settings(state, {"runlog": {"enabled": True}})
        custom = tmp / "custom-runs"
        save_settings(state, {"runlog": {"dir": str(custom)}})
        _wait_migration()
        assert state.config.runlog.dir == str(custom)
        assert RL.get_run_store().root == custom
        assert custom.is_dir(), "自定义台账目录应自动创建"

        save_settings(state, {"runlog": {"dir": ""}})
        _wait_migration()
        assert state.config.runlog.dir == ""
        assert RL.get_run_store().root == tmp / "runs"

        try:
            save_settings(state, {"runlog": {"dir": "relative/runs"}})
        except ValueError:
            pass
        else:
            raise AssertionError("相对路径应被拒绝")
        not_a_dir = tmp / "not-a-dir"
        not_a_dir.write_text("x", encoding="utf-8")
        try:
            save_settings(state, {"runlog": {"dir": str(not_a_dir)}})
        except ValueError:
            pass
        else:
            raise AssertionError("已存在的文件路径应被拒绝")

        for bad in ({"retention_days": -1}, {"max_total_mb": -5},
                    {"retention_days": 99999}):
            try:
                save_settings(state, {"runlog": bad})
            except ValueError:
                pass
            else:
                raise AssertionError(f"非法 runlog 配置应被拒绝：{bad}")
        print("ok: 设置分块保存 + 热重建 + 落盘 + 非法值拒绝")
    finally:
        RL._STORE = None
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_events_artifacts_and_redaction()
    check_list_and_filters()
    check_prune_by_age()
    check_invalid_ids_and_disabled()
    check_config_and_singleton()
    check_web_api()
    check_settings_save_and_hot_reload()
    print("\n运行台账自检全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
