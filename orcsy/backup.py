"""配置备份与恢复（1.0 稳定化 S2）。

备份内容：config.yaml + 同目录 sessions.json（登录会话；两者都含敏感信息，
归档一律 chmod 600）。技能库/日志/项目仓库体积大且可再生，不进配置备份；
完整数据目录备份交给部署侧（docker compose 卷快照）。

升级（updater.install_update）前自动创建本地备份；升级失败用
`orcsy restore-config <归档>` 回滚。目标支持本地目录、WebDAV 与 S3 兼容空间
（经 `orcsy.storage` 统一 provider，远程请求过 egress 白名单）。
"""

from __future__ import annotations

import calendar
import json
import pathlib
import shutil
import tarfile
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:  # 仅类型标注，避免运行时重依赖
    from .storage import StorageProvider

BACKUP_GLOB = "orcsy-config-backup-*.tar.gz"


def collect_source_files(config_path: pathlib.Path) -> list[pathlib.Path]:
    """config.yaml + 同目录 sessions.json（存在才带上）。"""
    config_path = pathlib.Path(config_path)
    out = [config_path]
    sessions = config_path.parent / "sessions.json"
    if sessions.exists():
        out.append(sessions)
    return out


def create_backup(config_path: pathlib.Path, dest_dir: pathlib.Path,
                  keep_last: int = 10) -> pathlib.Path:
    """把配置要点打包为 tar.gz 落到 dest_dir（含保留数裁剪），返回归档路径。"""
    config_path = pathlib.Path(config_path)
    dest_dir = pathlib.Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    files = collect_source_files(config_path)
    dest = dest_dir / f"orcsy-config-backup-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    with tarfile.open(dest, "w:gz") as tf:
        for f in files:
            tf.add(f, arcname=f.name)
    try:
        dest.chmod(0o600)  # 归档含登录会话/API Key，落盘即最小权限
    except OSError:
        pass
    prune_backups(dest_dir, keep_last)
    return dest


def prune_backups(dest_dir: pathlib.Path, keep_last: int) -> None:
    """按文件名时间戳外的修改时间排序保留最近 keep_last 份，其余删除。"""
    if keep_last <= 0:
        keep_last = 1
    dest_dir = pathlib.Path(dest_dir)
    backups = sorted(dest_dir.glob(BACKUP_GLOB), key=lambda p: p.stat().st_mtime)
    for old in backups[:-keep_last] if len(backups) > keep_last else []:
        try:
            old.unlink()
        except OSError:
            pass


def restore_backup(archive: pathlib.Path, config_path: pathlib.Path) -> pathlib.Path:
    """从备份恢复 config.yaml（+sessions.json）；现配置先留 .pre-restore 兜底。

    归档成员必须为纯文件名（防路径穿越）；缺 config.yaml 的归档直接报错。
    """
    archive, config_path = pathlib.Path(archive), pathlib.Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tf:
        names = tf.getnames()
        for n in names:
            pp = pathlib.PurePosixPath(n)
            if pp.is_absolute() or ".." in pp.parts:
                raise ValueError(f"备份包含不安全路径成员：{n}")
        if "config.yaml" not in names:
            raise ValueError(f"备份里没有 config.yaml（包含：{names[:6]}）")
        if config_path.exists():
            shutil.copy2(config_path, config_path.with_name("config.yaml.pre-restore"))
        tf.extract("config.yaml", config_path.parent, filter="data")
        if "sessions.json" in names:
            tf.extract("sessions.json", config_path.parent, filter="data")
    return config_path


def push_backup_archive(archive: pathlib.Path, provider: "StorageProvider",
                        *, keep_last: int | None = None) -> str:
    """把备份归档推送到 provider；对象键用归档文件名，返回 provider 句柄。

    keep_last 非空时，推送后按「名字字典序 = 时间序」裁剪远端旧归档（失败静默）。
    """
    archive = pathlib.Path(archive)
    handle = provider.put(archive.name, archive.read_bytes(),
                          content_type="application/gzip")
    if keep_last is not None:
        from .storage import prune_remote
        prune_remote(provider, prefix="orcsy-config-backup-", keep_last=keep_last)
    return handle


def download_backup(provider: "StorageProvider", key: str,
                    dest: pathlib.Path) -> pathlib.Path:
    """从 provider 下载归档到本地 dest（供 restore-config 使用）。"""
    dest = pathlib.Path(dest)
    dest.write_bytes(provider.get(key))
    try:
        dest.chmod(0o600)
    except OSError:
        pass
    return dest


def push_backup_webdav(archive: pathlib.Path, url: str,
                       username: str = "", password: str = "") -> str:
    """兼容旧签名：直接用 WebDAV 目标推一份（等价于 provider 分派到 webdav）。"""
    from .storage import WebDAVProvider
    return push_backup_archive(
        archive, WebDAVProvider(url, username, password))


def do_backup(cfg) -> dict:
    """执行一次配置备份（本地 + 按 backup.target 推送），返回 {path, handle, kind}。

    异常向上抛（ValueError 配置错 / httpx.HTTPError / StorageError / EgressDenied）。
    """
    from .config import resolve_storage_target
    from .storage import LocalProvider, build_provider

    if cfg.source_path is None:
        raise ValueError("配置来源未知，无法备份")
    base = cfg.source_path.parent
    target = resolve_storage_target(cfg.storage_targets, cfg.backup.target)
    provider = build_provider(target, base_dir=base)
    dest_dir = provider.root if isinstance(provider, LocalProvider) else base / "backups"
    archive = create_backup(cfg.source_path, dest_dir, keep_last=cfg.backup.keep_last)
    handle = str(archive)
    if not isinstance(provider, LocalProvider):
        handle = push_backup_archive(archive, provider, keep_last=cfg.backup.keep_last)
    return {"path": str(archive), "handle": handle, "kind": provider.kind}


# ---- 定时备份：间隔计算与上次运行时间持久化 ----

def _schedule_state_path(data_dir: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(data_dir) / "state" / "schedule_backup.json"


def load_last_backup(data_dir: pathlib.Path) -> float | None:
    """上次（定时）备份的 unix 时间戳；无记录/损坏返回 None（视为「该跑一次」）。"""
    try:
        raw = json.loads(_schedule_state_path(data_dir).read_text(encoding="utf-8"))
        ts = float(raw.get("last_run") or 0)
        return ts or None
    except Exception:  # noqa: BLE001
        return None


def mark_backup_run(data_dir: pathlib.Path, ts: float) -> None:
    p = _schedule_state_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"last_run": ts}), encoding="utf-8")


def _add_months(dt: datetime, n: int) -> datetime:
    month = dt.month - 1 + n
    year = dt.year + month // 12
    month = month % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def schedule_due(last_ts: float | None, now: float, interval: int, unit: str) -> bool:
    """判断定时备份是否到期；从未跑过 → True。"""
    if not last_ts:
        return True
    if interval <= 0:
        interval = 1
    dt = datetime.fromtimestamp(last_ts)
    if unit == "hours":
        nxt = dt + timedelta(hours=interval)
    elif unit == "weeks":
        nxt = dt + timedelta(weeks=interval)
    elif unit == "months":
        nxt = _add_months(dt, interval)
    else:  # days 兜底
        nxt = dt + timedelta(days=interval)
    return now >= nxt.timestamp()