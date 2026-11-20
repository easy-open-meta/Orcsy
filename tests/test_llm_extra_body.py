"""llm.extra_body 扩展请求体自检：配置解析、载荷合并、项目绑定透传。

背景：部分 vLLM 系中转在 auto 工具选择下不返回结构化 tool_calls（think 混入
正文还会触发 400），需要 {"tool_choice": "required", "enable_thinking": false}
这类逐键开关——extra_body 是通用逃生舱口。

运行：uv run python tests/test_llm_extra_body.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

from orcsy.config import LLMConfig, _coerce_llm, resolve_llm
from orcsy.llm import LLMClient


def check_coerce() -> None:
    cfg = _coerce_llm({
        "api_key": "sk-x", "base_url": "https://api.example.com/v1",
        "model": "m", "extra_body": {"tool_choice": "required",
                                     "enable_thinking": False},
    })
    assert cfg.extra_body == {"tool_choice": "required",
                              "enable_thinking": False}, cfg.extra_body

    # 缺省 = 空 dict（不透传任何键）
    assert _coerce_llm({"api_key": "sk-x"}).extra_body == {}

    # 非对象（列表/字符串/数值）立即报错且带上下文
    for bad in ([1, 2], "required", 3):
        try:
            _coerce_llm({"api_key": "sk-x", "extra_body": bad})
            raise AssertionError(f"extra_body={bad!r} 应报错")
        except ValueError as e:
            assert "extra_body" in str(e), e
    print("_coerce_llm 解析/校验 ✅")


def check_payload_merge() -> None:
    cfg = LLMConfig(api_key="sk-x",
                    extra_body={"tool_choice": "required",
                                "enable_thinking": False})
    client = LLMClient(cfg)
    client.reset("system")

    # 关闭思考开关 + tools：extra_body 逐键合并且覆盖内置字段
    p = client._payload_openai(True)
    assert p["tool_choice"] == "required", p.get("tool_choice")
    assert p["enable_thinking"] is False, "extra_body 应覆盖 enable_thinking"
    assert "thinking" not in p, "思考关闭时不应带 thinking 对象"
    assert p["tools"] and p["messages"][0]["content"] == "system"

    # 空 extra_body：不引入任何额外键（严格服务零污染）
    p0 = LLMClient(LLMConfig(api_key="sk-x"))._payload_openai(True)
    for k in ("tool_choice", "enable_thinking", "thinking"):
        assert k not in p0, k
    print("_payload_openai 合并/覆盖语义 ✅")


def check_resolve_override() -> None:
    glob = LLMConfig(api_key="sk-g", extra_body={"tool_choice": "required"})
    # 未绑定 extra_body：整体继承
    r = resolve_llm(glob, {"model": "bound"})
    assert r.extra_body == {"tool_choice": "required"}
    # 绑定了：整体替换（dict 写了即覆盖，无字段级深合并）
    r = resolve_llm(glob, {"model": "bound", "extra_body": {"top_p": 0.9}})
    assert r.extra_body == {"top_p": 0.9}, r.extra_body
    print("resolve_llm 项目绑定透传 ✅")


def check_save_roundtrip() -> None:
    """存盘往返：save_config 漏写 extra_body 会让看板任意保存静默重置（实测踩坑）。"""
    import tempfile
    from orcsy.config import AppConfig, load_config, save_config

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "config.yaml"
        p.write_text(
            "llm:\n"
            "  api_key: sk-x\n"
            "  extra_body:\n"
            "    tool_choice: required\n"
            "    enable_thinking: false\n",
            encoding="utf-8",
        )
        cfg = load_config(p)
        assert cfg.llm.extra_body == {"tool_choice": "required",
                                      "enable_thinking": False}
        cfg.web.host = "127.0.0.1"  # 触发一次与 llm 无关的修改后存盘
        save_config(cfg)
        cfg2 = load_config(p)
        assert cfg2.llm.extra_body == {"tool_choice": "required",
                                       "enable_thinking": False}, \
            f"存盘后 extra_body 被静默重置：{cfg2.llm.extra_body!r}"
    print("save_config 存盘往返 ✅")


def check_text_protocol() -> None:
    """tool_protocol=text：请求不带 tools/tool_choice、指令注入、正文解析。"""
    import json as _json
    cfg = LLMConfig(api_key="sk-x", tool_protocol="text")
    client = LLMClient(cfg)
    client.reset("你是负责项目的开发 Agent。")
    client._enabled_tools = frozenset({"read_file", "finish"})

    # 1) 载荷：无 tools、无 tool_choice；extra_body 的 tool_choice 被剥掉
    p = client._payload_openai(True)
    assert "tools" not in p and "tool_choice" not in p, p.keys()

    # 2) 指令注入：system 带协议标记 + 工具清单（幂等）
    client._ensure_text_protocol()
    sys1 = client.messages[0]["content"]
    assert "工具调用协议（文本模式）" in sys1 and "read_file" in sys1
    client._ensure_text_protocol()
    assert client.messages[0]["content"] == sys1, "重复注入应被标记判重拦截"

    # 3) 正文解析：标记块 + think 内联剥离 + 多调用
    body = ('<think>先想想</think>我来写文件。\n<<TOOL_CALLS>>\n'
            '[{"tool": "write_file", "args": {"path": "a.py", "content": "x=1"}},'
            ' {"tool": "finish", "args": {"action": "completed", "summary": "ok"}}]\n'
            '<<END>>')
    clean, calls = LLMClient._parse_text_tool_calls(body)
    assert "<think>" not in clean and "先想想" not in clean
    assert [c[1] for c in calls] == ["write_file", "finish"], calls
    assert _json.loads(calls[0][2])["path"] == "a.py"
    assert _json.loads(calls[1][2])["action"] == "completed"

    # 4) 单对象形态 + 裸 JSON 兜底
    _, c2 = LLMClient._parse_text_tool_calls('{"tool": "finish", "args": {"action": "blocked"}}')
    assert [c[1] for c in c2] == ["finish"]
    _, c3 = LLMClient._parse_text_tool_calls('```json\n[{"tool": "grep", "args": {"pattern": "x"}}]\n```')
    assert [c[1] for c in c3] == ["grep"]

    # 5) 工具结果以用户消息回传（无 tool 角色）
    tr = client._tool_result("call_1", "文件内容")
    assert tr["role"] == "user" and "【工具结果 call_1】" in tr["content"]

    # 6) 未闭合 think（length 截断）→ 无调用，正文空（循环走 nudge 不炸）
    clean6, c6 = LLMClient._parse_text_tool_calls("<think>思考到一半被截断")
    assert clean6 == "" and c6 == []
    print("tool_protocol=text 文本协议 ✅")


def check_protocol_coerce() -> None:
    cfg = _coerce_llm({"api_key": "sk-x", "tool_protocol": "text"})
    assert cfg.tool_protocol == "text"
    assert _coerce_llm({"api_key": "sk-x"}).tool_protocol == "native"
    for bad in ("json", "xml", 1):
        try:
            _coerce_llm({"api_key": "sk-x", "tool_protocol": bad})
            raise AssertionError(f"tool_protocol={bad!r} 应报错")
        except ValueError as e:
            assert "tool_protocol" in str(e), e
    print("tool_protocol 解析/校验 ✅")


if __name__ == "__main__":
    check_coerce()
    check_payload_merge()
    check_resolve_override()
    check_save_roundtrip()
    check_text_protocol()
    check_protocol_coerce()
    print("test_llm_extra_body: 全部通过")
