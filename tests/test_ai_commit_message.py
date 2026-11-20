"""AI 生成提交说明（/api/commit-message）回归。

覆盖：
- 工作区干净（无 diff）→ ok:false 且提示干净状态
- 模型调用成功 → message 填入（支持多行：标题 + 要点正文）
- 模型调用失败 → ok:false 不抛异常（页面不打断）
- 未知项目 → ValueError
- 大改动覆盖：多文件 diff 全部进入 prompt（不再整体截断丢文件）
- 超预算截断（generate_commit_message 单元层）
- 两种协议的超时配置透传（含不超时）及专属超时提示
- Web 生成失败不修改任务状态与工作区
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

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
update:
  auto_check: false
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


def _commit_response() -> httpx.Response:
    message = "feat: 汇总当前改动\n\n- 保留多行提交说明"
    return httpx.Response(200, json={
        "choices": [{"message": {"content": message}}],
        "content": [{"type": "text", "text": message}],
    })


def check_generation_timeout_values() -> None:
    """完整请求链路遵循配置，不把生成请求当作短连接探活。"""
    with patch.object(httpx, "post", return_value=_commit_response()) as post:
        for provider in ("openai", "anthropic"):
            for seconds, expected in ((-1, None), (0, None), (300, 300),
                                      (600, 600), (900, 900), (30, 30), (1, 1)):
                result = generate_commit_message(
                    provider, "http://localhost:9/v1", "k", "m",
                    "diff --git a/a.py b/a.py\n+changed", seconds)
                actual = post.call_args.kwargs["timeout"]
                assert actual == expected, (provider, seconds, actual)
                assert "保留多行提交说明" in result["message"], result
    print("ok: 两种协议生成请求遵循超时配置（长超时原样 / -1 与 0 不超时）")


def check_generation_timeout_errors() -> None:
    """保留重试次数，但手动生成不得显示任务受阻/强制停止指引。"""
    with patch.object(llm_mod, "_LLM_RETRY_BACKOFF", (0, 0, 0)):
        for provider in ("openai", "anthropic"):
            for seconds in (900, -1):
                with patch.object(httpx, "post", side_effect=httpx.ReadTimeout("timed out")) as post:
                    try:
                        generate_commit_message(
                            provider, "http://localhost:9/v1", "k", "m",
                            "diff --git a/a.py b/a.py\n+changed", seconds)
                        raise AssertionError("超时应返回 LLMError")
                    except llm_mod.LLMError as e:
                        assert isinstance(e, llm_mod.LLMTimeoutError), type(e)
                        assert e.timeout_seconds == seconds and e.attempts == 4
                        text = str(e)
                        assert "AI 生成提交说明超时" in text and "4 次尝试" in text, text
                        assert "手动填写" in text and "超时秒数" in text, text
                        assert not any(s in text for s in ("blocked", "重置为待办", "强制停止")), text
                        if seconds > 0:
                            assert "900 秒" in text, text
                        else:
                            assert "-1 秒内" not in text, text
                    assert post.call_count == 4, post.call_count
    print("ok: 生成超时保留重试信息，不误报任务 blocked 或强制停止")


def check_api_timeout_configuration_and_safety() -> None:
    tmp, state, proj = _make_env()
    try:
        (proj / "feature.py").write_text("print('pending')\n", encoding="utf-8")
        task = proj / "tasks" / "P2-pending.md"
        task.parent.mkdir(exist_ok=True)
        task.write_text("---\ntitle: 保留待办状态\npriority: P2\nstatus: open\n---\n\n尚未执行\n",
                        encoding="utf-8")
        before = {p.relative_to(proj): p.read_bytes() for p in proj.rglob("*")
                  if p.is_file() and ".git" not in p.relative_to(proj).parts}
        with patch.object(httpx, "post", return_value=_commit_response()) as post:
            for seconds, expected in ((-1, None), (900, 900)):
                state.config.llm.timeout_seconds = seconds
                result = state.generate_commit_message({"name": "proj"})
                assert result["ok"] is True, result
                assert post.call_args.kwargs["timeout"] == expected
        with (patch.object(llm_mod, "_LLM_RETRY_BACKOFF", (0, 0, 0)),
              patch.object(httpx, "post", side_effect=httpx.ReadTimeout("timed out")) as post):
            result = state.generate_commit_message({"name": "proj"})
            assert result["ok"] is False, result
            assert "AI 生成提交说明超时" in result["message"], result
            assert "blocked" not in result["message"], result
            assert post.call_count == 4
        after = {p.relative_to(proj): p.read_bytes() for p in proj.rglob("*")
                 if p.is_file() and ".git" not in p.relative_to(proj).parts}
        assert after == before, "手动生成失败不能修改工作区或任务状态"
        print("ok: Web API 透传超时配置，生成失败不修改任务与工作区")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_clean_workspace()
    check_unknown_project()
    check_success_and_failure()
    check_multi_file_coverage()
    check_over_budget_truncation()
    check_generation_timeout_values()
    check_generation_timeout_errors()
    check_api_timeout_configuration_and_safety()
    print("\nAI 生成提交说明全部测试通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
