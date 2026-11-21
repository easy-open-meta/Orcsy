"""Phase 2 扩展角色（devops / ui_critic / security）与 worktree 空工作区自愈兜底测试。

运行：uv run python tests/test_phase2_roles.py
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from orcsy.config import (BUILT_IN_ROLES, VALID_AGENT_ROLES, ProjectTeamConfig,
                          TeamMemberConfig, load_config, save_config)
from orcsy.git_ops import Git
from orcsy.prompts import build_role_prompt_extra
from orcsy.worktree import WorktreeManager


def check_role_registration() -> None:
    assert set(("devops", "ui_critic", "security")) <= set(VALID_AGENT_ROLES)
    for role in ("devops", "ui_critic", "security"):
        info = BUILT_IN_ROLES[role]
        assert info["label"] and info["icon"] and info["description"]
        assert info["default_prompt_template"], f"{role} 必须有内置职责模板"
    # 只读验证角色不带代码写工具；ui_critic/security 具备报 Bug 与联调/扫描能力
    for role in ("ui_critic", "security"):
        tools = BUILT_IN_ROLES[role]["default_tools"]
        assert tools.get("write_file") is False and tools.get("edit_file") is False
        assert tools.get("report_bug") is True
    assert BUILT_IN_ROLES["ui_critic"]["default_tools"].get("browser") is True
    assert BUILT_IN_ROLES["devops"]["default_tools"].get("report_bug") is False
    assert BUILT_IN_ROLES["devops"]["produces_code"] is True
    print("扩展角色注册（VALID_AGENT_ROLES / BUILT_IN_ROLES / 内置模板）✅")


def check_role_prompt_extra() -> None:
    devops = build_role_prompt_extra("devops", "运维", None)
    assert "DevOps" in devops and "docker" in devops.lower()
    assert "明文密钥" in devops
    ui = build_role_prompt_extra("ui_critic", "视觉审查", None)
    assert "report_bug" in ui and "browser_snapshot" in ui and "不修改业务源码" in ui
    sec = build_role_prompt_extra("security", "安全审计", None)
    assert "run_command" in sec and "report_bug" in sec and "CVE" in sec
    # 团队附加要求仍然透传
    with_extra = build_role_prompt_extra("security", "安全审计", None,
                                         extra_prompt="重点审计支付模块")
    assert "重点审计支付模块" in with_extra
    print("build_role_prompt_extra 三个新角色专属分支 ✅")


def check_config_roundtrip() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="cfg-p2roles-"))
    try:
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text(
            "projects:\n"
            "  - name: p1\n"
            "    path: .\n"
            "    team:\n"
            "      enabled: true\n"
            "      members:\n"
            "        - id: devops1\n"
            "          role: devops\n"
            "          name: 运维工程师\n"
            "        - id: uicrit1\n"
            "          role: ui_critic\n"
            "        - id: secaudit1\n"
            "          role: security\n",
            encoding="utf-8"
        )
        cfg = load_config(cfg_file, require_api_key=False)
        team = cfg.projects[0].team
        assert isinstance(team, ProjectTeamConfig) and team.enabled
        roles = {m.id: m.role for m in team.members}
        assert roles == {"devops1": "devops", "uicrit1": "ui_critic",
                         "secaudit1": "security"}

        # save → load 往返不丢新角色字段（save 写回 cfg.source_path）
        out = tmp / "out.yaml"
        cfg.source_path = out
        save_config(cfg)
        cfg2 = load_config(out, require_api_key=False)
        roles2 = {m.id: m.role for m in cfg2.projects[0].team.members}
        assert roles2 == roles
        print("扩展角色配置往返（load / save / load）✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_worktree_empty_heal() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="wt-heal-"))
    try:
        proj = tmp / "proj"
        proj.mkdir()
        (proj / "app.py").write_text("print('hello')\n", encoding="utf-8")
        (proj / "README.md").write_text("# demo\n", encoding="utf-8")
        git = Git(proj)
        git.init()
        git.ensure_gitignore(["tasks/", ".worktree-arranger/"])
        git.add_all()
        git.commit("init")

        mgr = WorktreeManager(proj, "proj", commit_and_merge=True)
        branch = mgr.setup()
        wt = mgr.default_worktree
        assert (wt / "app.py").is_file(), "正常 setup 必须检出文件"

        # 模拟实测偶发的空工作区：文件全丢但 .git 指针仍在 → setup 应自动重建
        for child in wt.iterdir():
            if child.name != ".git":
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink()
        assert not (wt / "app.py").exists()
        mgr.setup()
        assert (wt / "app.py").is_file(), "空工作区必须被自愈重建"
        print("worktree 空工作区自愈兜底 ✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    check_role_registration()
    check_role_prompt_extra()
    check_config_roundtrip()
    check_worktree_empty_heal()
    print("全部 Phase 2 扩展角色与 worktree 自愈测试通过 ✨")


if __name__ == "__main__":
    main()
