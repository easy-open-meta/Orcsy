"""S2 配置备份/恢复回归：创建-恢复往返、保留数裁剪、路径穿越拒绝、存储目标读写。

运行：uv run python tests/test_backup.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

from orcsy.backup import (create_backup, prune_backups, push_backup_webdav,
                          restore_backup)
from orcsy.config import load_config, save_config


def check_create_restore_roundtrip() -> None:
    """config.yaml + sessions.json 打包 → 恢复 → 内容逐字节一致。"""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "conf" / "config.yaml"
        src.parent.mkdir(parents=True)
        src.write_text("llm:\n  api_key: sk-x\n  model: m\n", encoding="utf-8")
        (src.parent / "sessions.json").write_text('{"tok": "abc"}', encoding="utf-8")
        dest = Path(td) / "backups"
        archive = create_backup(src, dest, keep_last=5)
        assert archive.exists() and archive.name.startswith("orcsy-config-backup-")

        # 模拟升级失败后的回滚：配置目录被换掉/写坏
        src.write_text("broken: true\n", encoding="utf-8")
        (src.parent / "sessions.json").unlink()
        restored = restore_backup(archive, src)
        assert restored == src
        assert "api_key: sk-x" in src.read_text(encoding="utf-8")
        assert (src.parent / "sessions.json").read_text(encoding="utf-8") == '{"tok": "abc"}'
        # 现配置在恢复前留了兜底副本
        assert (src.parent / "config.yaml.pre-restore").exists()
    print("备份-恢复往返 ✅")


def check_prune() -> None:
    """保留最近 keep_last 份，更旧删除。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for i in range(5):
            f = d / f"orcsy-config-backup-20260101-00000{i}.tar.gz"
            f.write_text(str(i), encoding="utf-8")
        import os
        for i, f in enumerate(sorted(d.iterdir())):
            os.utime(f, (1000 + i, 1000 + i))
        prune_backups(d, keep_last=2)
        left = sorted(p.name for p in d.iterdir())
        assert len(left) == 2, left
        assert left[-1].endswith("4.tar.gz"), left  # 保留最新的两份
    print("保留数裁剪 ✅")


def check_traversal_rejected() -> None:
    """归档成员含路径穿越/绝对路径：拒绝恢复（data filter 双保险）。"""
    import tarfile
    with tempfile.TemporaryDirectory() as td:
        evil = Path(td) / "evil.tar.gz"
        payload = Path(td) / "payload.txt"
        payload.write_text("pwn", encoding="utf-8")
        with tarfile.open(evil, "w:gz") as tf:
            tf.add(payload, arcname="../../etc/passwd")
        target = Path(td) / "conf" / "config.yaml"
        target.parent.mkdir(parents=True)
        target.write_text("ok\n", encoding="utf-8")
        try:
            restore_backup(evil, target)
            raise AssertionError("路径穿越归档应报错")
        except ValueError as e:
            assert "不安全路径" in str(e), e
    print("路径穿越拒绝 ✅")


def check_webdav_push_monkeypatched() -> None:
    """WebDAV 推送：PUT 目标/认证/内容正确（monkeypatch httpx.put 不发真网）。"""
    import orcsy.backup as B
    import orcsy.storage as S

    captured = {}

    def fake_put(url, content=None, headers=None, auth=None, timeout=None):
        captured.update(url=url, content=content, auth=auth)

        class R:
            def raise_for_status(self):
                pass

        return R()

    orig = S.httpx.put
    S.httpx.put = fake_put
    try:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "config.yaml"
            src.write_text("x: 1\n", encoding="utf-8")
            archive = create_backup(src, Path(td) / "bk", keep_last=3)
            url = B.push_backup_webdav(archive, "https://dav.example.com/orcsy/", "u", "p")
            assert url.endswith("/" + archive.name), url
            assert captured["auth"] == ("u", "p")
            assert captured["content"] == archive.read_bytes()
    finally:
        S.httpx.put = orig
    print("WebDAV 推送 ✅")


def check_storage_targets_roundtrip() -> None:
    """storage_targets + backup/archive 分区进配置读写循环（S1 往返盲区守卫同款）。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "c.yaml"
        p.write_text(yaml.safe_dump({
            "llm": {"provider": "openai", "api_key": "sk-x",
                    "base_url": "https://a.example/v1", "model": "m"},
            "storage_targets": {
                "dav": {"type": "webdav", "url": "https://dav.example.com/orcsy/",
                        "username": "u", "password": "p"},
                "s3": {"type": "s3", "endpoint": "https://s3.example.com", "bucket": "bk",
                       "region": "us-east-1", "access_key": "ak", "secret_key": "sk"},
            },
            "backup": {"enabled": True, "target": "dav", "keep_last": 5},
            "archive": {"enabled": True, "target": "s3", "include_runs": True,
                        "keep_last": 3},
        }), encoding="utf-8")
        cfg = load_config(p, require_api_key=False)
        assert cfg.backup.target == "dav" and cfg.backup.keep_last == 5
        assert cfg.storage_targets["dav"].url == "https://dav.example.com/orcsy/"
        assert cfg.storage_targets["dav"].password == "p"
        assert cfg.storage_targets["s3"].bucket == "bk"
        assert cfg.archive.target == "s3" and cfg.archive.include_runs is True
        save_config(cfg)
        again = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert again["backup"]["target"] == "dav"
        assert again["storage_targets"]["dav"]["password"] == "p"
        assert again["archive"]["target"] == "s3"
    print("storage_targets/backup/archive 分区读写往返 ✅")


if __name__ == "__main__":
    check_create_restore_roundtrip()
    check_prune()
    check_traversal_rejected()
    check_webdav_push_monkeypatched()
    check_storage_targets_roundtrip()
    print("test_backup: 全部通过")