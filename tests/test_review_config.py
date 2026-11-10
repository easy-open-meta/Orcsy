"""Review Agent 配置自检：审查模型解析、返工次数两级覆盖、互斥校验、配置读写往返。

运行：uv run python tests/test_review_config.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

from orcsy.config import (LLMConfig, ProjectConfig, ReviewConfig, load_config,
                          normalize_review_llm, resolve_review_llm,
                          resolve_review_rounds, review_enabled_for,
                          review_model_normalized, assert_review_model_distinct)

GLOBAL_REVIEW = ReviewConfig(
    llm=LLMConfig(api_key="sk-rev", base_url="https://api.deepseek.com/v1",
                  model="review-model", temperature=0.1))


def check_resolve_review_llm() -> None:
    # 未绑定：原样返回全局审查模型
    assert resolve_review_llm(GLOBAL_REVIEW, None) is GLOBAL_REVIEW.llm
    assert resolve_review_llm(GLOBAL_REVIEW, {}) is GLOBAL_REVIEW.llm
    assert resolve_review_llm(GLOBAL_REVIEW, {"model": "", "api_key": " "}) is GLOBAL_REVIEW.llm

    # 部分覆盖：只换 model，其余继承全局审查模型
    r = resolve_review_llm(GLOBAL_REVIEW, {"model": "bound-review"})
    assert r is not GLOBAL_REVIEW.llm and r.model == "bound-review"
    assert r.base_url == GLOBAL_REVIEW.llm.base_url and r.api_key == "sk-rev"
    assert r.temperature == 0.1

    # 全局未配置模型 + 项目有效绑定：以 override 重建
    empty = ReviewConfig()
    assert resolve_review_llm(empty, None) is None
    r = resolve_review_llm(empty, {"model": "m2", "api_key": "sk-x"})
    assert r is not None and r.model == "m2" and r.api_key == "sk-x"
    # 全局未配置 + 项目绑定全空 → 仍 None
    assert resolve_review_llm(empty, {"model": ""}) is None
    print("resolve_review_llm 继承/重建语义 ✅")


def check_resolve_rounds_and_enabled() -> None:
    # 返工次数：项目覆盖优先，None 跟随全局
    proj = ProjectConfig(name="a", path=Path("."), review_max_rounds=3)
    default_proj = ProjectConfig(name="b", path=Path("."))
    unlimited_proj = ProjectConfig(name="u", path=Path("."), review_max_rounds=-1)
    assert resolve_review_rounds(GLOBAL_REVIEW, proj) == 3
    assert resolve_review_rounds(GLOBAL_REVIEW, default_proj) == 2
    assert resolve_review_rounds(ReviewConfig(max_review_rounds=5), default_proj) == 5
    assert resolve_review_rounds(GLOBAL_REVIEW, unlimited_proj) == -1
    assert resolve_review_rounds(ReviewConfig(max_review_rounds=-1), default_proj) == -1

    # 审查开关：项目覆盖优先，None 跟随全局
    assert review_enabled_for(GLOBAL_REVIEW, default_proj) is True
    off = ProjectConfig(name="c", path=Path("."), review_enabled=False)
    on = ProjectConfig(name="d", path=Path("."), review_enabled=True)
    assert review_enabled_for(GLOBAL_REVIEW, off) is False
    assert review_enabled_for(ReviewConfig(enabled=False), on) is True
    assert review_enabled_for(ReviewConfig(enabled=False), default_proj) is False
    print("resolve_review_rounds / review_enabled_for 两级覆盖 ✅")


def check_model_distinct() -> None:
    task = LLMConfig(api_key="k", model="GPT-4o")
    rev_same = LLMConfig(api_key="k2", model=" gpt-4O ")  # 大小写/空白不同仍是同一模型
    rev_diff = LLMConfig(api_key="k3", model="deepseek-chat")

    # 任一未配置 → 直接通过
    assert_review_model_distinct(task, None)
    assert_review_model_distinct(None, rev_diff)

    try:
        assert_review_model_distinct(task, rev_same, context="项目 demo 的 review")
        raise AssertionError("同名模型应报错")
    except ValueError as e:
        assert "不能与任务模型相同" in str(e), f"错误文案不符：{e}"

    assert_review_model_distinct(task, rev_diff)  # 不同模型通过
    assert review_model_normalized(" GPT-4o ") == review_model_normalized("gpt-4o")
    print("assert_review_model_distinct 互斥判重 ✅")


def check_normalize() -> None:
    assert normalize_review_llm({"model": "m", "typo": 1, "api_key": "",
                                 "base_url": None}) == {"model": "m"}
    assert normalize_review_llm({"api_key": "  "}) == {}
    print("normalize_review_llm 清洗 ✅")


def _write_config(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def check_load_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = _write_config(Path(tmp) / "config.yaml", """
llm:
  api_key: sk-task
  model: gpt-4o
review:
  enabled: true
  llm:
    api_key: sk-rev
    base_url: https://api.deepseek.com/v1
    model: deepseek-chat
  max_turns: 8
  max_review_rounds: 3
  allow_test_commands: false
  squash_merge: false
projects:
  - name: a
    path: .
  - name: b
    path: .
    review_enabled: false
    review_llm:
      model: glm-4-plus
    review_max_rounds: 5
