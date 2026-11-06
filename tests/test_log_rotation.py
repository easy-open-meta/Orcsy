"""日志按保留天数归档/清理的自检（rotate_and_cleanup）。

覆盖：
1. 清理：logs/archive/ 与大小滚动备份（orcsy.log.N）里超过保留期的删除，新的保留；
2. 归档：活动日志创建时间超过保留期 → 挪进 logs/archive/ 并重建新日志，
   归档后日志输出不中断（新记录落在新文件里）；
3. 未到期 / retention_days=0：无动作；
4. 配置：logging.retention_days 从 config.yaml 解析并夹取 0~365。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):  # GBK 控制台打不出 ✅ 等字符
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from orcsy import logging_setup as ls
from orcsy.config import load_config

NOW = time.time()
DAY = 86400


def _touch_old(path: Path, days: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("old log line\n", encoding="utf-8")
    os.utime(path, (NOW - days * DAY, NOW - days * DAY))


def check_cleanup(tmp: Path) -> None:
    log = tmp / "logs" / "orcsy.log"
    _touch_old(tmp / "logs" / "archive" / "orcsy-20250101-000000.log", 10)
    _touch_old(tmp / "logs" / "archive" / "orcsy-20260101-000000.log", 1)
    _touch_old(tmp / "logs" / "orcsy.log.3", 10)
    _touch_old(tmp / "logs" / "orcsy.log.4", 1)
    log.write_text("active\n", encoding="utf-8")
    os.utime(log, (NOW - 1 * DAY, NOW - 1 * DAY))

    msg = ls.rotate_and_cleanup(log, 7, now=NOW)
    assert "orcsy-20250101-000000.log" in (msg or ""), msg
    assert not (tmp / "logs" / "archive" / "orcsy-20250101-000000.log").exists()
    assert (tmp / "logs" / "archive" / "orcsy-20260101-000000.log").is_file()
    assert not (tmp / "logs" / "orcsy.log.3").exists(), "过期的大小滚动备份应删除"
    assert (tmp / "logs" / "orcsy.log.4").is_file()
    assert (tmp / "logs" / "orcsy.log").read_text(encoding="utf-8") == "active\n", "活动日志不动"
    print("1. 清理：archive/ 与 orcsy.log.N 超期删除，未超期与活动日志保留 ✅")


def check_rotate_due(tmp: Path) -> None:
    log = tmp / "logs" / "orcsy.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    ls.setup_logging("INFO", str(log))
    ls.get_logger("rotate-test").info("old content marker")
    real_created = ls._created_ts
    ls._created_ts = lambda p: NOW - 10 * DAY  # 模拟 10 天前创建
    try:
        msg = ls.rotate_and_cleanup(log, 7, now=NOW)
    finally:
        ls._created_ts = real_created
    assert msg and "已归档" in msg, msg
    archives = list((tmp / "logs" / "archive").glob("orcsy-*.log"))
    assert len(archives) == 1 and "old content marker" in archives[0].read_text(encoding="utf-8")
    assert log.is_file(), "应重建新的活动日志"

    # 归档后日志输出不中断：新记录必须落在新活动日志里
    ls.get_logger("rotate-test").info("new content marker")
    assert "new content marker" in log.read_text(encoding="utf-8"), "新日志应继续接收输出"
    ls.get_logger("rotate-test").handlers.clear()
    print("2. 归档：超期活动日志挪进 archive/，新日志重建且输出不中断 ✅")


    # 同秒双进程升级冲突：升级换名落位后新旧进程同秒各归档一次，第二次 rename 的
    # 目标与第一次同名（stamp 只到秒）→ Windows 上 Path.rename 目标已存在即
    # WinError 183。修复后按秒去重重试（-2/-3…），归档成功且不覆盖已有归档。
    # rotate 内部的 stamp 用真实当前时间，无法从外部钉死 —— 预置「当前秒」同名
    # 归档文件来构造冲突（mtime 拉到未来，避免被过期清理顺带删掉）。
    stamp_now = time.strftime("%Y%m%d-%H%M%S")
    preset2 = log.parent / "archive" / f"orcsy-{stamp_now}.log"
    preset2.parent.mkdir(parents=True, exist_ok=True)
    preset2.write_text("archived by the other process same second", encoding="utf-8")
    # mtime 须晚于 cleanup 的 deadline（now_arg - 7d = time.time()+3d），
    # 否则预置文件会被过期清理顺带删掉
    future = time.time() + 5 * DAY
    os.utime(preset2, (future, future))
    ls.setup_logging("INFO", str(log))
    ls.get_logger("rotate-test").info("third process boot")
    real_created = ls._created_ts
    try:
        ls._created_ts = lambda p: NOW - 10 * DAY
        msg3 = ls.rotate_and_cleanup(log, 7, now=time.time() + 10 * DAY)
    finally:
        ls._created_ts = real_created
    assert "WinError 183" not in (msg3 or ""), f"同秒冲突仍报 183：{msg3}"
    assert "-2.log" in (msg3 or ""), f"应归档到 -2 去重后缀：{msg3}"
    assert preset2.read_text(encoding="utf-8") == "archived by the other process same second",         "已有归档不得被覆盖"
    assert log.is_file(), "活动日志应重建"
    ls.get_logger("rotate-test").handlers.clear()
    print("2b. 同秒双进程归档冲突：去重后缀落位，已有归档完好，无 WinError 183 ✅")


def check_not_due_and_disabled(tmp: Path) -> None:
    log = tmp / "logs" / "orcsy.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("recent\n", encoding="utf-8")
    os.utime(log, (NOW - 1 * DAY, NOW - 1 * DAY))
    assert ls.rotate_and_cleanup(log, 7, now=NOW) is None, "未到期应无动作"
    assert log.read_text(encoding="utf-8") == "recent\n"
    assert ls.rotate_and_cleanup(log, 0, now=NOW) is None, "retention=0 应为关闭"
    print("3. 未到期与 retention=0：无任何动作 ✅")


def check_config_parse(tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    cfg = tmp / "config.yaml"
    cfg.write_text(
        "llm:\n  api_key: sk-test\n"
        "logging:\n  retention_days: 3\n", encoding="utf-8")
    assert load_config(cfg, require_api_key=False).logging.retention_days == 3
    cfg.write_text(
        "llm:\n  api_key: sk-test\n"
        "logging:\n  retention_days: 9999\n", encoding="utf-8")
    assert load_config(cfg, require_api_key=False).logging.retention_days == 365, "应夹取到 365"
    # max_total_mb：默认 200；0 = 不限制；超大夹取到 100000
    cfg.write_text(
        "llm:\n  api_key: sk-test\n"
        "logging:\n  retention_days: 3\n  max_total_mb: 0\n", encoding="utf-8")
    assert load_config(cfg, require_api_key=False).logging.max_total_mb == 0
    cfg.write_text(
        "llm:\n  api_key: sk-test\n"
        "logging:\n  max_total_mb: 99999999\n", encoding="utf-8")
    assert load_config(cfg, require_api_key=False).logging.max_total_mb == 100000
    print("4. 配置：retention_days / max_total_mb 解析与夹取 ✅")


def _seed_logs(tmp: Path) -> Path:
    """构造 4 个可清理日志（mtime 从旧到新）+ 活动日志 + 非日志文件。"""
    MB = 1024 * 1024
    log = tmp / "logs" / "orcsy.log"
    for rel, days, size in (
            ("logs/orcsy.log.3", 1.0, 0.6),
            ("logs/archive/orcsy-20250101-000000.log", 0.8, 0.6),
            ("logs/archive/orcsy-20260101-000000.log", 0.5, 0.6),
            ("logs/orcsy.log.4", 0.3, 0.6)):
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * int(size * MB))
        os.utime(p, (NOW - days * DAY,) * 2)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"x" * int(0.5 * MB))  # 活动日志 0.5MB
    os.utime(log, (NOW - 0.2 * DAY,) * 2)
    (tmp / "logs" / "install.log").write_bytes(b"x" * int(3 * MB))  # 非日志体系
    return log


def check_size_partial(tmp: Path) -> None:
    """上限 2MB：删最旧的两个回到限额内即停，较新的保留。"""
    log = _seed_logs(tmp)
    msg = ls.rotate_and_cleanup(log, 0, max_total_mb=2, now=NOW)
    assert not (tmp / "logs" / "orcsy.log.3").exists(), "最旧的应先删"
    assert not (tmp / "logs" / "archive" / "orcsy-20250101-000000.log").exists()
    assert (tmp / "logs" / "archive" / "orcsy-20260101-000000.log").is_file()
    assert (tmp / "logs" / "orcsy.log.4").is_file()
    assert log.is_file(), "活动日志不可删"
    assert (tmp / "logs" / "install.log").is_file(), "非日志体系不计入也不可删"
    assert "按大小清理完成" in (msg or ""), msg
    print("5. 按大小清理：超限从最旧删起、回到限额即停，活动日志与 install.log 不动 ✅")


def check_size_full_and_off(tmp: Path) -> None:
    """上限 1MB：候选全删干净；上限 0：一个都不动（两开关独立）。"""
    log = _seed_logs(tmp)
    msg = ls.rotate_and_cleanup(log, 0, max_total_mb=1, now=NOW)
    for rel in ("logs/orcsy.log.3", "logs/orcsy.log.4",
                "logs/archive/orcsy-20250101-000000.log",
                "logs/archive/orcsy-20260101-000000.log"):
        assert not (tmp / rel).exists(), rel
    assert log.is_file() and "按大小清理完成" in (msg or "")

    log2 = _seed_logs(tmp / "off")
    assert ls.rotate_and_cleanup(log2, 0, max_total_mb=0, now=NOW) is None, \
        "max_total_mb=0 应为不限制"
    assert (tmp / "off" / "logs" / "orcsy.log.3").is_file()
    print("6. 按大小清理：限额足够小时删净候选；=0 完全关闭 ✅")


def check_web_roundtrip(tmp: Path) -> None:
    """Web 层：save_settings 校验/落盘 + full_state 回显。"""
    from orcsy.config import AppConfig, LLMConfig
    from orcsy.scheduler import Scheduler
    from orcsy.web import AppState
    tmp.mkdir(parents=True, exist_ok=True)
    cfg_file = tmp / "config.yaml"
    cfg_file.write_text("llm: {}\n", encoding="utf-8")
    cfg = AppConfig(llm=LLMConfig(api_key="k", base_url="http://localhost:9",
                                  model="m"), source_path=cfg_file)
    st = AppState(cfg, Scheduler(cfg))
    st.save_settings({"logging": {"retention_days": 14, "max_total_mb": 50}})
    assert st.config.logging.max_total_mb == 50
    assert st.full_state()["logging"]["max_total_mb"] == 50
    from orcsy.config import load_config
    disk = load_config(cfg_file, require_api_key=False)
    assert disk.logging.max_total_mb == 50
    # 0 = 不限制是合法显式值；负数拒绝
    st.save_settings({"logging": {"retention_days": 14, "max_total_mb": 0}})
    assert st.config.logging.max_total_mb == 0
    try:
        st.save_settings({"logging": {"retention_days": 14, "max_total_mb": -5}})
        raise AssertionError("应拒绝负数")
    except ValueError:
        pass
    print("7. Web 层：max_total_mb 校验/落盘/回显，0 = 不限制、负数拒绝 ✅")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="orcsy-logrotate-"))
    try:
        check_cleanup(tmp / "c1")
        check_rotate_due(tmp / "c2")
        check_not_due_and_disabled(tmp / "c3")
        check_config_parse(tmp / "c4")
        check_size_partial(tmp / "c5")
        check_size_full_and_off(tmp / "c6")
        check_web_roundtrip(tmp / "c7")
        print("\n日志归档清理自检全部通过 ✅")
        return 0
    finally:
        ls.get_logger("rotate-test").handlers.clear()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
