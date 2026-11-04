"""模型规格上限自检：max_tokens / max_context_tokens 跟随模型上报值。

看板拉取模型列表（/api/models）时缓存模型规格；保存配置时超出自动钳到
模型上报值并提示——模型服务端的限制才是真上限，写死的 20 万/200 万只是
没拉过模型列表时的兜底。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from orcsy.config import AppConfig, LLMConfig, load_config
from orcsy.scheduler import Scheduler
from orcsy.web import AppState

LLM_BLOCK = {"provider": "openai", "model": "qwen-test",
             "base_url": "http://localhost:9/v1", "api_key": "k"}


def _app(tmp: Path) -> AppState:
    tmp.mkdir(parents=True, exist_ok=True)
    cfg_file = tmp / "config.yaml"
    cfg_file.write_text("llm: {}\n", encoding="utf-8")
    cfg = AppConfig(llm=LLMConfig(api_key="k", base_url="http://localhost:9/v1",
                                  model="qwen-test"), source_path=cfg_file)
    return AppState(cfg, Scheduler(cfg))


def check_fetch_caches_spec(tmp: Path) -> None:
    st = _app(tmp)
    orig = httpx.get

    class FakeResp:
        status_code = 200

        def json(self):
            return {"data": [
                {"id": "qwen-test", "context_window": 200000,
                 "max_output_tokens": 65536},
                {"id": "huge-model", "context_window": 3000000,
                 "max_output_tokens": 500000},
            ]}

    def fake_get(url, headers=None, timeout=None, **kw):
        return FakeResp()

    httpx.get = fake_get
    try:
        r = st.fetch_models({})
    finally:
        httpx.get = orig
    assert r["ok"], r
    assert st._model_meta["qwen-test"]["max_output_tokens"] == 65536
    assert st._model_meta["huge-model"]["context_window"] == 3000000
    print("ok: fetch_models 缓存模型规格（含 model_spec 补全后的值）")


def check_save_clamps(tmp: Path) -> None:
    st = _app(tmp)
    st._model_meta["qwen-test"] = {"context_window": 200000,
                                   "max_output_tokens": 65536}
    r = st.save_settings({"llm": {**LLM_BLOCK, "max_tokens": 131072,
                                  "max_context_tokens": 500000}})
    assert r["ok"], r
    assert st.config.llm.max_tokens == 65536, st.config.llm.max_tokens
    assert st.config.llm.max_context_tokens == 200000
    assert "65536" in r["message"] and "200000" in r["message"], r["message"]
    # 等于上限 / 低于上限：原样保存不提示
    st.save_settings({"llm": {**LLM_BLOCK, "max_tokens": 65536,
                              "max_context_tokens": 100000}})
    assert st.config.llm.max_tokens == 65536
    st.save_settings({"llm": {**LLM_BLOCK, "max_tokens": 4096,
                              "max_context_tokens": 100000}})
    assert st.config.llm.max_tokens == 4096
    print("ok: 超模型上报值自动钳制并在 message 提示；等于/低于原样保存")


def check_fallback_and_follow(tmp: Path) -> None:
    st = _app(tmp)  # 没拉过模型列表：无元数据
    # 兜底硬顶 200000 仍生效
    try:
        st.save_settings({"llm": {**LLM_BLOCK, "max_tokens": 300000,
                                  "max_context_tokens": 100000}})
        raise AssertionError("应拒绝超硬顶")
    except ValueError as e:
        assert "256~200000" in str(e), str(e)
    # 模型上报值比硬顶更大时完全跟随模型（500000 > 200000）
    st._model_meta["qwen-test"] = {"context_window": 3000000,
                                   "max_output_tokens": 500000}
    r = st.save_settings({"llm": {**LLM_BLOCK, "max_tokens": 450000,
                                  "max_context_tokens": 100000}})
    assert r["ok"] and st.config.llm.max_tokens == 450000, r
    # 落盘 roundtrip
    disk = load_config(tmp / "config.yaml", require_api_key=False)
    assert disk.llm.max_tokens == 450000
    print("ok: 无元数据回退硬顶 200000；模型上报更大值时完全跟随（落盘 roundtrip）")


def main() -> int:
    t = Path(tempfile.mkdtemp(prefix="modelcaps-"))
    try:
        check_fetch_caches_spec(t / "a")
        check_save_clamps(t / "b")
        check_fallback_and_follow(t / "c")
        print("\n模型规格上限自检全部通过 ✅")
        return 0
    finally:
        shutil.rmtree(t, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