""")
        cfg = load_config(p, require_api_key=False)
        assert cfg.review.enabled is True
        assert cfg.review.llm.model == "deepseek-chat"
        assert cfg.review.llm.api_key == "sk-rev"
        assert cfg.review.max_turns == 8
        assert cfg.review.max_review_rounds == 3
        assert cfg.review.allow_test_commands is False
        assert cfg.review.squash_merge is False

        pa, pb = cfg.projects
        assert pa.review_enabled is None and pa.review_llm is None
        assert pa.review_max_rounds is None
        assert review_enabled_for(cfg.review, pa) is True
        assert resolve_review_rounds(cfg.review, pa) == 3
        assert resolve_review_llm(cfg.review, pa.review_llm).model == "deepseek-chat"

        assert pb.review_enabled is False
        assert resolve_review_llm(cfg.review, pb.review_llm).model == "glm-4-plus"
        # 部分覆盖：model 换了，api_key/base_url 继承全局审查模型
        assert resolve_review_llm(cfg.review, pb.review_llm).api_key == "sk-rev"
        assert pb.review_max_rounds == 5
    print("load_config review 节往返 ✅")


def check_legacy_config_defaults() -> None:
    # 老配置（无 review 节、项目无 review 字段）：默认 enabled + 未配置模型（迁移兼容）
    with tempfile.TemporaryDirectory() as tmp:
        p = _write_config(Path(tmp) / "config.yaml", """
llm:
  api_key: sk-task
  model: gpt-4o
projects:
  - name: a
    path: .
""")
        cfg = load_config(p, require_api_key=False)
        assert cfg.review.enabled is True
        assert cfg.review.llm is None
        assert cfg.review.max_review_rounds == 2
        assert cfg.review.max_turns == 10
        assert cfg.review.allow_test_commands is True
        assert cfg.review.squash_merge is True
        assert cfg.projects[0].review_enabled is None
        assert cfg.projects[0].review_llm is None
        assert cfg.projects[0].review_max_rounds is None
    print("老配置无 review 节迁移兼容 ✅")


def check_load_rejects_conflict() -> None:
    # 显式冲突：审查模型与生效任务模型同名（大小写不敏感）→ 加载报错
    with tempfile.TemporaryDirectory() as tmp:
        p = _write_config(Path(tmp) / "config.yaml", """
llm:
  api_key: sk-task
  model: gpt-4o
review:
  llm:
    api_key: sk-rev
    model: GPT-4O
projects:
  - name: a
    path: .
""")
        try:
            load_config(p, require_api_key=False)
            raise AssertionError("冲突配置应报错")
        except ValueError as e:
            assert "不能与任务模型相同" in str(e), f"错误文案不符：{e}"

        # 项目关闭审查时冲突配置放行（审查不执行，无自审自批可能）
        p2 = _write_config(Path(tmp) / "config2.yaml", """
llm:
  api_key: sk-task
  model: gpt-4o
review:
  llm:
    api_key: sk-rev
    model: gpt-4o
projects:
  - name: a
    path: .
    review_enabled: false
""")
        cfg = load_config(p2, require_api_key=False)
        assert cfg.projects[0].review_enabled is False

        # 全局未配置审查模型 → 不做互斥校验（默认态合法）
        p3 = _write_config(Path(tmp) / "config3.yaml", """
llm:
  api_key: sk-task
  model: gpt-4o
projects:
  - name: a
    path: .
""")
        load_config(p3, require_api_key=False)
    print("load_config 互斥校验（冲突拒启/项目关闭放行/未配置放行） ✅")


def check_load_rejects_bad_values() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        for bad, needle in (
            ("review:\n  max_review_rounds: 0\n", "max_review_rounds"),
            ("review:\n  max_review_rounds: -2\n", "max_review_rounds"),
            ("review:\n  max_review_rounds: 11\n", "max_review_rounds"),
            ("review:\n  max_turns: 0\n", "max_turns"),
            ("review:\n  max_turns: -2\n", "max_turns"),
            ("review:\n  llm: oops\n", "review.llm"),
            ("projects:\n  - name: a\n    path: .\n    review_max_rounds: 99\n",
             "review_max_rounds"),
            ("projects:\n  - name: a\n    path: .\n    review_enabled: yes-please\n",
             "review_enabled"),
        ):
            p = _write_config(Path(tmp) / "bad.yaml", "llm:\n  api_key: k\n  model: m\n" + bad)
            try:
                load_config(p, require_api_key=False)
                raise AssertionError(f"{bad!r} 应报错")
            except ValueError as e:
                assert needle in str(e), f"错误信息应含 {needle}：{e}"

        # -1（不设上限）合法放行
        p_unlimited = _write_config(Path(tmp) / "unlimited.yaml", """
llm:
  api_key: k
  model: m
review:
  max_review_rounds: -1
  max_turns: -1
projects:
  - name: a
    path: .
    review_max_rounds: -1
""")
        cfg_unlimited = load_config(p_unlimited, require_api_key=False)
        assert cfg_unlimited.review.max_review_rounds == -1
        assert cfg_unlimited.review.max_turns == -1
        assert cfg_unlimited.projects[0].review_max_rounds == -1
    print("load_config 非法值校验 ✅")


def main() -> None:
    check_resolve_review_llm()
    check_resolve_rounds_and_enabled()
    check_model_distinct()
    check_normalize()
    check_load_roundtrip()
    check_legacy_config_defaults()
    check_load_rejects_conflict()
    check_load_rejects_bad_values()
    print("\nreview 配置全部自检通过 ✅")


if __name__ == "__main__":
    main()
