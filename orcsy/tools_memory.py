"""项目记忆工具（Phase 1/2）：架构黑板报、经验记忆与满额归档。

只允许写固定的记忆文件，避免给 PM/QA 开放任意文件写权限。
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from typing import TYPE_CHECKING

from .logging_setup import get_logger

if TYPE_CHECKING:
    from .tools import ToolContext

log = get_logger("tools.memory")

ARCHITECTURE_FILE = "ARCHITECTURE.md"
MAX_REPLACE_CHARS = 6000
MAX_APPEND_CHARS = 2000
MAX_FILE_CHARS = 16000

NOTES_FILE = "PROJECT_NOTES.md"
NOTES_MAX_REPLACE_CHARS = 3000
NOTES_MAX_APPEND_CHARS = 600
NOTES_MAX_FILE_CHARS = 8000

# 长期记忆：只由 archive 模式写入；文件名保持 ASCII，避免中文路径在 git/归档链路踩坑
LONG_TERM_FILE = "LONG_TERM_MEMORY.md"
ARCHIVE_SUMMARY_MAX_CHARS = 2000
LONG_TERM_MAX_CHARS = 20000
LONG_TERM_HEADER = ("# 长期记忆（LONG_TERM_MEMORY.md）\n\n"
                    "> 由 `PROJECT_NOTES.md` 归档而来：每次归档追加一段带日期的摘要，"
                    "超出 20000 字符时自动裁剪最旧的段落。需要历史经验时用 read_file 读取。\n\n")


def _write_memory_file(ctx: ToolContext, filename: str, content: str, mode: str,
                       *, replace_limit: int, append_limit: int,
                       file_limit: int, overflow_hint: str = "") -> str:
    """只写工作根下固定记忆文件；返回给模型的可读结果（含错误）。"""
    limit = replace_limit if mode == "replace" else append_limit
    if len(content) > limit:
        return f"错误：content 过长（{len(content)} > {limit} 字符）"

    path = ctx.root / filename
    try:
        if mode == "replace":
            text = content.rstrip() + "\n"
        else:
            old = path.read_text(encoding="utf-8") if path.is_file() else ""
            if old and not old.endswith("\n"):
                old += "\n"
            text = old + ("\n" if old else "") + content.rstrip() + "\n"
        if len(text) > file_limit:
            if overflow_hint:
                return overflow_hint
            return (f"错误：{filename} 将超过 {file_limit} 字符上限；"
                    "请先精简内容，再用 mode=replace 重写合并")
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        log.warning("写项目记忆文件失败（%s）：%s", filename, e)
        return f"错误：写 {filename} 失败：{e}"
    action = "整体重写" if mode == "replace" else "追加"
    return f"已{action} {filename}（当前 {len(text)} 字符）。"


def tool_update_architecture(ctx: ToolContext, args: dict) -> str:
    """创建/更新/追加项目根 ARCHITECTURE.md（架构黑板报）。"""
    content = str(args.get("content") or "").strip()
    mode = str(args.get("mode") or "append").strip().lower()
    if mode not in ("append", "replace"):
        return "错误：mode 只能是 append（追加）或 replace（整体重写）"
    if not content:
        return "错误：content 不能为空"
    result = _write_memory_file(
        ctx, ARCHITECTURE_FILE, content, mode,
        replace_limit=MAX_REPLACE_CHARS, append_limit=MAX_APPEND_CHARS,
        file_limit=MAX_FILE_CHARS)
    if result.startswith("错误"):
        return result
    return (f"{result}后续任务启动时会自动只读注入该文件的前 3000 字符。")


def _prune_long_term(text: str) -> tuple[str, int]:
    """长期记忆超出上限时裁剪最旧的归档段落，返回 (新文本, 裁剪段数)。"""
    if len(text) <= LONG_TERM_MAX_CHARS:
        return text, 0
    parts = re.split(r"(?m)^(?=## )", text)
    head = ""
    if parts and not parts[0].startswith("## "):
        head = parts.pop(0)
    dropped = 0
    while parts and len(head) + sum(len(p) for p in parts) > LONG_TERM_MAX_CHARS:
        parts.pop(0)
        dropped += 1
    return head + "".join(parts), dropped


def _archive_notes(ctx: ToolContext, summary: str) -> str:
    """把 notes 全文的归纳摘要归档，并把 notes 精简为该摘要。"""
    notes_path = ctx.root / NOTES_FILE
    try:
        old = notes_path.read_text(encoding="utf-8") if notes_path.is_file() else ""
    except OSError as e:
        return f"错误：读取 {NOTES_FILE} 失败：{e}"
    if not old.strip():
        return f"错误：{NOTES_FILE} 为空，没有可归档的内容"
    if len(summary) > ARCHIVE_SUMMARY_MAX_CHARS:
        return (f"错误：归档摘要过长（{len(summary)} > {ARCHIVE_SUMMARY_MAX_CHARS} 字符）；"
                "请先压缩到 1500 字以内再归档")

    long_path = ctx.root / LONG_TERM_FILE
    try:
        existing = long_path.read_text(encoding="utf-8") if long_path.is_file() else ""
        if not existing.strip():
            existing = LONG_TERM_HEADER
        elif not existing.endswith("\n\n"):
            existing += "\n" if existing.endswith("\n") else "\n\n"
        section = (f"## {date.today().isoformat()} 归档"
                   f"（原 {NOTES_FILE} 共 {len(old)} 字符）\n"
                   f"{summary.rstrip()}\n\n")
        text, dropped = _prune_long_term(existing + section)
        # 先写长期记忆再精简 notes，中途失败不丢原文
        long_path.write_text(text, encoding="utf-8")
        notes_path.write_text(summary.rstrip() + "\n", encoding="utf-8")
    except OSError as e:
        log.warning("归档项目经验记忆失败：%s", e)
        return f"错误：归档失败：{e}"

    tail = f"；已自动裁剪最旧的 {dropped} 段归档" if dropped else ""
    return (f"已归档到 {LONG_TERM_FILE}（当前 {len(text)} 字符{tail}）。"
            f"{NOTES_FILE} 已精简为 {len(summary)} 字符摘要，可继续 append 新经验。")


def tool_update_project_notes(ctx: ToolContext, args: dict) -> str:
    """创建/更新/追加项目根 PROJECT_NOTES.md；满额时归档到长期记忆。"""
    content = str(args.get("content") or "").strip()
    mode = str(args.get("mode") or "append").strip().lower()
    if mode not in ("append", "replace", "archive"):
        return "错误：mode 只能是 append（追加）/ replace（整体重写）/ archive（归档精简）"
    if not content:
        return "错误：content 不能为空"
    if mode == "archive":
        return _archive_notes(ctx, content)
    overflow_hint = (
        f"错误：{NOTES_FILE} 将超过 {NOTES_MAX_FILE_CHARS} 字符上限。"
        f"请先 read_file 读取全文并归纳成 ≤1500 字摘要（保留仍有效的命令/约定，合并重复），"
        f"调用 update_project_notes(mode=\"archive\", content=摘要)：摘要会追加到 "
        f"{LONG_TERM_FILE}，{NOTES_FILE} 会被精简为该摘要；随后再 append 本次新经验。")
    result = _write_memory_file(
        ctx, NOTES_FILE, content, mode,
        replace_limit=NOTES_MAX_REPLACE_CHARS,
        append_limit=NOTES_MAX_APPEND_CHARS, file_limit=NOTES_MAX_FILE_CHARS,
        overflow_hint=overflow_hint)
    if result.startswith("错误"):
        return result
    return (f"{result}后续任务启动时会自动只读注入该文件（首尾共 1500 字符，"
            "新条目在末尾优先可见）。")


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
    "update_project_notes": (
        "维护项目根 PROJECT_NOTES.md（项目经验记忆）。任务收尾前若发现会长期"
        "影响后续任务的经验——测试/构建命令与坑、环境怪癖、依赖约束、易误伤"
        "的操作、验证技巧——用 mode=append 追加 1~3 行（每条 ≤120 字符，英文"
        "优先以省 token）。已有同类条目重复时用 mode=replace 合并重写全文。"
        "append 因超过 8000 字符上限被拒时：先 read_file 读全文，归纳成 ≤1500 字"
        "摘要，用 mode=archive 归档（摘要追加到 LONG_TERM_MEMORY.md，"
        "PROJECT_NOTES.md 精简为该摘要），再 append 新经验。"
        "不写任务日志、临时端口/凭证、当次调试过程或架构事实（后者用 "
        "update_architecture）。",
        {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "经验条目；append 单次 ≤600 字符（建议 1~3 行），replace 为全文 ≤3000 字符，archive 为当前全文的归纳摘要 ≤2000 字符（建议 ≤1500）",
                },
                "mode": {
                    "type": "string",
                    "enum": ["append", "replace", "archive"],
                    "description": "append = 追加（默认）；replace = 整体重写；archive = 把当前全文归纳成摘要归档到 LONG_TERM_MEMORY.md 并精简本文件",
                },
            },
            "required": ["content"],
        },
        tool_update_project_notes,
    ),
}

__all__ = ["MEMORY_TOOLS", "tool_update_architecture", "tool_update_project_notes",
           "ARCHITECTURE_FILE", "NOTES_FILE", "LONG_TERM_FILE"]
