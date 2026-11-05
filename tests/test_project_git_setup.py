"""新建/导入项目时的 git 就绪自检：非仓库自动 init，仓库自动补 .gitignore。

运行：uv run python tests/test_project_git_setup.py

覆盖：
1. 非 git 项目目录 → 建项后 .git 存在、.gitignore 含 tasks/、AGENT.md、worktree；
2. 已是 git 仓库 → 不重复 init，用户 .gitignore 原有内容保留，仅追加缺失条目；
3. 自定义 tasks_dir → 忽略的是自定义目录而非写死的 tasks/；
4. 修改已有项目（original_name 传入）→ 不触发任何 git 初始化/改写；
5. ensure_gitignore 幂等 → 重复调用不产生重复行。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import AppConfig, LLMConfig, LoggingConfig, WebConfig
from orcsy.git_ops import Git
from orcsy.scheduler import Scheduler
from orcsy.web import AppState

WORKTREE = ".worktree-arranger"


def _state(tmp: Path) -> AppState:
    cfg_file = tmp / "config.yaml"
    cfg_file.write_text("llm: {}\n", encoding="utf-8")
    cfg = AppConfig(
        llm=LLMConfig(api_key="sk-test", base_url="http://localhost:9", model="m"),
        web=WebConfig(host="127.0.0.1", auth_password=""),
        logging=LoggingConfig(file=str(tmp / "orcsy.log"), level="INFO"),
        projects=[],
        source_path=cfg_file,
    )
    return AppState(cfg, Scheduler(cfg), log_path=str(tmp / "orcsy.log"))


def _gitignore_lines(root: Path) -> list[str]:
    f = root / ".gitignore"
    return [x.strip() for x in f.read_text(encoding="utf-8").splitlines()]


def check_dirty_dir_gets_git() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="git-setup-"))
    try:
        root = tmp / "plain"
        root.mkdir()
        state = _state(tmp)
        r = state.save_project({"name": "plain", "path": str(root)})
        assert r["ok"] is True
        assert (root / ".git").exists(), "非 git 项目应自动 git init"
        lines = _gitignore_lines(root)
        for want in ("tasks/", "AGENT.md", f"{WORKTREE}/"):
            assert want in lines, f".gitignore 缺少 {want}：{lines}"
        assert any("已初始化 git 仓库" in n for n in r["git"]), r["git"]
        print("非 git 目录 → 自动 init + 忽略条目 ✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_existing_repo_keeps_content() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="git-setup-"))
    try:
        root = tmp / "repo"
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True,
                       capture_output=True)
        (root / ".gitignore").write_text(
            "# 用户自己的规则\nnode_modules/\ndist/\n", encoding="utf-8")
        state = _state(tmp)
        r = state.save_project({"name": "repo", "path": str(root)})
        text = (root / ".gitignore").read_text(encoding="utf-8")
        # 用户原有内容原样保留
        assert "# 用户自己的规则" in text
        assert "node_modules/" in text and "dist/" in text
        assert "tasks/" in text and "AGENT.md" in text
        assert not any("已初始化 git 仓库" in n for n in r["git"]), r["git"]
        print("已有仓库 → 只追加 .gitignore，不动原内容 ✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_custom_tasks_dir() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="git-setup-"))
    try:
        root = tmp / "custom"
        root.mkdir()
        state = _state(tmp)
        state.save_project({"name": "custom", "path": str(root),
                            "tasks_dir": "backlog", "guide_file": "GUIDE.md"})
        lines = _gitignore_lines(root)
        assert "backlog/" in lines, lines
        assert "GUIDE.md" in lines, lines
        assert "tasks/" not in lines, "自定义任务目录时不应写死 tasks/"
        print("自定义 tasks_dir / guide_file → 按实际名忽略 ✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_edit_does_not_touch_git() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="git-setup-"))
    try:
        root = tmp / "edit-me"
        root.mkdir()
        state = _state(tmp)
        # 先建一个非 git 项目（此时会自动 init）；删掉 .git 模拟用户事后清理
        state.save_project({"name": "edit-me", "path": str(root)})
        shutil.rmtree(root / ".git")
        (root / ".gitignore").unlink()
        state.save_project({"name": "edit-me", "path": str(root),
                            "original_name": "edit-me"})
        assert not (root / ".git").exists(), "修改已有项目不应自动 git init"
        assert not (root / ".gitignore").exists(), "修改已有项目不应改写 .gitignore"
        print("修改已有项目 → 不触发 git 初始化/忽略改写 ✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_ensure_gitignore_idempotent() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="git-setup-"))
    try:
        root = tmp / "idem"
        root.mkdir()
        git = Git(root)
        git.init()
        first = git.ensure_gitignore(["tasks/", "AGENT.md"])
        assert first == ["tasks/", "AGENT.md"]
        before = (root / ".gitignore").read_text(encoding="utf-8")
        second = git.ensure_gitignore(["tasks/", "AGENT.md", ".worktree-arranger/"])
        after = (root / ".gitignore").read_text(encoding="utf-8")
        assert second == [".worktree-arranger/"], second
        assert after.count("tasks/") == 1 and "AGENT.md" in after
        assert after.startswith(before.rstrip("\n")), "已有内容不应被重写"
        print("ensure_gitignore 幂等、只追加缺失条目 ✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    check_dirty_dir_gets_git()
    check_existing_repo_keeps_content()
    check_custom_tasks_dir()
    check_edit_does_not_touch_git()
    check_ensure_gitignore_idempotent()
    print("全部断言通过 ✅")


if __name__ == "__main__":
    main()
