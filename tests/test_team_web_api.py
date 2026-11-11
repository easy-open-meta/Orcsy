"""M3 Web API 自检：full_state 状态暴露、agent_profiles 保存与三态脱敏、project.team 往返落盘、任务依赖与指派校验、人工提交与 Git 初始化接口。"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import AppConfig, LLMConfig, ProjectConfig, load_config
from orcsy.git_ops import Git
from orcsy.scheduler import Scheduler
from orcsy.web import AppState


def _make_env() -> tuple[Path, AppState]:
    tmp = Path(tempfile.mkdtemp(prefix="web-team-"))
    proj_dir = tmp / "my_project"
    proj_dir.mkdir(parents=True, exist_ok=True)
    g = Git(proj_dir)
    g.init()
    g.run("config", "user.email", "t@t.com")
    g.run("config", "user.name", "tester")
    (proj_dir / "README.md").write_text("# Test\n", encoding="utf-8")
    (proj_dir / "AGENT.md").write_text("# Guide\n", encoding="utf-8")
    g.ensure_gitignore(["tasks/", "AGENT.md", ".worktree-arranger/"])
    g.add_all()
    g.commit("chore: init")

    cfg_file = tmp / "config.yaml"
    cfg_file.write_text(
        f"""
llm:
  provider: openai
  api_key: sk-top-secret
  model: gpt-global
projects:
  - name: my_project
    path: {proj_dir.as_posix()}
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file, require_api_key=False)
    sched = Scheduler(cfg)
    state = AppState(cfg, sched)
    return tmp, state


