"""S1 配置格式版本化回归：世代迁移、存盘往返不变量、世代越界拒绝。

守卫的缺陷类：「看板任意保存静默重置用户配置」（update/notify 节、extra_body、
review.llm 写回三次踩坑同源）——只要 save_config 漏写任何已加载字段，本文件的
deep-equal 往返断言立刻红。

运行：uv run python tests/test_config_roundtrip.py
"""

from __future__ import annotations

import copy
import shutil
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

from orcsy.config import CONFIG_VERSION, load_config, migrate_config_raw, save_config

SAMPLES = sorted((Path(__file__).parent / "config_samples").glob("config-*.yaml"))


def _load_sample_copy(sample: Path, td: Path) -> tuple[AppConfig, Path]:
    """样本拷入临时目录再加载：保持样本原文件不被 save_config 改写。"""
    work = td / sample.name
    shutil.copy(sample, work)
    return load_config(work, require_api_key=False), work


def check_legacy_samples_migrate_and_roundtrip() -> None:
    """历史世代样本：加载即迁移盖章；save→load 深度无损；二次 save 字节稳定。"""
    assert len(SAMPLES) >= 2, f"历史样本不足：{SAMPLES}"
    for sample in SAMPLES:
        with tempfile.TemporaryDirectory() as td:
            cfg, work = _load_sample_copy(sample, Path(td))
            first = asdict(cfg)
            save_config(cfg)
            # 迁移盖章：保存后的文件恒为当前世代
            raw = yaml.safe_load(work.read_text(encoding="utf-8"))
            assert raw["config_version"] == CONFIG_VERSION, sample.name
            # 二次加载深度无损：save 前后所有已加载字段一致（除 source_path）
            cfg2 = load_config(work, require_api_key=False)
            a, b = copy.deepcopy(first), asdict(cfg2)
            del a["source_path"], b["source_path"]
            assert a == b, f"{sample.name}: save→load 往返有字段丢失/漂移"
            # 字节级稳定：load→save→load→save 第二次存盘与第一次完全一致
            second = work.read_bytes()
            cfg3 = load_config(work, require_api_key=False)
            save_config(cfg3)
            assert work.read_bytes() == second, f"{sample.name}: 两次存盘字节不一致"
    print(f"历史样本迁移+往返无损 ✅（{len(SAMPLES)} 个样本）")


def check_save_always_stamps_current_version() -> None:
    """存盘恒写当前世代：旧文件被新应用保存一次即完成迁移登记。"""
    raw = {"llm": {"provider": "openai", "api_key": "sk-x",
                   "base_url": "https://api.example.com/v1", "model": "m"}}
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "c.yaml"
        p.write_text(yaml.safe_dump(raw), encoding="utf-8")
        cfg = load_config(p, require_api_key=False)
        assert "config_version" not in yaml.safe_load(p.read_text(encoding="utf-8"))
        save_config(cfg)
        after = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert after["config_version"] == CONFIG_VERSION, after.get("config_version")
    print("存盘恒写当前世代 ✅")


def check_future_version_rejected() -> None:
    """世代超前（新应用写的配置被旧应用加载）：拒绝启动而非静默丢字段。"""
    raw = {"config_version": CONFIG_VERSION + 1, "llm": {"provider": "openai"}}
    try:
        migrate_config_raw(copy.deepcopy(raw), context="config")
        raise AssertionError("世代超前应报错")
    except ValueError as e:
        assert "高于当前应用支持" in str(e), e
    # 幂等：当前世代重复迁移不变
    cur = {"config_version": CONFIG_VERSION}
    again = migrate_config_raw(copy.deepcopy(cur))
    assert again["config_version"] == CONFIG_VERSION
    print("世代越界拒绝 + 幂等 ✅")


def check_all_current_sections_covered() -> None:
    """样本必须覆盖 save_config 写出的全部顶层键——否则 roundtrip 断言有盲区。"""
    covered: set[str] = set()
    for sample in SAMPLES:
        raw = yaml.safe_load(sample.read_text(encoding="utf-8"))
        covered |= set(raw.keys())
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "c.yaml"
        p.write_text(yaml.safe_dump({"llm": {"provider": "openai", "api_key": "sk-x",
                                            "base_url": "https://a.example/v1", "model": "m"}}), encoding="utf-8")
        cfg = load_config(p, require_api_key=False)
        cfg.source_path = p
        save_config(cfg)
        saved_keys = set(yaml.safe_load(p.read_text(encoding="utf-8")).keys()) - {"config_version"}
    missing = saved_keys - covered
    assert not missing, f"样本未覆盖这些顶层键，roundtrip 存在盲区：{sorted(missing)}"
    print(f"样本顶层键覆盖 ✅（{len(saved_keys)} 个分区全在样本中）")


if __name__ == "__main__":
    check_legacy_samples_migrate_and_roundtrip()
    check_save_always_stamps_current_version()
    check_future_version_rejected()
    check_all_current_sections_covered()
    print("test_config_roundtrip: 全部通过")
