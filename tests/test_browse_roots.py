"""目录选择器根入口的跨平台自检（Docker/Linux 部署适配）。

历史 bug：根入口无条件拼 Path.home()/Documents 与 Desktop——这两个目录在
Linux/容器里不存在，列出来就是点不进去的死链接；且 POSIX 分支缺失导致
容器部署的目录选择器除了死链外一无所有。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import AppConfig, LLMConfig, SchedulerConfig, UpdateConfig, WebConfig
from orcsy.scheduler import Scheduler
from orcsy.web import AppState, _browse_root_entries

HOME = Path.home()


def test_windows_branch() -> None:
    """Windows 分支：盘符 + 存在的用户目录；不存在的目录不列（无死链）。"""
    dirs = _browse_root_entries("nt", str(HOME))
    if os.name == "nt":
        assert any(d.upper().startswith("C:") for d in dirs), dirs
    # 不存在的用户子目录绝不出现（死链防护）
    assert str(HOME / "Documents") not in dirs or (HOME / "Documents").is_dir()


def test_posix_branch() -> None:
    """POSIX 分支：列出真实存在的常用根目录（含 Docker 约定的 /data /projects）。

    os.path.isdir 打桩模拟容器环境：/ /home /data /projects 存在、/mnt 不存在。"""
    real_isdir = os.path.isdir

    def fake_isdir(p):
        s = str(p)
        if s in ("/", "/home", "/data", "/projects"):
            return True
        if s == "/mnt":
            return False
        return real_isdir(p)

    with patch("os.path.isdir", fake_isdir):
        # home 传 POSIX 风格路径（容器里的真实形态）
        dirs = _browse_root_entries("posix", "/home/orcsy")
    assert "/" in dirs and "/home" in dirs, dirs
    assert "/data" in dirs and "/projects" in dirs, dirs
    assert "/mnt" not in dirs, dirs
    # Windows 盘符绝不出现在 POSIX 分支
    assert not any(len(d) >= 2 and d[1] == ":" for d in dirs), dirs
    # 不存在的用户子目录不列（死链防护）：容器里 Documents/Desktop 不存在
    assert "/home/orcsy/Documents" not in dirs and "/home/orcsy/Desktop" not in dirs, dirs


def test_browse_dir_end_to_end(tmp: Path) -> None:
    """browse_dir 根入口端到端（Windows 本机）：返回结构含 path/parent/dirs。"""
    cfg = AppConfig(
        llm=LLMConfig(api_key="k", base_url="http://localhost:9", model="m"),
        update=UpdateConfig(auto_check=False),
        scheduler=SchedulerConfig(run_once=True),
        web=WebConfig(host="127.0.0.1", port=5218, open_browser=False),
    )
    cfg.source_path = tmp / "config.yaml"
    state = AppState(cfg, Scheduler(cfg))
    r = state.browse_dir({})
    assert r["ok"] and r["path"] == "" and r["parent"] is None
    assert r["dirs"], r
    # 逐级进入一个真实存在的目录：parent 与 dirs 正确
    child = r["dirs"][0]
    r2 = state.browse_dir({"path": child})
    assert r2["ok"], r2
    # 盘根没有上级：parent 回盘符列表根（空串，设计行为）；子目录清单正确
    assert r2["parent"] == "" and r2["dirs"], r2
    print("ok: browse_dir 根入口 + 逐级进入端到端")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="browse-roots-"))
    try:
        test_windows_branch()
        print("ok: Windows 根入口（盘符 + 存在性过滤，无死链）")
        test_posix_branch()
        print("ok: POSIX 根入口（/ /home /data /projects，容器适配）")
        test_browse_dir_end_to_end(tmp)
        print("\n目录选择器根入口跨平台自检通过 ✅")
        return 0
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
