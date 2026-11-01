"""定时拉取 Issues 自检（离线）：normalize_repo 的 auto_pull_interval 清洗、
调度器 issue-pull 循环的到期判定/去重间隔/异常隔离。

运行：uv run python tests/test_issue_autopull.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

from orcsy.config import (AppConfig, LLMConfig, LoggingConfig, ProjectConfig,
                          SchedulerConfig, WebConfig, load_config)
from orcsy.issues import normalize_repo


def check_normalize_interval() -> None:
    # 0/缺省 = 关闭：不写入输出字典（与手动拉取行为一致）
    base = {"url": "https://github.com/o/r", "token": " t "}
    assert normalize_repo(dict(base)) == {"url": "https://github.com/o/r",
                                          "provider": "github", "token": "t"}
    assert normalize_repo({**base, "auto_pull_interval": 0}) == \
        {"url": "https://github.com/o/r", "provider": "github", "token": "t"}
    # 正常值保留；过小值钳制到最小 60
    r = normalize_repo({**base, "auto_pull_interval": 600})
    assert r["auto_pull_interval"] == 600
    assert normalize_repo({**base, "auto_pull_interval": 5})["auto_pull_interval"] == 60
    # 字符串数字容忍；非法值（非数字/None）视为关闭
    assert normalize_repo({**base, "auto_pull_interval": "300"})["auto_pull_interval"] == 300
    assert normalize_repo({**base, "auto_pull_interval": "abc"}) == \
        {"url": "https://github.com/o/r", "provider": "github", "token": "t"}
    assert normalize_repo({**base, "auto_pull_interval": None})["token"] == "t"
    print("normalize_repo auto_pull_interval 清洗（0/钳制/容忍非法值）✅")


def check_config_roundtrip() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="orcsy-autopull-cfg-"))
    try:
        proj = tmp / "proj"
        proj.mkdir()
        (tmp / "config.yaml").write_text(
            "llm:\n  api_key: sk-x\n  base_url: https://api.deepseek.com/v1\n"
            "projects:\n"
            "  - name: auto\n"
            f"    path: {proj.as_posix()}\n"
            "    repo:\n"
            "      url: https://github.com/o/r\n"
            "      token: tok-1\n"
            "      auto_pull_interval: 300\n",
            encoding="utf-8")
        cfg = load_config(tmp / "config.yaml")
        assert cfg.projects[0].repo is not None
        assert cfg.projects[0].repo.get("auto_pull_interval") == 300
        print("config.yaml repo.auto_pull_interval 读回 ✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _mini_config(tmp: Path, *, interval: int | None, name: str = "auto") -> AppConfig:
    proj = tmp / name
    proj.mkdir(exist_ok=True)
    repo: dict | None = None
    if interval is not None:
        repo = {"url": "https://github.com/o/r", "provider": "github", "token": "t",
                **({"auto_pull_interval": interval} if interval else {})}
    return AppConfig(
        llm=LLMConfig(api_key="sk-x", base_url="https://api.deepseek.com/v1"),
        scheduler=SchedulerConfig(), logging=LoggingConfig(), web=WebConfig(),
        projects=[ProjectConfig(name=name, path=proj, repo=repo)],
        source_path=tmp / "config.yaml",
    )


def check_pull_loop() -> None:
    """issue-pull 循环：到期判定、按间隔重拉、失败隔离、关闭项目跳过。"""
    from orcsy import scheduler as sched_mod
    from orcsy.scheduler import Scheduler

    tmp = Path(tempfile.mkdtemp(prefix="orcsy-autopull-loop-"))
    calls: list[tuple[str, int]] = []

    def fake_sync(name: str, repo: dict, tasks_dir: Path) -> dict:
        calls.append((name, int(repo.get("auto_pull_interval") or 0)))
        if repo.get("token") == "boom":
            raise RuntimeError("网络炸了")
        return {"imported": 1, "skipped": 0, "total": 1, "project": name}

    orig_sync = sched_mod.sync_issues
    sched_mod.sync_issues = fake_sync
    try:
        cfg = _mini_config(tmp, interval=60)
        s = Scheduler(cfg)
        # 未到期不该拉（timer 未登记 → 首次见到立即拉）
        s._pull_due_issues()
        assert len(calls) == 1, calls
        assert s.issue_pull_status["auto"] == {
            "interval": 60, "imported": 1, "skipped": 0, "total": 1, "last_error": ""}
        # 刚拉完：60 秒内再醒来不应重拉
        s._pull_due_issues()
        assert len(calls) == 1
        # 强制到期：重拉
        s._issue_timers["auto"] = time.monotonic() - 1
        s._pull_due_issues()
        assert len(calls) == 2
        # 拉取失败：状态记错误、循环不中断、timer 照常推进（60s 内不重试打爆 API）
        s.agents[0].project.repo["token"] = "boom"
        s._issue_timers["auto"] = time.monotonic() - 1
        s._pull_due_issues()
        assert "boom" not in str(s.issue_pull_status["auto"].get("last_error", "")) or \
            s.issue_pull_status["auto"]["last_error"], s.issue_pull_status
        assert s.issue_pull_status["auto"]["last_error"]
        saved_timer = s._issue_timers["auto"]
        assert saved_timer > time.monotonic() + 55
        # 未绑定 repo / 无间隔：完全不进循环
        cfg2 = _mini_config(tmp, interval=0, name="manual")
        s2 = Scheduler(cfg2)
        s2._pull_due_issues()
        assert "manual" not in s2.issue_pull_status
        # stop 应同时停掉 issue 循环 event
        s.stop()
        assert s._issue_stop_event.is_set()
        print("issue-pull 循环（到期判定/间隔/失败隔离/关闭跳过）✅")
    finally:
        sched_mod.sync_issues = orig_sync
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_normalize_interval()
    check_config_roundtrip()
    check_pull_loop()
    print("\n全部断言通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
