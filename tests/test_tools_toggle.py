"""工具开关机制测试：配置解析/合并语义、schema 过滤、dispatch 拦截、
提示词动态化、Web 保存与回显、config.yaml 持久化 roundtrip。

风格与 test_project_llm.py 一致：main() + assert，直跑无 pytest。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import (AppConfig, LLMConfig, ProjectConfig, ToolsConfig,
                          load_config, resolve_tools)
from orcsy.executor import TaskExecutor
from orcsy.prompts import _output_contract, _skills_section, build_system_prompt
from orcsy.scheduler import Scheduler
from orcsy.tools import (TOGGLEABLE_TOOLS, FinishSignal, ToolContext,
                         dispatch_tool, tool_definitions_anthropic,
                         tool_definitions_openai, validate_tools_map)
from orcsy.web import AppState


def check_validate_and_resolve() -> None:
    assert validate_tools_map(None) == {}
    assert validate_tools_map({}) == {}
    assert validate_tools_map({"grep": False, "web_fetch": True}) == \
        {"grep": False, "web_fetch": True}
    # finish: true 是 UI 展示行的全量 map 产物——容忍并剥掉，不进配置
    assert validate_tools_map({"finish": True, "grep": False}) == {"grep": False}
    for bad, why in [({"nope": True}, "未知工具名"),
                     ({"finish": False}, "禁用 finish"),
                     ({"finish": "yes"}, "finish 非布尔 true"),
                     ({"grep": "yes"}, "非布尔值"),
                     ("grep", "非字典")]:
        try:
            validate_tools_map(bad)
            raise AssertionError(f"应拒绝：{why}")
        except ValueError:
            pass
    g = ToolsConfig(enabled={"run_command": False})
    assert resolve_tools(g, None) is g.enabled        # 未覆盖保持同一性
    assert resolve_tools(g, {}) is g.enabled          # 空覆盖同样继承
    merged = resolve_tools(g, {"run_command": True, "web_fetch": False})
    assert merged == {"run_command": True, "web_fetch": False}  # 项目逐项覆盖全局
    print("ok: validate_tools_map / resolve_tools（继承、逐项覆盖、非法值拒绝）")


def check_load_config() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tools-cfg-"))
    try:
        proj = tmp / "proj"
        proj.mkdir()
        good = tmp / "good.yaml"
        good.write_text(
            "llm:\n  api_key: k\n  model: m\ntools:\n  web_fetch: false\n"
            "projects:\n"
            f"  - name: p1\n    path: {proj.as_posix()}\n"
            "    tools:\n      web_fetch: true\n      grep: false\n",
            encoding="utf-8")
        cfg = load_config(good, require_api_key=False)
        assert cfg.tools.enabled == {"web_fetch": False}
        assert cfg.projects[0].tools == {"web_fetch": True, "grep": False}
        # 无 tools 节 → 默认全开、项目 None
        bare = tmp / "bare.yaml"
        bare.write_text(f"llm:\n  api_key: k\n  model: m\n"
                        f"projects:\n  - name: p1\n    path: {proj.as_posix()}\n",
                        encoding="utf-8")
        cfg2 = load_config(bare, require_api_key=False)
        assert cfg2.tools.enabled == {} and cfg2.projects[0].tools is None
        # 非法配置加载期即报错
        for body, why in [("tools:\n  nope: false\n", "全局未知工具名"),
                          ("tools:\n  finish: false\n", "全局禁用 finish"),
                          ("projects:\n  - name: p1\n    path: "
                           + proj.as_posix() + "\n    tools:\n      zz: true\n",
                           "项目未知工具名")]:
            bad = tmp / "bad.yaml"
            bad.write_text("llm:\n  api_key: k\n  model: m\n" + body, encoding="utf-8")
            try:
                load_config(bad, require_api_key=False)
                raise AssertionError(f"应拒绝：{why}")
            except ValueError:
                pass
        print("ok: load_config（全局/项目 tools 解析、缺省语义、非法值加载期报错）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_schema_filter_and_dispatch() -> None:
    full = tool_definitions_openai()
    assert len(full) == len(TOGGLEABLE_TOOLS) + 1     # 含 finish
    enabled = frozenset({"read_file", "grep", "finish"})
    names_o = {t["function"]["name"] for t in tool_definitions_openai(enabled)}
    names_a = {t["name"] for t in tool_definitions_anthropic(enabled)}
    assert names_o == names_a == set(enabled), (names_o, names_a)

    tmp = Path(tempfile.mkdtemp(prefix="tools-disp-"))
    try:
        ctx = ToolContext(root=tmp, enabled_tools=frozenset({"read_file", "finish"}))
        (tmp / "a.txt").write_text("hello", encoding="utf-8")
        assert "hello" in dispatch_tool(ctx, "read_file", {"path": "a.txt"})
        r = dispatch_tool(ctx, "write_file", {"path": "b.txt", "content": "x"})
        assert r.startswith("错误") and "已被配置禁用" in r, r
        assert not (tmp / "b.txt").exists(), "被禁工具不得产生副作用"
        r = dispatch_tool(ctx, "run_command", '{"command": ["git", "--version"]}')
        assert "已被配置禁用" in r, r
        # finish 不受开关约束（协议工具）
        try:
            dispatch_tool(ctx, "finish", {"action": "blocked", "summary": "s"})
            raise AssertionError("finish 应抛 FinishSignal")
        except FinishSignal:
            pass
        # enabled_tools=None → 全放行（旧行为）
        ctx2 = ToolContext(root=tmp)
        assert dispatch_tool(ctx2, "write_file", {"path": "c.txt", "content": "y"}
                             ).startswith("已创建")
        print("ok: schema 过滤（openai/anthropic 一致）+ dispatch 拦截 + finish 恒可用")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_executor_enabled_set() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tools-exec-"))
    try:
        proj = ProjectConfig(name="p", path=tmp)
        llm = LLMConfig(api_key="k", model="m")
        # 空 map / 全 true → None（不过滤，零配置零变化）
        assert TaskExecutor(proj, llm)._enabled_tools() is None
        assert TaskExecutor(proj, llm, tools={})._enabled_tools() is None
        all_on = {n: True for n in TOGGLEABLE_TOOLS}
        assert TaskExecutor(proj, llm, tools=all_on)._enabled_tools() is None
        # 有关闭项 → 启用集含 finish、不含被禁项
        en = TaskExecutor(proj, llm, tools={"run_command": False})._enabled_tools()
        assert en is not None and "finish" in en and "run_command" not in en
        assert "read_file" in en and len(en) == len(TOGGLEABLE_TOOLS)  # 未列出的默认启用
        print("ok: TaskExecutor._enabled_tools（默认 None、部分关闭收敛为启用集）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_prompts() -> None:
    proj = ProjectConfig(name="demo", path=Path("."))
    full = build_system_prompt(proj, "# g", False)
    assert "必须调用 finish 工具" in full and "资源约定" in full
    # read_skill 只随技能索引节出现（无技能时协议节不点名它，与旧版行为一致）
    for name in TOGGLEABLE_TOOLS:
        if name == "read_skill":
            continue
        assert name in full, f"全启用时提示词应提及 {name}"

    class _S:  # 鸭子类型 Skill（_skills_section 只用 name/description）
        name, description = "skill-a", "描述A"

    en = frozenset({"list_dir", "read_file", "write_file", "finish"})
    p2 = build_system_prompt(proj, "# g", False, skills=[_S()], tools=en)
    for gone in ("run_command", "web_fetch", "grep", "multi_edit"):
        assert gone not in p2, f"被禁工具 {gone} 不应出现在提示词"
    # read_skill 被禁 → 技能索引节整体消失
    assert "Skills 技能库" not in p2 and "skill-a" not in p2
    # read_skill 启用 → 索引回来
    p3 = build_system_prompt(proj, "# g", False, skills=[_S()],
                             tools=en | {"read_skill"})
    assert "skill-a" in p3
    assert _skills_section(None) == ""
    # 只剩 finish 的极端情况也要给出可执行的指令
    p4 = _output_contract(frozenset({"finish"}))
    assert "cannot_do" in p4
    print("ok: 提示词动态化（被禁工具不出现、read_skill 联动技能节、极端情况兜底）")


def check_web_roundtrip() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tools-web-"))
    try:
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text("llm: {}\n", encoding="utf-8")
        proj_dir = tmp / "proj"
        proj_dir.mkdir()
        cfg = AppConfig(
            llm=LLMConfig(api_key="fake", base_url="http://localhost:9", model="fake"),
            source_path=cfg_file)
        state = AppState(cfg, Scheduler(cfg))

        # 全局开关保存 + 回显
        r = state.save_settings({"tools": {"web_fetch": False, "delete_file": False}})
        assert r["ok"] is True
        fs = state.full_state()
        assert fs["tools_cfg"] == {"web_fetch": False, "delete_file": False}
        catalog = {t["name"] for t in fs["tools_catalog"]}
        # 目录含全部工具；finish 也在（前端渲染为常开且禁用的展示行）
        assert catalog == set(TOGGLEABLE_TOOLS) | {"finish"}
        assert all(t["description"] for t in fs["tools_catalog"])
        # 非法值 → ValueError（Web 层转 400）；finish: true 容忍（UI 全量 map 产物）
        for bad in ({"nope": True}, {"finish": False}, {"grep": "no"}):
            try:
                state.save_settings({"tools": bad})
                raise AssertionError(f"应拒绝：{bad}")
            except ValueError:
                pass
        r = state.save_settings({"tools": {"finish": True, "grep": False}})
        assert r["ok"] and state.config.tools.enabled == {"grep": False}, \
            "finish:true 应被剥掉，不污染配置"
        state.save_settings({"tools": {"web_fetch": False, "delete_file": False}})
        assert state.config.tools.enabled == {"web_fetch": False, "delete_file": False}, \
            "非法保存不得污染现有配置"

        # 项目级覆盖：新建带 tools → 回显 → 清除（null）→ 跟随全局
        r = state.save_project({"name": "proj1", "path": str(proj_dir),
                                "tools": {"web_fetch": True, "run_command": False}})
        assert r["ok"] is True
        fs = state.full_state()
        assert fs["projects"][0]["tools"] == {"web_fetch": True, "run_command": False}
        # Agent 生效 map = 全局 + 项目覆盖合并
        eff = resolve_tools(state.config.tools, state.config.projects[0].tools)
        assert eff == {"web_fetch": True, "delete_file": False, "run_command": False}
        r = state.save_project({"name": "proj1", "path": str(proj_dir),
                                "original_name": "proj1", "tools": None})
        assert r["ok"] is True
        assert state.full_state()["projects"][0]["tools"] is None

        # 持久化 roundtrip：落盘的 config.yaml 重新 load 后语义一致
        disk = load_config(cfg_file, require_api_key=False)
        assert disk.tools.enabled == {"web_fetch": False, "delete_file": False}
        assert disk.projects[0].tools is None
        state.save_project({"name": "proj1", "path": str(proj_dir),
                            "original_name": "proj1",
                            "tools": {"grep": False}})
        disk = load_config(cfg_file, require_api_key=False)
        assert disk.projects[0].tools == {"grep": False}
        print("ok: Web 层（save_settings/save_project/full_state）与 config.yaml roundtrip")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_validate_and_resolve()
    check_load_config()
    check_schema_filter_and_dispatch()
    check_executor_enabled_set()
    check_prompts()
    check_web_roundtrip()
    print("\n工具开关机制测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
