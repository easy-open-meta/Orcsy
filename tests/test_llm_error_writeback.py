"""大模型 429/500 时的任务回写自检：任务原内容不被错误信息"覆盖"。

背景：结果节固定插在正文最前；早期实现把 HTTP 429/500 的错误文本也写进
结果节，导致任务原描述被顶到下面——看板描述/文件开头看起来像被错误覆盖。
现约定：基础设施错误只标状态 + 进度留痕，正文原样不动；模型自述的 blocked
（有 summary）仍写结果节。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.agent import ProjectAgent
from orcsy.config import LLMConfig, ProjectConfig
from orcsy.llm import LLMClient, LLMError

BODY = """## 现象

原始内容标记 ABC123 必须保留

## 验收标准

- 保留我
"""


def make_project(root: Path) -> tuple[ProjectConfig, Path]:
    proj_dir = root / "p"
    (proj_dir / "tasks").mkdir(parents=True)
    (proj_dir / "AGENT.md").write_text("# 指引\n", encoding="utf-8")
    f = proj_dir / "tasks" / "P2-t.md"
    f.write_text(f"---\ntitle: 复现\npriority: P2\nstatus: open\n---\n\n{BODY}",
                 encoding="utf-8")
    return ProjectConfig(name="p", path=proj_dir), f


def run_agent(proj: ProjectConfig, post) -> str:
    orig = LLMClient._post
    LLMClient._post = post
    try:
        agent = ProjectAgent(proj, LLMConfig(api_key="k",
                                             base_url="http://localhost:9",
                                             model="m"))
        results = agent.scan_and_work()
        return results[0].outcome if results else ""
    finally:
        LLMClient._post = orig


def check_timeout_message() -> None:
    """超时错误要带可行动指引（去哪儿调大、任务怎么恢复），不再是裸 timed out。

    背景：真实跑批中思考型模型 + 长上下文单轮超过 timeout_seconds（默认 300s），
    用户只看到「请求大模型失败：timed out」无从下手。
    """
    import httpx
    from orcsy.llm import LLMClient

    cfg = LLMConfig(api_key="k", base_url="http://localhost:9", model="m",
                    timeout_seconds=300)
    client = LLMClient(cfg)
    orig = httpx.post

    def fake_timeout(*a, **kw):
        raise httpx.ReadTimeout("timed out")
    httpx.post = fake_timeout
    try:
        client._post("http://localhost:9/v1/chat/completions", {}, {})
        raise AssertionError("应抛 LLMError")
    except LLMError as e:
        msg = str(e)
        assert "请求大模型超时" in msg and "300 秒" in msg, msg
        assert "超时秒数" in msg and "blocked" in msg, msg
    finally:
        httpx.post = orig

    # 非超时类网络错误保持原文案（不误挂超时指引）
    def fake_conn(*a, **kw):
        raise httpx.ConnectError("connection refused")
    httpx.post = fake_conn
    try:
        client._post("http://localhost:9/v1/chat/completions", {}, {})
        raise AssertionError("应抛 LLMError")
    except LLMError as e:
        assert "请求大模型失败" in str(e) and "超时秒数" not in str(e), str(e)
    finally:
        httpx.post = orig
    print("ok: 超时错误带可行动指引（设置页超时秒数/blocked 重置重跑）")


def main() -> int:
    check_timeout_message()
    for code in (429, 500):
        tmp = Path(tempfile.mkdtemp(prefix="llmerr-"))
        try:
            proj, f = make_project(tmp)

            def fake_post(self, url, payload, headers, _c=code):
                raise LLMError(f"大模型返回 HTTP {_c}：rate limited / 网关错误")
            outcome = run_agent(proj, fake_post)
            text = f.read_text(encoding="utf-8")
            assert outcome == "blocked", outcome
            # 原内容完整、错误信息不写进结果节
            assert "原始内容标记 ABC123" in text and "- 保留我" in text
            assert "## Agent 处理结果" not in text, "基础设施错误不应写结果节"
            assert "status: blocked" in text
            # 错误只在进度节留痕
            prog = text.split("## 执行进度")[1]
            assert "调用大模型失败" in prog and f"HTTP {code}" in prog, prog
            # frontmatter 之后紧跟的就是原内容（不再被结果节顶下去）
            body_after_fm = text.split("---", 2)[2].lstrip("\n")
            assert body_after_fm.startswith("## 现象"), body_after_fm[:40]
            print(f"ok: {code} → 状态 blocked、正文原样、错误仅入执行进度")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # 对照：模型自己给出 blocked 结论（有 summary）仍应写结果节
    tmp = Path(tempfile.mkdtemp(prefix="llmerr-ok-"))
    try:
        proj, f = make_project(tmp)

        def fake_blocked(self, url, payload, headers):
            return {"choices": [{
                "message": {"role": "assistant", "content": None,
                            "tool_calls": [{"id": "c1", "type": "function",
                                            "function": {"name": "finish",
                                                         "arguments": json.dumps(
                                                             {"action": "blocked",
                                                              "summary": "依赖缺失，无法继续",
                                                              "notes": "缺 X"})}}]},
                            "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        outcome = run_agent(proj, fake_blocked)
        text = f.read_text(encoding="utf-8")
        assert outcome == "blocked", outcome
        assert "## Agent 处理结果" in text and "依赖缺失" in text
        assert "原始内容标记 ABC123" in text
        print("ok: 模型自述 blocked → 结果节照写、原内容保留")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n大模型错误回写自检全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
