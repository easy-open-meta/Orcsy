"""配置备份与恢复（1.0 稳定化 S2）。

备份内容：config.yaml + 同目录 sessions.json（登录会话；两者都含敏感信息，
归档一律 chmod 600，WebDAV 目标凭据由用户自管）。技能库/日志/项目仓库体积大
且可再生，不进配置备份；完整数据目录备份交给部署侧（docker compose 卷快照）。

升级（updater.install_update）前自动创建本地备份；升级失败用
`orcsy restore-config <归档>` 回滚。目标支持本地目录与 WebDAV（PUT + Basic
Auth，覆盖主流 NAS/网盘）；S3/OneDrive 待后续接入。
"""

from __future__ import annotations

import pathlib
import shutil
import tarfile
import time

import httpx

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


def push_backup_webdav(archive: pathlib.Path, url: str,
                       username: str = "", password: str = "") -> str:
    """把备份归档 PUT 到 WebDAV 目录（{url}/{文件名}），返回最终 URL。"""
    archive = pathlib.Path(archive)
    dest = url.rstrip("/") + "/" + archive.name
    auth = (username, password) if username else None
    resp = httpx.put(dest, content=archive.read_bytes(),
                     headers={"Content-Type": "application/gzip"},
                     auth=auth, timeout=120)
    resp.raise_for_status()
    return dest
