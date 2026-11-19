"""AI 生成提交说明（/api/commit-message）回归。

覆盖：
- 工作区干净（无 diff）→ ok:false 且提示干净状态
- 模型调用成功 → message 填入（支持多行：标题 + 要点正文）
- 模型调用失败 → ok:false 不抛异常（页面不打断）
- 未知项目 → ValueError
- 大改动覆盖：多文件 diff 全部进入 prompt（不再整体截断丢文件）
- 超预算截断（generate_commit_message 单元层）
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


def check_success_and_failure() -> None:
    tmp, state, proj = _make_env()
    orig = llm_mod.generate_commit_message
    try:
        # 有改动（未跟踪新文件 + 修改）
        (proj / "feature.py").write_text("print('feat')\n", encoding="utf-8")

        # 1) 模型成功：多行规范 message（标题 + 要点正文）
        def fake_ok(provider, base_url, api_key, model, diff, timeout_seconds,
                    enable_thinking=False, max_tokens=4096):
            assert "feature.py" in diff, "diff 应包含改动文件"
            return {"message": "feat: 新增 feature 模块\n\n- 新增 feature.py 提供示例入口\n- 同步更新说明文档",
                    "elapsed_seconds": 1.2}

        llm_mod.generate_commit_message = fake_ok
        r = state.generate_commit_message({"name": "proj"})
        assert r["ok"] is True, r
        assert r["message"].startswith("feat: 新增 feature 模块"), r
        assert "新增 feature.py" in r["message"], "要点正文应保留"
        print("ok: 模型成功生成多行 message（标题 + 要点正文）")

        # 2) 模型失败：ok:false 不打断页面
        def fake_fail(provider, base_url, api_key, model, diff, timeout_seconds,
                      enable_thinking=False, max_tokens=4096):
            raise llm_mod.LLMError("连接超时")

        llm_mod.generate_commit_message = fake_fail
        r2 = state.generate_commit_message({"name": "proj"})
        assert r2["ok"] is False and "连接超时" in r2["message"], r2
        print("ok: 模型失败返回 ok:false 不抛异常")
    finally:
        llm_mod.generate_commit_message = orig
        shutil.rmtree(tmp, ignore_errors=True)


def check_multi_file_coverage() -> None:
    """大改动覆盖：多文件 diff 全部进入 prompt（不再整体截断丢后面文件）。"""
    calls = {}

    class _FakeClient:
        def __init__(self, cfg):
            calls["cfg"] = cfg

        def _post(self, url, payload, headers):
            calls["prompt"] = payload["messages"][0]["content"]
            return {"choices": [{"message": {"content": "feat: 多文件改动"}}]}

    orig_client = llm_mod.LLMClient
    llm_mod.LLMClient = _FakeClient
    try:
        # 12 个文件，每个 diff 2KB：旧实现整体截 12KB 只能看到前 ~6 个文件
        files = [f"mod_{i:02d}.py" for i in range(12)]
        chunks = []
        for name in files:
            chunks.append(f"diff --git a/{name} b/{name}\n"
                          f"index 000..111\n--- a/{name}\n+++ b/{name}\n"
                          f"@@ -1 +1 @@\n+{name} changed " + "x" * 2000)
        diff = "\n".join(chunks)
        r = generate_commit_message("openai", "http://x/v1", "k", "m", diff, 30)
        assert r["message"], r
        prompt = calls["prompt"]
        missing = [n for n in files if n not in prompt]
        assert not missing, f"以下文件未进入 prompt（覆盖不全）: {missing}"
        # 文件清单应在（模型据此知道全部改动）
        assert "Changed files" in prompt and prompt.count("- mod_") >= 12
        print("ok: 多文件大改动全覆盖进入 prompt（12/12 文件）")
    finally:
        llm_mod.LLMClient = orig_client


def check_over_budget_truncation() -> None:
    calls = {}

    class _FakeClient:
        def __init__(self, cfg):
            pass

        def _post(self, url, payload, headers):
            calls["prompt"] = payload["messages"][0]["content"]
            return {"choices": [{"message": {"content": "fix: 修复"}}]}

    orig_client = llm_mod.LLMClient
    llm_mod.LLMClient = _FakeClient
    try:
        # 单文件 200KB：超出 48K 总预算 → 整体截断且带提示
        r = generate_commit_message("openai", "http://x/v1", "k", "m",
                                    "x" * 200000, 30)
        assert r["message"], r
        assert len(calls["prompt"]) < 60000, "prompt 应在预算内截断"
        assert "已截断" in calls["prompt"], "截断提示应在"
        print("ok: 超预算单文件 diff 截断且带提示")
    finally:
        llm_mod.LLMClient = orig_client


def main() -> int:
    check_clean_workspace()
    check_unknown_project()
    check_success_and_failure()
    check_multi_file_coverage()
    check_over_budget_truncation()
    print("\nAI 生成提交说明全部测试通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
