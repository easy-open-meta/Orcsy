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
    print("4. 配置：logging.retention_days 解析与 0~365 夹取 ✅")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="orcsy-logrotate-"))
    try:
        check_cleanup(tmp / "c1")
        check_rotate_due(tmp / "c2")
        check_not_due_and_disabled(tmp / "c3")
        check_config_parse(tmp / "c4")
        print("\n日志归档清理自检全部通过 ✅")
        return 0
    finally:
        ls.get_logger("rotate-test").handlers.clear()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
