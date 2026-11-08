"""测试代码提交与合并开关（commit_and_merge）：默认关闭、全局配置、项目覆盖与工作区执行逻辑。

运行：uv run python tests/test_commit_and_merge.py
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from orcsy.config import (AppConfig, GitConfig, LLMConfig, LoggingConfig,
                          ProjectConfig, WebConfig, load_config, resolve_commit_and_merge)
from orcsy.executor import TaskExecutor
from orcsy.git_ops import Git
from orcsy.scheduler import Scheduler
from orcsy.task_model import Task
from orcsy.web import AppState


def check_default_and_resolution() -> None:
    g = GitConfig()
    assert g.commit_and_merge is False, "全局默认必须关闭"

    p_none = ProjectConfig(name="p1", path=Path("."))
    assert p_none.commit_and_merge is None
    assert resolve_commit_and_merge(g, p_none) is False, "未设置项目级时跟随全局（False）"

    g_on = GitConfig(commit_and_merge=True)
    assert resolve_commit_and_merge(g_on, p_none) is True, "未设置项目级时跟随全局（True）"

    p_override_off = ProjectConfig(name="p2", path=Path("."), commit_and_merge=False)
    assert resolve_commit_and_merge(g_on, p_override_off) is False, "项目级覆盖优先（False）"

    p_override_on = ProjectConfig(name="p3", path=Path("."), commit_and_merge=True)
    assert resolve_commit_and_merge(g, p_override_on) is True, "项目级覆盖优先（True）"
    print("commit_and_merge 默认值与两级继承覆盖解析 ✅")


def check_config_roundtrip() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="cfg-cam-"))
    try:
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text(
            "git:\n  commit_and_merge: true\n"
            "projects:\n"
            "  - name: p1\n    path: .\n    commit_and_merge: false\n"
            "  - name: p2\n    path: .\n",
            encoding="utf-8"
        )
        cfg = load_config(cfg_file, require_api_key=False)
        assert cfg.git.commit_and_merge is True
        assert cfg.projects[0].commit_and_merge is False
        assert cfg.projects[1].commit_and_merge is None

        # 默认不写 git 节时
        cfg_bare = tmp / "bare.yaml"
        cfg_bare.write_text("projects: []\n", encoding="utf-8")
        cfg_default = load_config(cfg_bare, require_api_key=False)
        assert cfg_default.git.commit_and_merge is False, "未配置时默认 False"

        # Web 层 roundtrip
        state = AppState(cfg, Scheduler(cfg), log_path=str(tmp / "log.txt"))
        full = state.full_state()
        assert full["git_cfg"]["commit_and_merge"] is True
        assert full["projects"][0]["commit_and_merge"] is False
        assert full["projects"][1]["commit_and_merge"] is None

        # 修改设置
        state.save_settings({"git": {"commit_and_merge": False}})
        assert state.config.git.commit_and_merge is False

        # 修改项目
        state.save_project({"original_name": "p1", "name": "p1", "path": str(tmp), "commit_and_merge": True})
        p1 = next(p for p in state.config.projects if p.name == "p1")
        assert p1.commit_and_merge is True
        print("config 加载解析与 Web 层状态往返落盘 ✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_executor_workspace_mode() -> None:
    """当 commit_and_merge=False（默认）时，即使是 git 仓库也不建 worktree，改动直接保留在项目目录。"""
    tmp = Path(tempfile.mkdtemp(prefix="exec-cam-"))
    try:
        g = Git(tmp)
        g.init()
        g.ensure_gitignore(["tasks/"])
        g.add_all()
        g.commit("init")

        p = ProjectConfig(name="test", path=tmp, commit_and_merge=False)
        llm = LLMConfig(api_key="k", base_url="http://localhost:9", model="m")
        executor = TaskExecutor(p, llm, commit_and_merge=False)

        assert executor.commit_and_merge is False
        # 不使用 worktree
        assert not (tmp / ".worktree-arranger").exists()
        print("TaskExecutor 默认工作区模式（commit_and_merge=False）无 worktree ✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    check_default_and_resolution()
    check_config_roundtrip()
    check_executor_workspace_mode()
    print("全部 commit_and_merge 机制测试通过 ✨")


if __name__ == "__main__":
    main()
