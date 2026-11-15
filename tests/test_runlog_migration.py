"""运行台账目录迁移（V1-R1）自检：历史迁移、活跃 run 合并、崩溃续跑、校验与配置联动。

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

import orcsy.runlog as RL
from orcsy.config import (AppConfig, LLMConfig, ProjectConfig, RunLogConfig,
                          UpdateConfig, WebConfig)
from orcsy.runlog import RunStore, init_run_store
from orcsy.scheduler import Scheduler
from orcsy.settings_service import save_settings
from orcsy.web import AppState


def _events(run_dir: Path) -> list[dict]:
    path = run_dir / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()
            if x.strip()]


def _wait(store: RunStore, timeout: float = 15) -> dict:
    end = time.time() + timeout
    while store.is_migrating() and time.time() < end:
        time.sleep(0.02)
    state = store.migration_status()
    assert state["state"] == "done", state
    return state


def check_finished_runs_migrate() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mig-finished-"))
    try:
        a, b = tmp / "a", tmp / "b"
        store = RunStore(a)
        r1 = store.new_run("proj", "T-1", role="pm")
        r1.progress("one"); r1.finish("done", "completed")
        r2 = store.new_run("proj", "T-2", role="backend")
        r2.progress("two"); r2.artifact("diff.patch", "+x\n")
        r2.finish("blocked", "review_fail")

        st = store.relocate(b, background=False)
        assert st["state"] == "done" and st["percent"] == 100.0, st
        assert store.root == b
        runs = store.list_runs(project="proj")
        assert len(runs) == 2, runs
        assert {r["task_id"] for r in runs} == {"T-1", "T-2"}
        assert not (a / "proj").exists(), "旧项目目录应清空并移除"
        loaded = store.load_run("proj", r2.run_id)
        assert loaded["meta"]["outcome"] == "blocked"
        assert (b / "proj" / r2.run_id / "diff.patch").is_file()
        print("ok: 历史 run 全量迁移（列表/回放/旧目录清理）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_active_run_merge() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mig-active-"))
    try:
        a, b = tmp / "a", tmp / "b"
        store = RunStore(a)
        rec = store.new_run("proj", "T-live", role="backend", agent_name="be")
        rec.progress("迁移前-1")
        rec.artifact("diff.patch", "old\n")
        assert rec.dir.parent.parent == a

        store.relocate(b, background=False)
        assert rec.dir.parent.parent == b, rec.dir
        rec.progress("迁移后-2")
        rec.model_event("m", 10, 2, turn=1)
        rec.record_command_output("pytest -q", "1 passed")
        rec.finish("done", "completed")

        run_dir = b / "proj" / rec.run_id
        events = _events(run_dir)
        seqs = [e["seq"] for e in events]
        assert seqs == list(range(1, len(seqs) + 1)), seqs
        text = (run_dir / "events.jsonl").read_text(encoding="utf-8")
        assert "迁移前-1" in text and "迁移后-2" in text
        assert events[-1]["type"] == "run_end"
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        assert meta["outcome"] == "done" and meta["event_count"] == len(events)
        assert not (a / "proj" / rec.run_id).exists(), "旧目录不应残留"
        print("ok: 活跃 run 迁移后事件连续（seq 1..N，前后内容都在）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_same_run_id_merge() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mig-merge-"))
    try:
        a, b = tmp / "a", tmp / "b"
        store = RunStore(a)
        rec = store.new_run("proj", "T-dup")
        rec.progress("p1"); rec.finish("done", "completed")
        src_dir = a / "proj" / rec.run_id
        dst_dir = b / "proj" / rec.run_id
        dst_dir.parent.mkdir(parents=True)
        shutil.copytree(src_dir, dst_dir)
        # 目标侧多一条更晚的事件（模拟中断后重试的残留）
        with open(dst_dir / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"seq": 99, "ts": "x", "type": "progress",
                                "actor": "a", "data": {"line": "dst-only"}},
                               ensure_ascii=False) + "\n")
        store.relocate(b, background=False)
        events = _events(b / "proj" / rec.run_id)
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(set(seqs)), seqs
        assert 99 in seqs, seqs
        assert any(e["data"].get("line") == "dst-only" for e in events)
        print("ok: 同名 run 合并（seq 并集、无丢失）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_crash_resume() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mig-resume-"))
    try:
        a, b = tmp / "a", tmp / "b"
        journal = tmp / "runlog-migration.json"
        state_path = tmp / "runlog-state.json"
        store = RunStore(a, state_path=state_path, journal_path=journal)
        rec = store.new_run("proj", "T-crash")
        rec.progress("before"); rec.finish("done", "completed")

        # 模拟「切了 root、搬了一部分」：目标已有半成品 + journal 未完成
        partial = b / "proj" / (rec.run_id + ".migrating")
        partial.mkdir(parents=True)
        (partial / "junk.txt").write_text("junk", encoding="utf-8")
        journal.write_text(json.dumps({
            "schema_version": 1, "old_root": str(a), "new_root": str(b),
            "state": "running", "started_at": "x"}, ensure_ascii=False),
            encoding="utf-8")

        store2 = RunStore(b, state_path=state_path, journal_path=journal)
        assert store2.resume_if_needed(background=False)
        assert store2.migration_status()["state"] == "done"
        assert (b / "proj" / rec.run_id / "meta.json").is_file()
        assert not partial.exists(), "半成品目录应被清理"
        assert not journal.exists(), "完成后应删除迁移日志"
        print("ok: 崩溃续跑（journal + .migrating 清理）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_validation() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mig-validate-"))
    try:
        a = tmp / "a"
        store = RunStore(a)
        for bad in (a / "nested", tmp):
            try:
                store.relocate(bad, background=False)
            except ValueError:
                pass
            else:
                raise AssertionError(f"嵌套目录应被拒绝：{bad}")
        f = tmp / "file.txt"
        f.write_text("x", encoding="utf-8")
        try:
            store.relocate(f, background=False)
        except ValueError:
            pass
        else:
            raise AssertionError("文件路径应被拒绝")
        assert store.root == a, "校验失败不能切换 root"

        # 受管项目内目录 + 迁移中拒绝保存
        proj = tmp / "proj"
        proj.mkdir()
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text("llm: {}\n", encoding="utf-8")
        cfg = AppConfig(
            update=UpdateConfig(auto_check=False),
            llm=LLMConfig(api_key="k", base_url="http://localhost:9", model="m"),
            projects=[ProjectConfig(name="p", path=proj)],
            web=WebConfig(port=5219, open_browser=False),
            runlog=RunLogConfig(enabled=True), source_path=cfg_file)
        RL._STORE = None
        state = AppState(cfg, Scheduler(cfg))
        try:
            save_settings(state, {"runlog": {"dir": str(proj / "runs")}})
        except ValueError as e:
            assert "受管项目" in str(e), e
        else:
            raise AssertionError("项目内目录应被拒绝")

        store = RL.get_run_store()
        store._migration["state"] = "running"
        try:
            save_settings(state, {"runlog": {"enabled": False}})
        except ValueError as e:
            assert "迁移" in str(e), e
        else:
            raise AssertionError("迁移中应拒绝修改")
        store._migration["state"] = "idle"
        print("ok: 嵌套/文件/项目内目录/迁移中修改全部拒绝")
    finally:
        RL._STORE = None
        shutil.rmtree(tmp, ignore_errors=True)


def check_settings_dir_roundtrip() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mig-settings-"))
    try:
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text("llm: {}\n", encoding="utf-8")
        cfg = AppConfig(
            update=UpdateConfig(auto_check=False),
            llm=LLMConfig(api_key="k", base_url="http://localhost:9", model="m"),
            runlog=RunLogConfig(enabled=True), source_path=cfg_file)
        RL._STORE = None
        state = AppState(cfg, Scheduler(cfg))
        store = RL.get_run_store()
        default_root = store.root
        rec = store.new_run("proj", "T-s")
        rec.progress("x"); rec.finish("done", "completed")

        target = tmp / "custom-runs"
        save_settings(state, {"runlog": {"dir": str(target)}})
        _wait(store)
        assert state.config.runlog.dir == str(target)
        assert store.root == target
        assert (target / "proj" / rec.run_id / "meta.json").is_file()
        assert not (default_root / "proj" / rec.run_id).exists()
        assert json.loads((tmp / "runlog-state.json").read_text(
            encoding="utf-8"))["last_root"] == str(target)

        # 显式留空 → 回默认目录（反向迁移），完成后再结束
        save_settings(state, {"runlog": {"dir": ""}})
        _wait(store)
        assert store.root == default_root
        assert (default_root / "proj" / rec.run_id / "meta.json").is_file()
        print("ok: 设置保存触发迁移/回迁 + last_root 状态文件")
    finally:
        RL._STORE = None
        shutil.rmtree(tmp, ignore_errors=True)


def check_last_root_boot() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mig-boot-"))
    try:
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text("llm: {}\n", encoding="utf-8")
        old = tmp / "old-runs"
        new = tmp / "new-runs"
        old.mkdir()
        old_store = RunStore(old)
        rec = old_store.new_run("proj", "T-boot")
        rec.progress("x"); rec.finish("done", "completed")
        (tmp / "runlog-state.json").write_text(json.dumps({
            "schema_version": 1, "last_root": str(old),
            "updated_at": "x"}), encoding="utf-8")

        # 配置显式指向新目录 → 启动自动迁移
        cfg = AppConfig(
            update=UpdateConfig(auto_check=False),
            llm=LLMConfig(api_key="k", base_url="http://localhost:9", model="m"),
            runlog=RunLogConfig(enabled=True, dir=str(new), dir_explicit=True),
            source_path=cfg_file)
        RL._STORE = None
        store = init_run_store(cfg, force=True)
        # 第二次初始化（Scheduler/AppState 各自调用）必须复用同一实例，
        # 不能在迁移进行中重建并重复启动迁移
        assert init_run_store(cfg) is store
        _wait(store)
        assert store.root == new
        assert (new / "proj" / rec.run_id / "meta.json").is_file()

        # 配置整节缺失（旧版本写丢）→ 采用 last_root，不搬回默认
        (tmp / "runlog-state.json").write_text(json.dumps({
            "schema_version": 1, "last_root": str(new),
            "updated_at": "x"}), encoding="utf-8")
        cfg2 = AppConfig(
            update=UpdateConfig(auto_check=False),
            llm=LLMConfig(api_key="k", base_url="http://localhost:9", model="m"),
            runlog=RunLogConfig(enabled=True), source_path=cfg_file)
        RL._STORE = None
        store2 = init_run_store(cfg2, force=True)
        assert store2.root == new, "runlog 节缺失时应保留 last_root"
        assert cfg2.runlog.dir == str(new), "应采用 last_root 回写配置对象，避免下次保存触发反向迁移"
        print("ok: 启动自动迁移 + 配置节缺失时保留 last_root")
    finally:
        RL._STORE = None
        shutil.rmtree(tmp, ignore_errors=True)


def check_retry_after_failure() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mig-retry-"))
    try:
        a, b = tmp / "a", tmp / "b"
        journal = tmp / "runlog-migration.json"
        store = RunStore(a, journal_path=journal)
        rec = store.new_run("proj", "T-retry")
        rec.progress("x"); rec.finish("done", "completed")
        journal.write_text(json.dumps({
            "schema_version": 1, "old_root": str(a), "new_root": str(b),
            "state": "failed", "error": "disk full"}, ensure_ascii=False),
            encoding="utf-8")
        store2 = RunStore(b, journal_path=journal)
        store2._migration.update({"state": "failed",
                                  "old_root": str(a), "new_root": str(b),
                                  "error": "disk full"})
        store2.retry_migration(background=False)
        assert store2.migration_status()["state"] == "done"
        assert (b / "proj" / rec.run_id / "meta.json").is_file()
        assert not journal.exists()
        print("ok: 失败后按 journal 重试成功")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_finished_runs_migrate()
    check_active_run_merge()
    check_same_run_id_merge()
    check_crash_resume()
    check_validation()
    check_settings_dir_roundtrip()
    check_last_root_boot()
    check_retry_after_failure()
    print("\n运行台账目录迁移自检全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
