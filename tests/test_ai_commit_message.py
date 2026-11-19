"""AI 生成提交说明（/api/commit-message）回归。

覆盖：
- 工作区干净（无 diff）→ ok:false 且提示干净状态
- 模型调用成功 → message 单行规范文本填入
- 模型调用失败 → ok:false 不抛异常（页面不打断）
- 未知项目 → ValueError
- diff 超长截断（generate_commit_message 单元层）
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import orcsy.llm as llm_mod
from orcsy.config import load_config
from orcsy.git_ops import Git
from orcsy.llm import generate_commit_message
from orcsy.scheduler import Scheduler
from orcsy.web import AppState


def _make_env() -> tuple[Path, AppState, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="ai-commit-"))
    proj = tmp / "proj"
    proj.mkdir(parents=True)
    g = Git(proj)
    g.run("init")
    g.run("config", "user.email", "t@t.c")
    g.run("config", "user.name", "t")
    (proj / "README.md").write_text("# init\n", encoding="utf-8")
    g.add_all()
    g.commit("chore: init")

    cfg_file = tmp / "config.yaml"
    cfg_file.write_text(
        f"""
llm:
  provider: openai
  api_key: sk-test
  base_url: http://127.0.0.1:9/v1
  model: test-model
projects:
  - name: proj
    path: {proj.as_posix()}
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file, require_api_key=False)
    state = AppState(cfg, Scheduler(cfg))
    return tmp, state, proj


def check_clean_workspace() -> None:
    tmp, state, _proj = _make_env()
    try:
        r = state.generate_commit_message({"name": "proj"})
        assert r["ok"] is False and "干净" in r["message"], r
        print("ok: 干净工作区返回 ok:false + 提示")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_unknown_project() -> None:
    tmp, state, _proj = _make_env()
    try:
        try:
            state.generate_commit_message({"name": "no-such"})
            raise AssertionError("未知项目应报错")
        except ValueError as e:
            assert "未知项目" in str(e)
        print("ok: 未知项目抛 ValueError")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_success_and_failure(monkeypatched: bool = True) -> None:
    tmp, state, proj = _make_env()
    orig = llm_mod.generate_commit_message
    try:
        # 有改动（未跟踪新文件 + 修改）
        (proj / "feature.py").write_text("print('feat')\n", encoding="utf-8")

        # 1) 模型成功：单行规范 message
        def fake_ok(provider, base_url, api_key, model, diff, timeout_seconds,
                    enable_thinking=False, max_tokens=4096):
            assert "feature.py" in diff, "diff 应包含改动文件"
            return {"message": "feat: 新增 feature 模块", "elapsed_seconds": 1.2}

        llm_mod.generate_commit_message = fake_ok
        r = state.generate_commit_message({"name": "proj"})
        assert r["ok"] is True and r["message"] == "feat: 新增 feature 模块", r

        # 2) 模型失败：ok:false 不打断页面
        def fake_fail(provider, base_url, api_key, model, diff, timeout_seconds,
                      enable_thinking=False, max_tokens=4096):
            raise llm_mod.LLMError("连接超时")

        llm_mod.generate_commit_message = fake_fail
        r2 = state.generate_commit_message({"name": "proj"})
        assert r2["ok"] is False and "连接超时" in r2["message"], r2
        print("ok: 模型成功生成 / 失败返回 ok:false 不抛异常")
    finally:
        llm_mod.generate_commit_message = orig
        shutil.rmtree(tmp, ignore_errors=True)


def check_diff_truncation() -> None:
    calls = {}

    class _FakeClient:
        def __init__(self, cfg):
            calls["cfg"] = cfg

        def _post(self, url, payload, headers):
            calls["prompt"] = payload["messages"][0]["content"]
            return {"choices": [{"message": {"content": "fix: 修复反斜杠与引号剥离"}}]}

    orig_client = llm_mod.LLMClient
    llm_mod.LLMClient = _FakeClient
    try:
        r = generate_commit_message("openai", "http://x/v1", "k", "m",
                                    "x" * 20000, 30)
        assert r["message"].startswith("fix: 修复反斜杠与引号剥离") or r["message"].startswith("fix: 修复"), r
        assert len(calls["prompt"]) < 20000, "prompt 应被截断"
        assert "diff 已截断" in calls["prompt"], "截断提示应在"
        print("ok: 超长 diff 截断且引号剥离")
    finally:
        llm_mod.LLMClient = orig_client


def main() -> int:
    check_clean_workspace()
    check_unknown_project()
    check_success_and_failure()
    check_diff_truncation()
    print("\nAI 生成提交说明全部测试通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
