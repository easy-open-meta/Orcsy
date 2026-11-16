"""项目架构黑板报工具（Phase 1）：update_architecture。

只允许写当前工作根的 `ARCHITECTURE.md` 一个文件，避免为了维护黑板报而
给 PM/QA 开放任意文件写入权限。PM 首次梳理用 replace；开发角色重大接口变更
用 append（单次 ≤2000 字，提示词约定 ≤200 字）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .logging_setup import get_logger

if TYPE_CHECKING:
    from .tools import ToolContext

log = get_logger("tools.memory")

ARCHITECTURE_FILE = "ARCHITECTURE.md"
MAX_REPLACE_CHARS = 6000
MAX_APPEND_CHARS = 2000
MAX_FILE_CHARS = 16000


def tool_update_architecture(ctx: ToolContext, args: dict) -> str:
    """创建/更新/追加项目根 ARCHITECTURE.md（架构黑板报）。"""
    content = str(args.get("content") or "").strip()
    mode = str(args.get("mode") or "append").strip().lower()
    if mode not in ("append", "replace"):
        return "错误：mode 只能是 append（追加）或 replace（整体重写）"
    if not content:
        return "错误：content 不能为空"
    limit = MAX_REPLACE_CHARS if mode == "replace" else MAX_APPEND_CHARS
    if len(content) > limit:
        return f"错误：content 过长（{len(content)} > {limit} 字符）"

    path = ctx.root / ARCHITECTURE_FILE
    try:
        if mode == "replace":
            text = content.rstrip() + "\n"
        else:
            old = path.read_text(encoding="utf-8") if path.is_file() else ""
            if old and not old.endswith("\n"):
                old += "\n"
            text = old + ("\n" if old else "") + content.rstrip() + "\n"
        if len(text) > MAX_FILE_CHARS:
            return (f"错误：黑板报将超过 {MAX_FILE_CHARS} 字符上限；"
                    "请先精简内容，或由 PM 用 mode=replace 重写")
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        log.warning("写架构黑板报失败：%s", e)
        return f"错误：写架构黑板报失败：{e}"
    action = "整体重写" if mode == "replace" else "追加"
    return (f"已{action}架构黑板报 {ARCHITECTURE_FILE}（当前 {len(text)} 字符）。"
            "后续任务启动时会自动只读注入该文件的前 3000 字符。")


MEMORY_TOOLS: dict[str, tuple[str, dict, object]] = {
    "update_architecture": (
        "维护项目根 ARCHITECTURE.md（全局架构黑板报）。PM 首次拆解需求时用 "
        "mode=replace 写入核心分层/模块边界/技术选型/公共数据模型；开发角色完成"
        "重大接口或数据表变更后用 mode=append 追加简短增量（关键路由、"
        "数据模型、文件导引，≤200 字符）。content 请用英文书写以节省 token"
        "（代码路径/接口名/标识符保留原文）。仅用于长期架构事实，不写任务日志/"
        "临时端口/调试过程。",
        {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "黑板报内容；replace 为全文（≤6000 字），append 为增量（≤2000 字，建议 ≤200 字）",
                },
                "mode": {
                    "type": "string",
                    "enum": ["append", "replace"],
                    "description": "append = 追加（默认）；replace = 整体重写",
                },
            },
            "required": ["content"],
        },
        tool_update_architecture,
    ),
}

__all__ = ["MEMORY_TOOLS", "tool_update_architecture"]
