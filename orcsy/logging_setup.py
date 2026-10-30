"""统一日志：控制台 + 滚动文件 + 按保留天数归档/清理。"""

from __future__ import annotations

import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FMT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"

# RotatingFileHandler 的大小滚动上限（与保留天数的时间归档互为兜底）
_MAX_LOG_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5
_ARCHIVE_DIR = "archive"


def setup_logging(level: str = "INFO", file: str = "logs/orcsy.log") -> logging.Logger:
    logger = logging.getLogger("orcsy")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    console = logging.StreamHandler()
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(logging.Formatter(_FMT))
    logger.addHandler(console)

    log_path = Path(file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path, maxBytes=_MAX_LOG_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FMT))
    logger.addHandler(file_handler)

    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"orcsy.{name}")


def anchored_log_path(config) -> Path:
    """config.logging.file 相对路径锚定到配置文件所在目录（与 cmd_web 同规则）。"""
    p = Path(config.logging.file)
    if not p.is_absolute() and config.source_path:
        p = config.source_path.parent / p
    return p


def _created_ts(path: Path) -> float:
    """文件创建时间：Windows 的 st_ctime 即创建时刻；其它平台退化用 mtime。"""
    st = path.stat()
    return st.st_ctime if sys.platform == "win32" else st.st_mtime


def rotate_and_cleanup(log_file: str | Path, retention_days: int,
                       now: float | None = None) -> str | None:
    """按保留天数归档活动日志并清理过期归档，返回给人看的说明（无动作时 None）。

    - 归档：活动日志的创建时间超过 retention_days 天 → 挪进 logs/archive/ 并
      重新打开新日志。Windows 不能重命名打开中的文件，因此先摘下文件句柄、
      改名、再按原级别/格式重建；另有 5MB×5 的大小滚动兜底，两者互不影响。
    - 清理：logs/archive/ 与 logs/ 下 mtime 超过保留期的旧日志删除
      （含大小滚动产生的 orcsy.log.N）。
    - retention_days <= 0 表示关闭自动归档与清理。
    """
    retention_days = int(retention_days)
    if retention_days <= 0:
        return None
    log_path = Path(log_file)
    now = time.time() if now is None else now
    notes: list[str] = []

    # ---- 归档：活动日志写满保留期就挪走 ----
    if log_path.is_file() and log_path.stat().st_size > 0:
        age_days = (now - _created_ts(log_path)) / 86400
        if age_days >= retention_days:
            logger = logging.getLogger("orcsy")
            swapped = [h for h in logger.handlers
                       if isinstance(h, logging.FileHandler)
                       and Path(getattr(h, "baseFilename", "")) == log_path.resolve()]
            for h in swapped:
                logger.removeHandler(h)
                h.close()
            archive_dir = log_path.parent / _ARCHIVE_DIR
            archive_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dst = archive_dir / f"{log_path.stem}-{stamp}{log_path.suffix}"
            try:
                log_path.rename(dst)
                notes.append(f"已归档 {log_path.name} → archive/{dst.name}")
            except OSError as e:
                notes.append(f"归档失败（保留原文件）：{e}")
            # 为被摘掉的文件句柄重建：无论改名成败都指向原活动路径——
            # 成功则此处是全新日志；失败则原文件还在，续写不丢输出
            for h in swapped:
                nh = RotatingFileHandler(log_path,
                                         maxBytes=_MAX_LOG_BYTES,
                                         backupCount=_BACKUP_COUNT, encoding="utf-8")
                nh.setLevel(h.level)
                nh.setFormatter(h.formatter)
                logger.addHandler(nh)

    # ---- 清理：超过保留期的旧日志（归档 + 大小滚动备份） ----
    deadline = now - retention_days * 86400
    candidates = list((log_path.parent / _ARCHIVE_DIR).glob(f"{log_path.stem}-*{log_path.suffix}"))
    candidates += list(log_path.parent.glob(f"{log_path.name}.*"))  # 大小滚动备份 orcsy.log.N
    candidates += list(log_path.parent.glob(f"{log_path.stem}-*{log_path.suffix}"))
    for f in candidates:
        try:
            if f.is_file() and f.stat().st_mtime < deadline:
                f.unlink()
                notes.append(f"已清理过期日志 {f.name}")
        except OSError:
            continue
    return "；".join(notes) or None
