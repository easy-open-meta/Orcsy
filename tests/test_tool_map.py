"""任务启动工具扫描的接线测试：提示词「本机工具」节 + env.path_autofix 配置往返。

运行：uv run python tests/test_tool_map.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import load_config
from orcsy.prompts import build_system_prompt

ROOT = Path(__file__).resolve().parents[1]


def make_project() -> tuple:
    proj_dir = Path(tempfile.mkdtemp(prefix="tool-map-"))
    cfg = load_config(ROOT / "config.example.yaml", require_api_key=False)
    from orcsy.config import ProjectConfig
    proj = ProjectConfig(name="demo", path=proj_dir)
    return proj, cfg


def check_prompt_section() -> None:
    proj, _ = make_project()
    try:
        tool_map = {
            "tools": {
                "npm": r"C:\Program Files\nodejs\npm.cmd",
                "node": r"C:\Program Files\nodejs\node.EXE",
                "git": r"C:\Program Files\Git\cmd\git.exe",
            },
            "extra_dirs": [r"C:\Program Files\nodejs"],
        }
        p = build_system_prompt(proj, "指引", is_git_repo=False, tool_map=tool_map)
        assert "## 本机工具" in p
        assert r"C:\Program Files\nodejs\npm.cmd" in p
        assert "node →" in p
        assert "git →" not in p  # 不在注入目录里的命令不列（本来就能用）
        assert "用裸名直接调用即可" in p
        # tool_map=None：整节约省略（回归：老调用方式行为不变）
        p2 = build_system_prompt(proj, "指引", is_git_repo=False)
        assert "本机工具" not in p2
        # 有 extra_dirs 但命令都不属于注入目录：同样省略
        p3 = build_system_prompt(proj, "指引", is_git_repo=False, tool_map={
            "tools": {"git": r"C:\Program Files\Git\cmd\git.exe"},
            "extra_dirs": [r"C:\Program Files\nodejs"]})
        assert "本机工具" not in p3
        print("ok: 提示词「本机工具」节（只列注入命令/None 省略/空注入省略）")
    finally:
        import shutil
        shutil.rmtree(proj.path, ignore_errors=True)


def check_config_roundtrip() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tool-map-cfg-"))
    try:
        # 嵌套形态：path_autofix 读取
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text(
            "llm:\n  provider: openai\n  api_key: k\n  model: m\n"
            "env:\n  vars:\n    GOPROXY: https://goproxy.cn,direct\n"
            "  path_autofix: true\n", encoding="utf-8")
        cfg = load_config(cfg_file, require_api_key=False)
        assert cfg.env.path_autofix is True
        assert cfg.env.vars == {"GOPROXY": "https://goproxy.cn,direct"}
        # 缺省 = False
        cfg2_file = tmp / "config2.yaml"
        cfg2_file.write_text(
            "llm:\n  provider: openai\n  api_key: k\n  model: m\n"
            "env:\n  vars: {}\n", encoding="utf-8")
        assert load_config(cfg2_file, require_api_key=False).env.path_autofix is False
        # 平铺形态：所有键都是环境变量，path_autofix 键误写也按变量处理（宁可不生效）
        cfg3_file = tmp / "config3.yaml"
        cfg3_file.write_text(
            "llm:\n  provider: openai\n  api_key: k\n  model: m\n"
            "env:\n  FOO: bar\n  path_autofix: true\n", encoding="utf-8")
        cfg3 = load_config(cfg3_file, require_api_key=False)
        assert cfg3.env.path_autofix is False
        # YAML 的 true 解析成布尔 True，str() 后为 "True"——键保留在 vars 里
        assert str(cfg3.env.vars.get("path_autofix", "")).lower() == "true"
        # 示例模板：默认 false 且能加载
        assert load_config(ROOT / "config.example.yaml",
                           require_api_key=False).env.path_autofix is False
        print("ok: env.path_autofix 配置往返（嵌套读取/缺省/平铺不误读/示例默认关）")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def check_web_roundtrip() -> None:
    """Web 层保存/回显/落盘：开关不被「只带 vars」的保存静默重置。"""
    import shutil
    tmp = Path(tempfile.mkdtemp(prefix="tool-map-web-"))
    try:
        from orcsy.config import AppConfig, LLMConfig
        from orcsy.scheduler import Scheduler
        from orcsy.web import AppState
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text("llm: {}\n", encoding="utf-8")
        cfg = AppConfig(llm=LLMConfig(api_key="fake", base_url="http://localhost:9",
                                      model="fake"),
                        source_path=cfg_file)
        state = AppState(cfg, Scheduler(cfg))

        # 开启开关 → full_state 回显
        state.save_settings({"env": {"vars": {"FOO": "1"}, "path_autofix": True}})
        assert state.config.env.path_autofix is True
        assert state.full_state()["env_path_autofix"] is True

        # mirror_apply 式保存（只带 vars，无 path_autofix 键）→ 保持原值
        state.save_settings({"env": {"vars": {"FOO": "2"}}})
        assert state.config.env.path_autofix is True, "开关被静默重置"

        # 显式关闭 → 生效
        state.save_settings({"env": {"vars": {}, "path_autofix": False}})
        assert state.config.env.path_autofix is False

        # 落盘 → 重载：开启状态写进 yaml 且能读回
        state.save_settings({"env": {"vars": {"FOO": "1"}, "path_autofix": True}})
        reloaded = load_config(cfg_file, require_api_key=False)
        assert reloaded.env.path_autofix is True
        assert reloaded.env.vars == {"FOO": "1"}
        print("ok: Web 层 env.path_autofix 往返（回显/键缺失保持/显式关闭/落盘重载）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):  # GBK 控制台打不出 ✅ 等字符
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    check_prompt_section()
    check_config_roundtrip()
    check_web_roundtrip()
    print("\n工具扫描接线测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
