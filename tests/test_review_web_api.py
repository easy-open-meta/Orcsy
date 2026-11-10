"""Review Agent Web 层自检：项目载荷往返、互斥拒绝、full_state 掩码、toggleProject 回传。

复用 test_state_masking 的直构 AppState 模式（无需起真实 HTTP 服务）。
运行：uv run python tests/test_review_web_api.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import (AppConfig, LLMConfig, ProjectConfig, ReviewConfig,
                          load_config)
from orcsy.scheduler import Scheduler
from orcsy.web import AppState, mask_secret

TASK_KEY = "{{SECRET_c9c5mgin}}"
REV_KEY = "{{SECRET_8vbnwvq2}}"


def _make_state(tmp: Path, *, review_llm: LLMConfig | None = None,
                project_llm: dict | None = None) -> AppState:
    tmp.mkdir(parents=True, exist_ok=True)
    root = tmp / "proj"
    root.mkdir(exist_ok=True)
    cfg = AppConfig(
        llm=LLMConfig(api_key=TASK_KEY, base_url="http://localhost:9", model="worker-y"),
        review=ReviewConfig(llm=review_llm),
        projects=[ProjectConfig(name="p1", path=root, llm=project_llm)],
        source_path=tmp / "config.yaml",
    )
    state = AppState(cfg, Scheduler(cfg), log_path=str(tmp / "orcsy.log"))
    state._tmp = tmp  # type: ignore[attr-defined]
    return state


def check_project_payload_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = _make_state(Path(tmp),
                            review_llm=LLMConfig(api_key=REV_KEY, base_url="http://localhost:9",
                                                 model="reviewer-x"))
        root = str(Path(tmp) / "proj")
        # 三个 review 字段全带：roundtrip
        p = state._project_payload({
            "name": "p1", "path": root, "enabled": True,
            "review_enabled": True,
            "review_llm": {"model": "reviewer-b", "api_key": REV_KEY},
            "review_max_rounds": 3,
        }, original_name="p1")
        assert p.review_enabled is True
        assert p.review_llm == {"model": "reviewer-b", "api_key": REV_KEY}, p.review_llm
        assert p.review_max_rounds == 3

        # 落入配置（模拟 save_project 已保存），后续编辑才有「原值」可继承
        state.config.projects[0] = p

        # api_key 三态：键缺失=保持
        keep = state._project_payload({
            "name": "p1", "path": root,
            "review_llm": {"model": "reviewer-c"},
        }, original_name="p1")
        assert keep.review_llm["api_key"] == REV_KEY, keep.review_llm
        # 空串=清除该字段（normalize 丢弃空值 → 有效绑定只剩 model）
        cleared_key = state._project_payload({
            "name": "p1", "path": root,
            "review_llm": {"model": "reviewer-c", "api_key": ""},
        }, original_name="p1")
        assert "api_key" not in (cleared_key.review_llm or {}), cleared_key.review_llm
        # 显式 null=清除整个绑定
        cleared = state._project_payload({
            "name": "p1", "path": root, "review_llm": None,
        }, original_name="p1")
        assert cleared.review_llm is None
        # -1（不上限）合法保存
        unlimited_p = state._project_payload({"name": "p1", "path": root, "review_max_rounds": -1}, original_name="p1")
        assert unlimited_p.review_max_rounds == -1
        # rounds 越界拒绝
        try:
            state._project_payload({"name": "p1", "path": root, "review_max_rounds": 11})
            raise AssertionError("rounds=11 应报错")
        except ValueError as e:
            assert "1~10" in str(e)
    print("① 项目载荷 review 字段往返 + api_key 三态 + rounds 校验 ✅")


def check_payload_rejects_same_model() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = _make_state(Path(tmp),
                            review_llm=LLMConfig(api_key=REV_KEY, base_url="http://localhost:9",
                                                 model="reviewer-x"))
        root = str(Path(tmp) / "proj")
        # 项目级审查模型与生效任务模型（worker-y）同名 → 400 拒绝
        for bad in ("WORKER-Y", " worker-y "):
            try:
                state._project_payload({
                    "name": "p1", "path": root, "review_enabled": True,
                    "review_llm": {"model": bad},
                }, original_name="p1")
                raise AssertionError(f"同名模型 {bad!r} 应报错")
            except ValueError as e:
                assert "不能与任务模型相同" in str(e)
        # 项目关闭审查时允许存在（审查不执行，无自审自批可能）
        ok = state._project_payload({
            "name": "p1", "path": root, "review_enabled": False,
            "review_llm": {"model": "worker-y"},
        }, original_name="p1")
        assert ok.review_enabled is False
    print("② 互斥校验：同名审查模型保存被拒（大小写/空白归一），项目关闭放行 ✅")


def check_full_state_review_block() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = _make_state(Path(tmp),
                            review_llm=LLMConfig(api_key=REV_KEY, base_url="http://localhost:9",
                                                 model="reviewer-x"))
        s = state.full_state()
        blob = json.dumps(s, ensure_ascii=False)
        assert REV_KEY not in blob and TASK_KEY not in blob, "审查密钥不得明文外发"
        # 全局 review_cfg 块
        rc = s["review_cfg"]
        assert rc["model_configured"] is True and rc["model"] == "reviewer-x"
        assert rc["api_key_masked"] == mask_secret(REV_KEY)
        assert "api_key" not in rc, rc
        assert rc["max_review_rounds"] == 2 and rc["allow_test_commands"] is True
        # 项目块：原始值 + 生效值
        p = s["projects"][0]
        assert p["review_enabled"] is None and p["review_max_rounds"] is None
        assert p["review_llm"] is None
        eff = p["review"]
        assert eff["enabled_effective"] is True
        assert eff["model_configured"] is True and eff["effective_model"] == "reviewer-x"
        assert eff["api_key_masked"] == mask_secret(REV_KEY)
        assert eff["effective_max_rounds"] == 2
        assert eff["warning"] == ""

        # 未配置审查模型（默认态）：中性提示数据
        state2 = _make_state(Path(tmp) / "d2")
        s2 = state2.full_state()
        assert s2["review_cfg"]["model_configured"] is False
        eff2 = s2["projects"][0]["review"]
        assert eff2["model_configured"] is False and eff2["effective_model"] == ""
        assert eff2["warning"] == ""

        # 冲突告警：项目级把审查模型绑成任务模型（经 _project_payload 校验不会进来，
        # 但手改 yaml 加载后 full_state 应给出 warning）
        cfg_file = Path(tmp) / "conflict.yaml"
        (Path(tmp) / "proj2").mkdir()
        cfg_file.write_text(
            "llm:\n  api_key: k\n  model: worker-y\n"
            "review:\n  llm:\n    api_key: k\n    model: reviewer-x\n"
            "projects:\n  - name: p1\n    path: " + str(Path(tmp) / "proj2").replace("\\", "/")
            + "\n    review_llm:\n      model: worker-y\n    review_enabled: true\n",
            encoding="utf-8")
        # 加载期互斥校验会拒绝——手改绕过需要项目关闭开关再改开，这里直接断言加载拒绝
        try:
            load_config(cfg_file, require_api_key=False)
            raise AssertionError("手改 yaml 的冲突配置应加载失败")
        except ValueError as e:
            assert "不能与任务模型相同" in str(e)
    print("③ full_state review 块（掩码/生效值/warning）+ 手改 yaml 拒载 ✅")


def check_toggle_project_roundtrip() -> None:
    """save_project 启停开关路径不得丢 review 绑定（DashboardView.toggleProject 场景）。"""
    with tempfile.TemporaryDirectory() as tmp:
        state = _make_state(Path(tmp),
                            review_llm=LLMConfig(api_key=REV_KEY, base_url="http://localhost:9",
                                                 model="reviewer-x"))
        root = str(Path(tmp) / "proj")
        p = state._project_payload({
            "name": "p1", "path": root,
            "review_enabled": True,
            "review_llm": {"model": "reviewer-b", "api_key": REV_KEY},
            "review_max_rounds": 3,
        }, original_name="p1")
        # 模拟 toggleProject：只回传 enabled + 原样透传其余字段
        toggled = state._project_payload({
            "name": "p1", "path": root, "enabled": False,
            "review_enabled": p.review_enabled,
            "review_llm": p.review_llm,
            "review_max_rounds": p.review_max_rounds,
        }, original_name="p1")
        assert toggled.enabled is False
        assert toggled.review_llm == {"model": "reviewer-b", "api_key": REV_KEY}, \
            "启停切换不得丢审查绑定"
        assert toggled.review_max_rounds == 3
        assert toggled.review_enabled is True
    print("④ toggleProject 回传不丢 review_llm / review_max_rounds ✅")


def check_settings_save_review() -> None:
    """全局 review 节分块保存 + 全项目互斥复检。"""
    with tempfile.TemporaryDirectory() as tmp:
        state = _make_state(Path(tmp))
        root = Path(tmp) / "proj"
        # 分块保存 review（api_key 三态：键缺失=保持）
        state.save_settings({"review": {
            "enabled": True,
            "llm": {"provider": "openai", "base_url": "http://localhost:9",
                    "model": "reviewer-x", "api_key": REV_KEY},
            "max_turns": 8, "max_review_rounds": 3,
            "allow_test_commands": False, "squash_merge": False,
        }})
        assert state.config.review.llm.model == "reviewer-x"
        assert state.config.review.llm.api_key == REV_KEY
        assert state.config.review.max_review_rounds == 3
        assert state.config.review.allow_test_commands is False
        assert state.config.review.squash_merge is False

        # 再保存不带 api_key：保持原值
        state.save_settings({"review": {"llm": {"model": "reviewer-z"}}})
        assert state.config.review.llm.model == "reviewer-z"
        assert state.config.review.llm.api_key == REV_KEY, "api_key 应保持"

        # 全局审查模型换成与项目任务模型同名 → 全项目复检拒绝
        try:
            state.save_settings({"review": {"llm": {"model": "WORKER-Y"}}})
            raise AssertionError("与任务模型同名的全局审查模型应被拒绝")
        except ValueError as e:
            assert "不能与任务模型相同" in str(e)
            # 失败后配置不应被半写（current 仍是 reviewer-z）
            assert state.config.review.llm.model == "reviewer-z"

        # 清除审查模型 → 回默认态
        state.save_settings({"review": {"llm": None}})
        assert state.config.review.llm is None
    print("⑤ 全局 review 节保存（三态密钥/全项目互斥复检/清除绑定） ✅")


def main() -> int:
    check_project_payload_roundtrip()
    check_payload_rejects_same_model()
    check_full_state_review_block()
    check_toggle_project_roundtrip()
    check_settings_save_review()
    print("\nreview Web 层全部自检通过 ✅")
    return 0


if __name__ == "__main__":
    main()
