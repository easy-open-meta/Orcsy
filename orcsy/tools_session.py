"""批次共享剪贴板工具（Phase 1）：session_share / session_read。

只暴露轻量键值，不含批次生命周期逻辑（见 `orcsy/session_bus.py`）。
工具上下文未注入 `session_bus`（单元测试直构/无批次）时返回明确错误，
不抛异常打断 Agent 循环。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .logging_setup import get_logger

if TYPE_CHECKING:
    from .tools import ToolContext

log = get_logger("tools.session")


def _bus(ctx: object):
    bus = getattr(ctx, "session_bus", None)
    if bus is None:
        return None
    return bus


def tool_session_share(ctx: ToolContext, args: dict) -> str:
    """把一个非代码运行态值写入当前需求批次的临时共享剪贴板。"""
    bus = _bus(ctx)
    if bus is None:
        return ("错误：当前任务没有批次共享上下文（未注入 session_bus），"
                "无法共享；请直接用任务描述或上游交付摘要传递信息")
    key = str(args.get("key") or "")
    value = args.get("value")
    try:
        r = bus.share(key, value)
    except ValueError as e:
        return f"错误：{e}"
    except Exception as e:  # noqa: BLE001 — 状态盘异常不拖垮任务
        log.warning("写入批次共享剪贴板失败：%s", e, exc_info=True)
        return f"错误：写入批次共享失败：{e}"
    return (
        f"已共享 {r['key']}（批次 {bus.batch_id}，当前 {r['count']} 项）。"
        "同批次的下游任务（如 QA 联调）可用 session_read 直接读取；"
        "需求验收归档或终止后会自动清除。"
    )


def tool_session_read(ctx: ToolContext, args: dict) -> str:
    """读取当前需求批次共享剪贴板中的一个键；key 留空时列出可用键。"""
    bus = _bus(ctx)
    if bus is None:
        return "错误：当前任务没有批次共享上下文（未注入 session_bus），无法读取"
    key = str(args.get("key") or "").strip()
    try:
        if not key:
            keys = bus.keys()
            if not keys:
                return f"批次 {bus.batch_id} 的共享剪贴板为空（尚无 session_share 记录）"
            lines = [
                f"- {k['key']}（{k['chars']} 字符，{k['updated_by'] or 'agent'} 于 "
                f"{k['updated_at'] or '?'} 写入）"
                for k in keys
            ]
            return (f"批次 {bus.batch_id} 当前有 {len(keys)} 个共享键：\n"
                    + "\n".join(lines)
                    + "\n\n用 session_read(key=...) 读取具体值。")
        value = bus.read(key)
        if value is None:
            keys = bus.keys()
            avail = "、".join(k["key"] for k in keys) or "（无）"
            return f"错误：批次 {bus.batch_id} 中没有键 {key!r}。当前可用键：{avail}"
        return value
    except ValueError as e:
        return f"错误：{e}"
    except Exception as e:  # noqa: BLE001
        log.warning("读取批次共享剪贴板失败：%s", e, exc_info=True)
        return f"错误：读取批次共享失败：{e}"


SESSION_TOOLS: dict[str, tuple[str, dict, object]] = {
    "session_share": (
        "把临时运行态信息（本地服务端口、测试账号、动态 JWT、临时 DSN 等）写入"
        "当前需求批次的共享剪贴板，供同批次的下游角色（如 QA 联调）读取；"
        "只放非代码临时状态，不要放长期密钥或大段文本。需求验收/终止后自动清除。",
        {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "键名（1~64 位字母/数字/下划线/点/连字符），如 backend_port、qa_token",
                },
                "value": {
                    "type": "string",
                    "description": "要共享的值（≤8000 字符），如 http://127.0.0.1:5173、登录测试账号",
                },
            },
            "required": ["key", "value"],
        },
        tool_session_share,
    ),
    "session_read": (
        "读取当前需求批次共享剪贴板：给 key 返回对应值；key 留空时列出本批次"
        "已共享的键（不返回敏感值）。用于 QA 获取上游开发写入的端口/测试凭证。",
        {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "要读取的键名；留空则列出所有可用键",
                },
            },
        },
        tool_session_read,
    ),
}

__all__ = ["SESSION_TOOLS", "tool_session_read", "tool_session_share"]
