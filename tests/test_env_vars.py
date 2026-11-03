"""Agent 子进程环境变量（代理/国内镜像）自检：校验、注入、网络失败提示、Web 往返。

背景：国内网络拉 Go 模块/依赖常出现 unexpected EOF —— 需要能配置 GOPROXY /
HTTP_PROXY 等变量注入 run_command 子进程。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import AppConfig, EnvConfig, LLMConfig, load_config, validate_env_vars
from orcsy.executor import TaskExecutor
from orcsy.scheduler import Scheduler
from orcsy.tools import ToolContext, _augmented_env, dispatch_tool
from orcsy.web import AppState


def check_validate() -> None:
    assert validate_env_vars(None) == {}
    assert validate_env_vars({"GOPROXY": "https://goproxy.cn,direct"}) == \
        {"GOPROXY": "https://goproxy.cn,direct"}
    assert validate_env_vars({"MY_VAR2": 123}) == {"MY_VAR2": "123"}  # 值转字符串
    for bad, why in [({"1BAD": "x"}, "非法变量名"),
                     ({"BAD NAME": "x"}, "含空格"),
                     ({"PATH": "x"}, "禁止覆盖 PATH"),
                     ({"ComSpec": "x"}, "禁止覆盖 COMSPEC（大小写不敏感）"),
                     (["GOPROXY"], "非字典")]:
        try:
            validate_env_vars(bad)
            raise AssertionError(f"应拒绝：{why}")
        except ValueError:
            pass
    print("ok: validate_env_vars（合法 map/值转字符串/非法名/保留变量/非字典）")


def check_injection() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="env-inj-"))
    try:
        # 注入生效：子进程能看到配置的变量
        ctx = ToolContext(root=tmp, extra_env={"ORCSY_TEST_VAR": "hello-123"})
        r = dispatch_tool(ctx, "run_command", {"command": [
            "python", "-c", "import os; print(os.environ.get('ORCSY_TEST_VAR', 'MISSING'))"]})
        assert "hello-123" in r, r
        # 不注入时不可见（不污染全局）
        ctx2 = ToolContext(root=tmp)
        r2 = dispatch_tool(ctx2, "run_command", {"command": [
            "python", "-c", "import os; print(os.environ.get('ORCSY_TEST_VAR', 'MISSING'))"]})
        assert "MISSING" in r2, r2
        # _augmented_env 优先级：配置变量覆盖同名系统变量（除保留项，校验层已挡）
        env = _augmented_env(ctx)
        assert env["ORCSY_TEST_VAR"] == "hello-123"
        print("ok: 环境变量注入 run_command（生效/不注入不污染/扩充 PATH 共存）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_network_hint() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="env-net-"))
    try:
        ctx = ToolContext(root=tmp)
        # 模拟网络类失败：让 python 打印特征文本并以非零码退出
        r = dispatch_tool(ctx, "run_command", {"command": [
            "python", "-c",
            "import sys; print('go: downloading modernc.org/sqlite'); "
            "print('unexpected EOF', file=sys.stderr); sys.exit(1)"]})
        assert "环境提示" in r and "GOPROXY" in r and "goproxy.cn" in r, r
        # 非网络失败不提示（避免噪音）
        r2 = dispatch_tool(ctx, "run_command", {"command": [
            "python", "-c", "import sys; print('build error: undefined x'); sys.exit(1)"]})
        assert "环境提示" not in r2, r2
        print("ok: 网络类失败给出镜像/代理指引，普通失败不打扰")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_web_roundtrip() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="env-web-"))
    try:
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text("llm: {}\n", encoding="utf-8")
        cfg = AppConfig(
            llm=LLMConfig(api_key="fake", base_url="http://localhost:9", model="fake"),
            source_path=cfg_file)
        state = AppState(cfg, Scheduler(cfg))
        assert state.full_state()["env_cfg"] == {}
        r = state.save_settings({"env": {"vars": {"GOPROXY": "https://goproxy.cn,direct"}}})
        assert r["ok"] is True
        assert state.config.env == EnvConfig(vars={"GOPROXY": "https://goproxy.cn,direct"})
        # full_state 回显 + 执行器拿到注入值
        assert state.full_state()["env_cfg"]["GOPROXY"] == "https://goproxy.cn,direct"
        # 非法保存 400 且不污染
        for bad in ({"PATH": "x"}, {"1bad": "y"}):
            try:
                state.save_settings({"env": {"vars": bad}})
                raise AssertionError(f"应拒绝：{bad}")
            except ValueError:
                pass
        assert state.config.env.vars == {"GOPROXY": "https://goproxy.cn,direct"}
        # 落盘 roundtrip
        disk = load_config(cfg_file, require_api_key=False)
        assert disk.env.vars == {"GOPROXY": "https://goproxy.cn,direct"}
        assert "GOPROXY" in cfg_file.read_text(encoding="utf-8")
        # 手写平铺形态（env: 直接键值，无 vars 层）也能读回——此前会读成空、
        # 下次保存静默丢失，表现为「值没有持久化」
        flat = tmp / "flat.yaml"
        flat.write_text("llm: {}\nenv:\n  GOPROXY: https://goproxy.cn,direct\n"
                        "  HTTP_PROXY: http://127.0.0.1:7890\n", encoding="utf-8")
        d2 = load_config(flat, require_api_key=False)
        assert d2.env.vars == {"GOPROXY": "https://goproxy.cn,direct",
                               "HTTP_PROXY": "http://127.0.0.1:7890"}, d2.env.vars
        # save_settings 也兼容平铺 payload（整块替换语义）
        r = state.save_settings({"env": {"PIP_INDEX_URL": "https://pypi.org/simple"}})
        assert r["ok"] and state.config.env.vars == {"PIP_INDEX_URL": "https://pypi.org/simple"}
        # 执行器把 env 传给 ToolContext（agent 构造链；cfg 与 AppState 共享同一对象）
        from orcsy.agent import ProjectAgent
        from orcsy.config import ProjectConfig
        agent = ProjectAgent(ProjectConfig(name="p", path=tmp), cfg.llm,
                             extra_env=cfg.env.vars)
        assert agent.executor.extra_env == {"PIP_INDEX_URL": "https://pypi.org/simple"}
        print("ok: Web 层（save_settings/full_state/roundtrip/平铺兼容/执行器透传）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_validate()
    check_injection()
    check_network_hint()
    check_web_roundtrip()
    print("\n子进程环境变量自检全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
