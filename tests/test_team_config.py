"""M2 配置自检：agent_profiles 预设池、ProjectTeamConfig 自由组合解析、角色工具裁剪与向后兼容。"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import (
    AppConfig,
    GitConfig,
    LLMConfig,
    ProjectConfig,
    ToolsConfig,
    load_config,
    resolve_commit_and_merge,
    resolve_project_team_members,
    resolve_worktree_isolation,
    validate_agent_profiles,
    validate_project_team,
)


def check_profiles_and_team_validation() -> None:
    global_llm = LLMConfig(provider="openai", api_key="sk-g", base_url="https://api.example.com/v1", model="gpt-global")
    profiles = validate_agent_profiles(
        {
            "pm_pro": {
                "name": "架构PM",
                "role": "pm",
                "llm": {"provider": "anthropic", "model": "claude-pm", "api_key": "sk-pm"},
                "max_turns": 15,
                "extra_prompt": "严格按模块拆任务",
            },
            "qa_auto": {
                "name": "联调QA",
                "role": "qa",
                "allow_test_scripts": False,
            },
        },
        global_llm,
    )
    assert set(profiles.keys()) == {"pm_pro", "qa_auto"}
    assert profiles["pm_pro"].role == "pm" and profiles["pm_pro"].max_turns == 15

    # 非法角色、越界轮数、引用不存在预设、启用小组却无启用成员均报错
    for bad_fn, why in [
        (lambda: validate_agent_profiles({"x": {"role": "unknown"}}, global_llm), "非法角色"),
        (lambda: validate_agent_profiles({"x": {"role": "backend", "max_turns": 2}}, global_llm), "轮数过小"),
        (lambda: validate_project_team({"enabled": True, "members": []}, profiles, global_llm), "无启用成员"),
        (lambda: validate_project_team(
            {"enabled": True, "members": [{"id": "pm", "profile": "missing"}]},
            profiles, global_llm), "引用不存在预设"),
    ]:
        try:
            bad_fn()
            raise AssertionError(f"应拒绝：{why}")
        except ValueError:
            pass
    print("ok: validate_agent_profiles & validate_project_team 校验")


def check_member_resolution_and_tools() -> None:
    global_llm = LLMConfig(provider="openai", api_key="sk-g", base_url="https://api.example.com/v1", model="gpt-global")
    global_tools = ToolsConfig(enabled={"web_fetch": True})
    profiles = validate_agent_profiles(
        {
            "pm_p": {"name": "PM", "role": "pm", "llm": {"model": "pm-m"}, "extra_prompt": "P1"},
            "be_p": {"name": "后端", "role": "backend", "llm": {"model": "be-m"}},
            "qa_p": {"name": "测试", "role": "qa", "allow_test_scripts": False},
        },
        global_llm,
    )
    team = validate_project_team(
        {
            "enabled": True,
            "members": [
                {"id": "pm", "profile": "pm_p", "extra_prompt": "P2"},
                {"id": "backend", "profile": "be_p", "llm": {"model": "be-override"}},
                {"id": "qa", "profile": "qa_p"},
                {"id": "qa_writer", "role": "qa", "allow_test_scripts": True},
            ],
        },
        profiles,
        global_llm,
    )
    proj = ProjectConfig(name="demo", path=Path("."), commit_and_merge=False, team=team)

    # 小组模式下：resolve_worktree_isolation 强制为 True，但 resolve_commit_and_merge 依然为 False
    assert resolve_worktree_isolation(GitConfig(commit_and_merge=False), proj) is True
    assert resolve_commit_and_merge(GitConfig(commit_and_merge=False), proj) is False

    members = resolve_project_team_members(proj, profiles, global_llm, global_tools)
    # PM：只读 + create_tasks/reassign_task，不产出代码
    pm = members["pm"]
    assert pm["llm"].model == "pm-m"
    assert pm["produces_code"] is False
    assert pm["tools"]["write_file"] is False and pm["tools"]["run_command"] is False
    assert pm["tools"]["create_tasks"] is True and pm["tools"]["reassign_task"] is True
    assert "P1" in pm["extra_prompt"] and "P2" in pm["extra_prompt"]

    # Backend：项目内成员覆写模型，产出代码
    be = members["backend"]
    assert be["llm"].model == "be-override"
    assert be["produces_code"] is True
    assert be["tools"].get("write_file", True) is True
    assert be["tools"].get("create_tasks") is False

    # QA 默认只读 + 联调全工具 + report_bug
    qa = members["qa"]
    assert qa["produces_code"] is False
    assert qa["tools"]["write_file"] is False and qa["tools"]["edit_file"] is False
    assert qa["tools"]["report_bug"] is True
    assert qa["tools"]["browser_navigate"] is True and qa["tools"]["dev_server_start"] is True
    assert qa["tools"]["http_request"] is True

    # QA 开启 allow_test_scripts=True：解锁写文件并置 produces_code=True
    qa_w = members["qa_writer"]
    assert qa_w["produces_code"] is True
    assert qa_w["tools"]["write_file"] is True and qa_w["tools"]["report_bug"] is True
    print("ok: 成员模型继承链 / 角色工具权限裁剪 / QA 写脚本开关 / 强制 Worktree 隔离")


def check_load_config_roundtrip() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="team-cfg-"))
    try:
        proj = tmp / "app"
        proj.mkdir()
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text(
            f"""
llm:
  provider: openai
  api_key: sk-main
  model: gpt-main
agent_profiles:
  pm_default:
    name: "架构PM"
    role: pm
    llm:
      model: o3-mini
  be_default:
    name: "后端"
    role: backend
    llm:
      model: deepseek-v3
projects:
  - name: app
    path: {proj.as_posix()}
    team:
      enabled: true
      execution_mode: parallel_worktree
      max_qa_rounds: 4
      max_split_depth: 2
      chain_within_cycle: true
      max_chain_steps_per_cycle: 12
      fallback_role: backend
      members:
        - id: pm
          profile: pm_default
        - id: backend
          profile: be_default
  - name: solo_legacy
    path: {proj.as_posix()}
""",
            encoding="utf-8",
        )
        cfg = load_config(cfg_file, require_api_key=True)
        assert "pm_default" in cfg.agent_profiles
        p_team = cfg.projects[0]
        assert p_team.team is not None and p_team.team.enabled is True
        assert p_team.team.max_qa_rounds == 4 and p_team.team.max_chain_steps_per_cycle == 12
        assert resolve_worktree_isolation(cfg.git, p_team) is True

        p_solo = cfg.projects[1]
        assert p_solo.team is None
        assert resolve_worktree_isolation(cfg.git, p_solo) is False
        print("ok: load_config agent_profiles + project.team 解析与单兵兼容")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_profiles_and_team_validation()
    check_member_resolution_and_tools()
    check_load_config_roundtrip()
    print("\nM2 项目小组配置与角色解析测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