def check_full_state_and_profiles_saving() -> None:
    tmp, state = _make_env()
    try:
        # 1. 验证 full_state 基础字段曝光
        fs = state.full_state()
        assert "built_in_roles" in fs and "pm" in fs["built_in_roles"]
        assert "agent_profiles" in fs
        p0 = fs["projects"][0]
        assert "team" in p0 and "team_enabled" in p0 and "pending_git_changes" in p0
        assert p0["pending_git_changes"]["is_git"] is True

        # 2. 保存 agent_profiles（带 H2 密钥）
        res_prof = state.save_settings({
            "agent_profiles": {
                "pm_custom": {
                    "name": "架构师 PM",
                    "role": "pm",
                    "llm": {"provider": "openai", "model": "o3-mini", "api_key": "sk-pm-secret"},
                    "max_turns": 18,
                },
                "be_custom": {
                    "name": "后端开发",
                    "role": "backend",
                    "llm": {"provider": "openai", "model": "deepseek-chat", "api_key": "sk-be-secret"},
                },
            }
        })
        assert res_prof["ok"]
        fs2 = state.full_state()
        assert "pm_custom" in fs2["agent_profiles"]
        pm_prof = fs2["agent_profiles"]["pm_custom"]
        assert pm_prof["llm"]["api_key_configured"] is True
        assert "secret" not in pm_prof["llm"]["api_key_masked"], "密钥必须脱敏"

        # 3. 三态更新：未带 api_key 时保持原值不被清空
        state.save_settings({
            "agent_profiles": {
                "pm_custom": {
                    "name": "架构师 PM (改名)",
                    "role": "pm",
                    "llm": {"model": "o3-mini-v2"},
                },
                "be_custom": {
                    "name": "后端开发",
                    "role": "backend",
                },
            }
        })
        reloaded = load_config(state.config.source_path, require_api_key=False)
        assert reloaded.agent_profiles["pm_custom"].llm["api_key"] == "sk-pm-secret", "三态保持密钥成功"
        assert reloaded.agent_profiles["pm_custom"].llm["model"] == "o3-mini-v2"
        print("ok: full_state 角色预设曝光与 H2 三态保存落盘")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_project_team_save_and_task_crud() -> None:
    tmp, state = _make_env()
    try:
        # 预设池
        state.save_settings({
            "agent_profiles": {
                "pm": {"name": "PM", "role": "pm"},
                "backend": {"name": "后端", "role": "backend", "llm": {"api_key": "sk-be"}},
                "qa": {"name": "测试", "role": "qa"},
            }
        })
        proj_name = "my_project"

        # 1. 保存项目的小组配置
        res_team = state.save_project({
            "original_name": proj_name,
            "name": proj_name,
            "path": state.config.projects[0].path.as_posix(),
            "team": {
                "enabled": True,
                "execution_mode": "parallel_worktree",
                "max_qa_rounds": 4,
                "max_split_depth": 2,
                "chain_within_cycle": True,
                "fallback_role": "backend",
                "members": [
                    {"id": "pm", "profile": "pm"},
                    {"id": "backend", "profile": "backend"},
                    {"id": "qa", "profile": "qa"},
                ],
            },
        })
        assert res_team["ok"]
        fs = state.full_state()
        p0 = fs["projects"][0]
        assert p0["team_enabled"] is True
        assert p0["team"]["max_qa_rounds"] == 4
        assert len(p0["team_members_effective"]) == 3
        assert p0["worktree_isolation_effective"] is True

        # 2. 任务创建、指派与依赖校验
        # 错误：指派不存在的角色
        try:
            state.create_or_update_task({"project": proj_name, "title": "t1", "assignee": "ios_dev"})
            raise AssertionError("应拒绝未知角色")
        except ValueError as e:
            assert "不在本项目小组内" in str(e)

        # 正常创建任务 1
        t1 = state.create_or_update_task({"project": proj_name, "title": "后端接口", "assignee": "backend"})
        assert t1["ok"]
        tid1 = t1["id"]

        # 创建任务 2 依赖任务 1
        t2 = state.create_or_update_task({
            "project": proj_name, "title": "联调验收", "assignee": "qa", "depends_on": [tid1],
        })
        assert t2["ok"]
        tid2 = t2["id"]

        # 错误校验：不能依赖自身，不能成环
        try:
            state.create_or_update_task({"project": proj_name, "id": tid1, "title": "修改接口", "depends_on": [tid1]})
            raise AssertionError("应拒绝依赖自身")
        except ValueError as e:
            assert "不能依赖自身" in str(e)

        try:
            state.create_or_update_task({"project": proj_name, "id": tid1, "title": "修改接口", "depends_on": [tid2]})
            raise AssertionError("应拒绝环依赖")
        except ValueError as e:
            assert "循环" in str(e)

        # 任务详情读取
        detail = state.get_task({"project": proj_name, "id": tid2})
        assert detail["depends_on"] == [tid1]

        # full_state 中任务带 dep_state
        fs_tasks = {t["id"]: t for t in state.full_state()["projects"][0]["tasks"]}
        assert fs_tasks[tid1]["dep_state"] == "ready"
        assert fs_tasks[tid2]["dep_state"] == "waiting"
        assert fs_tasks[tid2]["blocking_deps"] == [{"id": tid1, "status": "open"}]

        # 3. 删除正在被引用的预设时报错拦截
        try:
            state.save_settings({"agent_profiles": {"pm": {"name": "PM", "role": "pm"}}})
            raise AssertionError("应阻止删除被项目引用的预设")
        except ValueError as e:
            assert "正被项目" in str(e)
        print("ok: 项目小组配置保存落盘 / 任务依赖 CRUD 校验 / 环拦截 / 预设防误删保护")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_manual_commit_and_git_init() -> None:
    tmp, state = _make_env()
    try:
        proj_dir = state.config.projects[0].path
        g = Git(proj_dir)

        # 模拟代码在主工作区落地未提交
        (proj_dir / "feature.py").write_text("print('feat')\n", encoding="utf-8")
        pending = state.full_state()["projects"][0]["pending_git_changes"]
        assert pending["count"] >= 1 and "feature.py" in pending["files"]

        # 人工把关提交弹窗：查看单个文件未提交改动 diff
        # 已跟踪文件的修改 → git diff；未跟踪的新文件 → 内容预览
        (proj_dir / "README.md").write_text("# Test\n\nnew line added\n", encoding="utf-8")
        res_diff_tracked = state.project_file_diff({
            "name": "my_project", "file": "README.md"})
        assert res_diff_tracked["ok"] and "new line added" in res_diff_tracked["diff"]
        res_diff_untracked = state.project_file_diff({
            "name": "my_project", "file": "feature.py"})
        assert res_diff_untracked["ok"] and "print('feat')" in res_diff_untracked["diff"]
        assert "新增文件" in res_diff_untracked["diff"]
        # 非法路径拒绝（GitError 与 ValueError 在端点层同样转 400）
        from orcsy.git_ops import GitError
        for bad_path in ("../escape.yaml", "..\\escape.yaml", "/abs/path"):
            try:
                state.project_file_diff({"name": "my_project", "file": bad_path})
                raise AssertionError(f"非法路径应被拒绝：{bad_path}")
            except (ValueError, GitError):
                pass  # 预期拒绝
        # 未知项目拒绝
        try:
            state.project_file_diff({"name": "no_such", "file": "x.py"})
            raise AssertionError("未知项目应报错")
        except ValueError as e:
            assert "未知项目" in str(e)

        # 人工把关提交端点
        res_commit = state.manual_commit_project({
            "name": "my_project",
            "message": "feat: manual review approved",
            "push": False,
        })
        assert res_commit["ok"] and res_commit["committed"]
        assert not g.is_dirty(), "人工提交后工作区应恢复干净"

        # 测试 git_init 端点（针对纯非 Git 目录一键就绪）
        from orcsy.agent import ProjectAgent
        non_git_dir = tmp / "non_git_proj"
        non_git_dir.mkdir()
        p_dummy = ProjectConfig(name="non_git_proj", path=non_git_dir)
        state.config.projects.append(p_dummy)
        state.agents["non_git_proj"] = ProjectAgent(p_dummy, state.config.llm)
        res_init = state.git_init_project({"name": "non_git_proj"})
        assert res_init["ok"]
        assert Git(non_git_dir).is_repo()
        print("ok: /api/project/diff 单文件 diff 预览（跟踪/未跟踪/穿越拒绝）")
        print("ok: /api/project/commit 人工提交把关 / /api/project/git-init 一键初始化 Git")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_full_state_and_profiles_saving()
    check_project_team_save_and_task_crud()
    check_manual_commit_and_git_init()
    print("\nM3 Web API 状态与操作接口测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
