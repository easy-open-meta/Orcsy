"""项目专属模型绑定自检：合并语义、配置读写往返、Web 载荷与存盘。

运行：uv run python tests/test_project_llm.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

from orcsy.config import (AppConfig, LLMConfig, LoggingConfig, ProjectConfig,
                          SchedulerConfig, WebConfig, load_config,
                          normalize_llm_override, resolve_llm)

GLOBAL = LLMConfig(api_key="sk-global", base_url="https://api.deepseek.com/v1",
                   model="global-model", temperature=0.3)


def check_resolve() -> None:
    # 未绑定：原样返回全局对象（行为与旧版完全一致）
    assert resolve_llm(GLOBAL, None) is GLOBAL
    assert resolve_llm(GLOBAL, {}) is GLOBAL
    assert resolve_llm(GLOBAL, {"model": "", "api_key": "  "}) is GLOBAL

    # 部分覆盖：只换 model，其余继承
    r = resolve_llm(GLOBAL, {"model": "bound-model"})
    assert r is not GLOBAL and r.model == "bound-model"
    assert r.base_url == GLOBAL.base_url and r.api_key == "sk-global"
    assert r.temperature == 0.3 and r.provider == "openai"

    # 全量覆盖：整套接口+参数都换，未写字段仍取默认/全局
    r = resolve_llm(GLOBAL, {"provider": "anthropic", "base_url": "https://x.example/y",
                             "api_key": "sk-b", "model": "m1", "temperature": 0.7})
    assert r.provider == "anthropic" and r.api_key == "sk-b" and r.temperature == 0.7
    assert r.max_tokens == 8192  # 未写 → 继承全局

    # 非法值立即报错（带上下文）
    for bad in ({"provider": "oops"}, {"base_url": "ftp://x"},
                {"temperature": "hot"}):
        try:
            resolve_llm(GLOBAL, bad, context="项目 demo 的 llm")
            raise AssertionError(f"{bad} 应报错")
        except ValueError as e:
            assert "demo" in str(e), f"错误信息应带项目上下文：{e}"
    print("resolve_llm 合并/校验语义 ✅")


def check_normalize() -> None:
    assert normalize_llm_override({"model": "m", "typo": 1, "api_key": "",
                                   "base_url": None}) == {"model": "m"}
    assert normalize_llm_override({"api_key": "   "}) == {}
    print("normalize_llm_override 清洗 ✅")


def _sample_config(path: Path) -> AppConfig:
    proj_a = path / "proj-a"
    proj_a.mkdir()
    return AppConfig(
        llm=GLOBAL,
        scheduler=SchedulerConfig(), logging=LoggingConfig(), web=WebConfig(),
        projects=[
            ProjectConfig(name="bound", path=proj_a, llm={"model": "bound-model"}),
            ProjectConfig(name="plain", path=path),
        ],
        source_path=path / "config.yaml",
    )


def check_save_and_load_roundtrip() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="orcsy-llm-test-"))
    try:
        cfg = _sample_config(tmp)
        # 经 Web 存盘路径写出（AppState._save_config 只用 cfg 参数，
        # 用 __new__ 免掉整套 Scheduler/Web 依赖），再 load_config 读回
        from orcsy.web import AppState

        AppState.__new__(AppState)._save_config(cfg)
        text = (tmp / "config.yaml").read_text(encoding="utf-8")
        assert "llm:" in text and "bound-model" in text, "项目 llm 应写入 config.yaml"
        # 未绑定项目不应出现 llm 键（按行锚定匹配，避免路径等子串误判）
        plain_block = text.split("name: plain", 1)[1]
        assert "\n  llm:" not in plain_block, "未绑定项目不应写 llm"

        loaded = load_config(tmp / "config.yaml", require_api_key=True)
        assert [p.name for p in loaded.projects] == ["bound", "plain"]
        assert loaded.projects[0].llm == {"model": "bound-model"}
        assert loaded.projects[1].llm is None
        assert loaded.llm.model == "global-model"
        # 绑定项目解析出专属模型；未绑定原样用全局
        a = resolve_llm(loaded.llm, loaded.projects[0].llm)
        assert a.model == "bound-model" and a.api_key == "sk-global"
        assert resolve_llm(loaded.llm, loaded.projects[1].llm) is loaded.llm
        print("config.yaml 存盘/读回 llm 往返 ✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_load_rejects_bad_binding() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="orcsy-llm-bad-"))
    try:
        proj = tmp / "proj"
        proj.mkdir()
        (tmp / "config.yaml").write_text(
            "llm:\n"
            "  api_key: sk-global\n"
            "  base_url: https://api.deepseek.com/v1\n"
            "  model: global-model\n"
            "projects:\n"
            "  - name: bad\n"
            f"    path: {proj.as_posix()}\n"
            "    llm:\n"
            "      provider: oops\n",
            encoding="utf-8",
        )
        try:
            load_config(tmp / "config.yaml")
            raise AssertionError("非法 provider 应加载失败")
        except ValueError as e:
            assert "bad" in str(e) and "provider" in str(e)
        print("load_config 对非法绑定快速失败 ✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_resolve()
    check_normalize()
    check_save_and_load_roundtrip()
    check_load_rejects_bad_binding()
    print("\n全部断言通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
