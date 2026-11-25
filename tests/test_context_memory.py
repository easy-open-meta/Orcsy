"""上下文记忆 Phase 2 自检：摘要式压缩、估算修正、项目经验记忆。

纯离线：摘要请求用替身函数注入，不访问网络。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.config import LLMConfig, ProjectConfig
from orcsy.llm import (LLMClient, _SUMMARY_MARK, _TRIM_PLACEHOLDER,
                       _est_tokens, _est_tokens_msg)
from orcsy.prompts import (NOTES_FILE, NOTES_MAX_CHARS, build_system_prompt)
from orcsy.tools import ToolContext, dispatch_tool
from orcsy.tools_memory import (ARCHIVE_SUMMARY_MAX_CHARS, LONG_TERM_FILE,
                                LONG_TERM_MAX_CHARS, NOTES_MAX_FILE_CHARS)


def _client(**overrides) -> LLMClient:
    cfg = LLMConfig(provider="openai", api_key="sk-test",
                    base_url="http://127.0.0.1:9/v1", model="fake-model",
                    **overrides)
    return LLMClient(cfg)


def _tool_pair(idx: int, content: str, name: str = "read_file") -> list[dict]:
    return [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": f"call_{idx}", "type": "function",
                         "function": {"name": name, "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": f"call_{idx}", "content": content},
    ]


def check_estimator() -> None:
    en = "a" * 400
    zh = "中" * 400
    en_tokens = _est_tokens(en)
    zh_tokens = _est_tokens(zh)
    assert en_tokens == 100, en_tokens
    assert zh_tokens > en_tokens, (zh_tokens, en_tokens)
    assert 200 <= zh_tokens <= 320, zh_tokens
    assert _est_tokens("") == 0
    assert _est_tokens_msg({"role": "user", "content": "中" * 100}) > 50
    print(f"ok: token 估算（ascii={en_tokens} / cjk={zh_tokens}，中文不再被低估一半）")


def check_trim_with_summary() -> None:
    client = _client()
    client.messages = [{"role": "system", "content": "sys"},
                       {"role": "user", "content": "任务正文"}]
    # 25 组工具调用，每组结果 4000 个汉字 ≈ 2800 tokens，总估算 > 64K 软预算
    for i in range(25):
        client.messages.extend(_tool_pair(i, "中" * 4000, "read_file"))
    client.messages.append({"role": "assistant", "content": "继续"})

    prompts: list[str] = []

    def fake_summary(prompt: str) -> str:
        prompts.append(prompt)
        return "已读 src/app.py：路由 /api/x 存在；测试命令 `uv run pytest` 通过。"

    client._request_summary = fake_summary  # type: ignore[method-assign]
    total_before = sum(_est_tokens_msg(m) for m in client.messages)
    client._trim_context()
    total_after = sum(_est_tokens_msg(m) for m in client.messages)

    assert len(prompts) == 1, f"一次裁剪应只发一次摘要请求，实际 {len(prompts)}"
    assert "src/app.py" not in prompts[0].split("<tool_outputs>")[0]
    assert "中中中" in prompts[0], "摘要请求应携带待裁结果原文片段"
    assert total_after < total_before, (total_before, total_after)

    placeholders = [m for m in client.messages
                    if m.get("role") == "tool" and m.get("content") == _TRIM_PLACEHOLDER]
    assert len(placeholders) >= 5, len(placeholders)

    last = client.messages[-1]
    assert last["role"] == "user" and last["content"].startswith(_SUMMARY_MARK), last
    assert "uv run pytest" in last["content"]

    # 最近的 30 条消息保持原文（摘要消息追加在其后）
    recent = client.messages[-31:-1]
    assert any(m.get("role") == "tool" and len(str(m.get("content"))) > 2500
               for m in recent), "最近消息不应被裁剪"

    # tool_call / tool_result 配对完整
    for i, m in enumerate(client.messages):
        if m.get("role") == "tool":
            prev = client.messages[i - 1]
            ids = [c["id"] for c in (prev.get("tool_calls") or [])]
            assert m["tool_call_id"] in ids, f"第 {i} 条配对被破坏"
    print(f"ok: 摘要式压缩（裁剪 {len(placeholders)} 条，估算 "
          f"{total_before}→{total_after} tokens，配对完整）")


def check_trim_fallback() -> None:
    client = _client()
    client.messages = [{"role": "system", "content": "sys"},
                       {"role": "user", "content": "任务正文"}]
    for i in range(25):
        client.messages.extend(_tool_pair(i, "测试输出" + "中" * 4000, "run_command"))
    client.messages.append({"role": "assistant", "content": "继续"})
    client._request_summary = lambda prompt: ""  # type: ignore[method-assign]
    client._trim_context()
    last = client.messages[-1]
    assert last["role"] == "user" and last["content"].startswith(_SUMMARY_MARK), last
    assert "run_command" in last["content"], "降级摘要应带工具名"
    assert "测试输出" in last["content"], "降级摘要应带结果开头片段"
    print("ok: 摘要失败降级为确定性摘要（工具名 + 片段）")


def check_text_protocol_trim() -> None:
    client = _client(tool_protocol="text")
    client.messages = [{"role": "system", "content": "sys"},
                       {"role": "user", "content": "任务正文"}]
    for i in range(25):
        client.messages.append({"role": "assistant", "content": "调用工具"})
        client.messages.append({"role": "user",
                                "content": f"【工具结果 call_{i}】\n" + "中" * 4000})
    client.messages.append({"role": "user", "content": "请继续"})
    client._request_summary = lambda prompt: "文本协议摘要"  # type: ignore[method-assign]
    client._trim_context()
    trimmed = [m for m in client.messages
               if str(m.get("content")) == _TRIM_PLACEHOLDER]
    assert trimmed, "文本协议下以 user 承载的工具结果也应被裁剪"
    assert client.messages[-1]["content"].startswith(_SUMMARY_MARK)
    print("ok: 文本工具协议下的工具结果同样纳入裁剪")


def check_notes_tool() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="ctx-memory-"))
    try:
        ctx = ToolContext(root=tmp)
        out = dispatch_tool(ctx, "update_project_notes", json.dumps(
            {"mode": "replace", "content": "- Tests: `uv run python tests/run_all.py`"}))
        assert "整体重写" in out, out
        notes = tmp / NOTES_FILE
        assert notes.is_file() and "run_all" in notes.read_text(encoding="utf-8")

        out = dispatch_tool(ctx, "update_project_notes", json.dumps(
            {"mode": "append", "content": "- Never run bare `python` on Windows (Store stub)"}))
        assert "追加" in out, out
        text = notes.read_text(encoding="utf-8")
        assert "Store stub" in text and "run_all" in text

        too_long = dispatch_tool(ctx, "update_project_notes", json.dumps(
            {"mode": "append", "content": "x" * 601}))
        assert too_long.startswith("错误") and "过长" in too_long, too_long

        bad_mode = dispatch_tool(ctx, "update_project_notes", json.dumps(
            {"mode": "delete", "content": "x"}))
        assert bad_mode.startswith("错误")

        # 只写固定文件：目录下不应出现其他产物
        names = sorted(p.name for p in tmp.iterdir())
        assert names == [NOTES_FILE], names
        print("ok: update_project_notes 追加/重写/长度上限/固定文件边界")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_notes_archive() -> None:
    """满 8000 字符 → 提示归档 → archive 写长期记忆并精简 → append 恢复。"""
    tmp = Path(tempfile.mkdtemp(prefix="ctx-archive-"))
    try:
        ctx = ToolContext(root=tmp)
        notes_path = tmp / NOTES_FILE
        # 模拟长期积累：直接铺满（走工具需要几十次 append，测试无意义）
        notes_path.write_text(
            "\n".join(f"- lesson {i}: {'x' * 60}" for i in range(150)) + "\n",
            encoding="utf-8")
        assert len(notes_path.read_text(encoding="utf-8")) > 7000

        out = dispatch_tool(ctx, "update_project_notes", json.dumps(
            {"mode": "append", "content": "- new pitfall: port 8080 busy"}))
        assert out.startswith("错误") and "archive" in out, out
        assert str(NOTES_MAX_FILE_CHARS) in out
        assert "长期记忆" not in out or LONG_TERM_FILE in out

        summary = "- Tests: `uv run pytest`\n- Do not run bare `python` (Store stub)"
        out = dispatch_tool(ctx, "update_project_notes", json.dumps(
            {"mode": "archive", "content": summary}))
        assert "已归档" in out and "精简" in out, out

        long_term = tmp / LONG_TERM_FILE
        assert long_term.is_file()
        lt_text = long_term.read_text(encoding="utf-8")
        assert "长期记忆" in lt_text and "归档" in lt_text
        assert "uv run pytest" in lt_text
        assert notes_path.read_text(encoding="utf-8").strip() == summary, \
            "归档后 PROJECT_NOTES.md 应精简为该摘要"

        out = dispatch_tool(ctx, "update_project_notes", json.dumps(
            {"mode": "append", "content": "- Node 20 required for npm ci"}))
        assert "追加" in out, out
        assert "npm ci" in notes_path.read_text(encoding="utf-8")

        # 空文件归档 / 摘要超长
        notes_path.write_text("", encoding="utf-8")
        out = dispatch_tool(ctx, "update_project_notes", json.dumps(
            {"mode": "archive", "content": summary}))
        assert out.startswith("错误") and "为空" in out, out
        notes_path.write_text("- x\n", encoding="utf-8")
        out = dispatch_tool(ctx, "update_project_notes", json.dumps(
            {"mode": "archive", "content": "x" * (ARCHIVE_SUMMARY_MAX_CHARS + 1)}))
        assert out.startswith("错误") and "过长" in out, out

        # 长期记忆超 20000 字符：自动裁剪最旧段落，最新归档保留
        old_sections = "".join(
            f"## 2020-01-{i + 1:02d} 归档（旧）\n{'y' * 1900}\n\n" for i in range(12))
        long_term.write_text(
            "# 长期记忆（LONG_TERM_MEMORY.md）\n\n" + old_sections, encoding="utf-8")
        notes_path.write_text("- placeholder lesson\n", encoding="utf-8")
        out = dispatch_tool(ctx, "update_project_notes", json.dumps(
            {"mode": "archive", "content": "- latest summary marker"}))
        assert "已自动裁剪最旧" in out, out
        lt_text = long_term.read_text(encoding="utf-8")
        assert len(lt_text) <= LONG_TERM_MAX_CHARS, len(lt_text)
        assert "latest summary marker" in lt_text
        assert "2020-01-01" not in lt_text, "最旧段落应被裁剪"
        print("ok: 经验记忆满额归档（超限提示 → archive → 精简 → 继续 append → 长期记忆裁剪）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_notes_prompt() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="ctx-prompt-"))
    try:
        proj = ProjectConfig(name="notes", path=tmp)
        text = "- Tests: uv run python tests/run_all.py\n- Do not run `npm audit fix --force`"
        prompt = build_system_prompt(proj, "# guide", False, notes=text)
        assert "项目经验记忆（PROJECT_NOTES.md）" in prompt
        assert "uv run python tests/run_all.py" in prompt
        assert "update_project_notes" in prompt and "维护约定" in prompt

        absent = build_system_prompt(proj, "# guide", False)
        assert "<project_notes>" not in absent

        long_prompt = build_system_prompt(proj, "# guide", False,
                                          notes="N" * (NOTES_MAX_CHARS + 300))
        assert "已截断" in long_prompt
        assert "N" * (NOTES_MAX_CHARS + 1) not in long_prompt

        # 新经验追加在文件末尾：超长时尾部必须仍然注入（不能只取前 N 字符）
        long_notes = "HEAD-" + "a" * 2000 + "-TAIL"
        tail_prompt = build_system_prompt(proj, "# guide", False, notes=long_notes)
        assert "HEAD-" in tail_prompt, "前段稳定约定应保留"
        assert "-TAIL" in tail_prompt, "最新追加的尾部经验必须注入"

        # 长期记忆存在时给按需读取提示；没有则不出现该提示
        # （归档约定里始终会提到 LONG_TERM_MEMORY.md，这里只验证指针句）
        lt_prompt = build_system_prompt(proj, "# guide", False, notes=text,
                                        long_term=True)
        assert "需要更早的经验" in lt_prompt and "read_file" in lt_prompt
        assert "需要更早的经验" not in prompt
        only_lt = build_system_prompt(proj, "# guide", False, long_term=True)
        assert "需要更早的经验" in only_lt, "notes 为空但存在长期记忆时也应提示"
        print("ok: 经验记忆注入（首段+尾部保留）/截断/缺省省略/维护约定/长期记忆指针")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_estimator()
    check_trim_with_summary()
    check_trim_fallback()
    check_text_protocol_trim()
    check_notes_tool()
    check_notes_archive()
    check_notes_prompt()
    print("\n上下文记忆 Phase 2 自检全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
