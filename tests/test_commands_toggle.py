"""命令白名单开关自检：校验/合并语义、schema 到 dispatch 的双层强制、
配置解析与 Web 层（save_settings/full_state/config.yaml roundtrip）。

风格与 test_tools_toggle.py 一致：main() + assert，直跑无 pytest。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import (AppConfig, CommandsConfig, LLMConfig, ProjectConfig,
                          load_config, resolve_commands)
from orcsy.scheduler import Scheduler
from orcsy.tools import (DEFAULT_ALLOWED_COMMANDS, ToolContext, dispatch_tool,
                         validate_commands_map)
from orcsy.web import AppState


def check_validate_and_resolve() -> None:
    assert validate_commands_map(None) == {}
    assert validate_commands_map({}) == {}
    assert validate_commands_map({"git": False, "msbuild": True}) == \
        {"git": False, "msbuild": True}
    for bad, why in [({"format": False}, "不在默认白名单"),
                     ({"git": "no"}, "非布尔值"),
                     (["git"], "非字典")]:
        try:
            validate_commands_map(bad)
            raise AssertionError(f"应拒绝：{why}")
        except ValueError:
            pass

    g = CommandsConfig(enabled={"git": False, "msbuild": False})
    # 无项目覆盖：默认白名单剔除禁用项，其余保持原顺序
    p_none = ProjectConfig(name="p", path=Path("."))
    eff = resolve_commands(g, p_none)
    assert "git" not in eff and "msbuild" not in eff
    assert eff == [c for c in DEFAULT_ALLOWED_COMMANDS if c not in ("git", "msbuild")]
    # 项目显式列表 = 整表替换（历史语义不变，全局禁用不影响显式列表）
    p_explicit = ProjectConfig(name="p", path=Path("."), allowed_commands=["git", "python"])
    assert resolve_commands(g, p_explicit) == ["git", "python"]
    # 全空 map：默认全放行（同一性）
    assert resolve_commands(CommandsConfig(), p_none) == DEFAULT_ALLOWED_COMMANDS
    print("ok: validate/resolve（未知命令拒绝/剔除禁用/显式列表整表替换）")


def check_dispatch_gate() -> None:
    """生效白名单经 ToolContext 下发后：被禁命令拒绝、放行命令正常。"""
    tmp = Path(tempfile.mkdtemp(prefix="cmds-disp-"))
    try:
        eff = [c for c in DEFAULT_ALLOWED_COMMANDS if c != "git"]
        ctx = ToolContext(root=tmp, allowed_commands=eff)
        r = dispatch_tool(ctx, "run_command", {"command": ["git", "status"]})
        assert r.startswith("错误") and "不在白名单" in r, r
        r = dispatch_tool(ctx, "run_command", {"command": ["python", "-c", "print(1)"]})
        assert r.startswith("退出码 0"), r
        # 环境探测命令在默认白名单内（模型排障常用）
        assert dispatch_tool(ctx, "run_command", {"command": ["where", "python"]}
                             ).startswith("退出码"), "where 应放行"
        # 命令不存在（白名单内但机器没装，如 go 未装）：给可行动指引而非干瘪报错
        r = dispatch_tool(ctx, "run_command", {"command": ["no-such-tool-xyz", "--v"]})
        assert "不在白名单" in r, r  # 白名单外的命令先被拦
        # no-such-tool-xyz 不在白名单，须先放开才能触达 FileNotFoundError 分支
        ctx_open = ToolContext(root=tmp, allowed_commands=eff + ["no-such-tool-xyz"])
        r = dispatch_tool(ctx_open, "run_command", {"command": ["no-such-tool-xyz", "--v"]})
        assert "不存在" in r and "finish" in r and "blocked" in r, r
        # None = 工具层回退默认全表（旧行为）
        ctx2 = ToolContext(root=tmp)
        assert dispatch_tool(ctx2, "run_command",
                             {"command": ["git", "--version"]}).startswith("退出码")
        print("ok: 生效白名单下发（被禁拒绝/放行正常/None 回退默认）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_config_and_web() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="cmds-cfg-"))
    try:
        # load_config：commands 节解析 + 非法值报错 + roundtrip
        proj = tmp / "proj"
        proj.mkdir()
        good = tmp / "good.yaml"
        good.write_text(
            "llm:\n  api_key: k\n  model: m\n"
            "commands:\n  git: false\n  msbuild: false\n"
            "projects:\n"
            f"  - name: p1\n    path: {proj.as_posix()}\n"
            "    allowed_commands:\n      - python\n      - pytest\n",
            encoding="utf-8")
        cfg = load_config(good, require_api_key=False)
        assert cfg.commands.enabled == {"git": False, "msbuild": False}
        assert cfg.projects[0].allowed_commands == ["python", "pytest"]
        bad = tmp / "bad.yaml"
        bad.write_text("llm:\n  api_key: k\ncommands:\n  format: false\n", encoding="utf-8")
        try:
            load_config(bad, require_api_key=False)
            raise AssertionError("应拒绝不在默认白名单的命令")
        except ValueError:
            pass

        # Web 层：save_settings + full_state 回显 + config.yaml roundtrip
        cfg_file = tmp / "web.yaml"
        cfg_file.write_text("llm: {}\n", encoding="utf-8")
        app_cfg = AppConfig(
            llm=LLMConfig(api_key="fake", base_url="http://localhost:9", model="fake"),
            source_path=cfg_file)
        state = AppState(app_cfg, Scheduler(app_cfg))
        fs = state.full_state()
        assert fs["commands_cfg"] == {}
        assert fs["commands_catalog"] == DEFAULT_ALLOWED_COMMANDS
        r = state.save_settings({"commands": {"git": False}})
        assert r["ok"] is True
        assert state.config.commands == CommandsConfig(enabled={"git": False})
        for bad in ({"format": False}, {"git": "no"}):
            try:
                state.save_settings({"commands": bad})
                raise AssertionError(f"应拒绝：{bad}")
            except ValueError:
                pass
        # 项目显式列表原样保留，resolve 不受全局禁用影响
        r = state.save_project({"name": "p1", "path": str(proj),
                                "allowed_commands": ["git", "python"]})
        assert r["ok"] is True
        p = state.config.projects[0]
        assert resolve_commands(state.config.commands, p) == ["git", "python"]
        # 持久化：config.yaml 含 commands 节，load 回读一致
        disk = load_config(cfg_file, require_api_key=False)
        assert disk.commands.enabled == {"git": False}
        assert disk.projects[0].allowed_commands == ["git", "python"]
        assert "commands:" in cfg_file.read_text(encoding="utf-8")
        print("ok: Web 层（save_settings/full_state/项目显式列表/roundtrip）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_validate_and_resolve()
    check_dispatch_gate()
    check_config_and_web()
    print("\n命令白名单开关自检全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
