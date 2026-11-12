"""工作区文件系统与代码搜索工具集。

提供在沙箱项目根目录内的安全文件读写、多点编辑、重命名/移动、删除以及 glob/正则搜索。
严格通过 `_resolve_in_root` 拦截任何路径穿越行为，保证 Agent 仅在沙箱内部工作。
"""

from __future__ import annotations

import difflib
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .tools import ToolContext

DEFAULT_READ_LINES = 250
MAX_FILE_READ_CHARS = 12000


def _resolve_in_root(ctx: ToolContext, rel_path: str) -> Path:
    """把相对路径解析到根目录内，拒绝绝对路径与路径穿越。"""
    p = Path(rel_path)
    if p.is_absolute():
        raise ValueError(f"只允许相对路径（相对项目根）：{rel_path}")
    resolved = (ctx.root / p).resolve()
    if ctx.root != resolved and ctx.root not in resolved.parents:
        raise ValueError(f"路径越界，超出项目根目录：{rel_path}")
    return resolved


def _closest_snippet(content: str, old: str, max_lines: int = 10) -> str:
    """文件中与 old_string 最接近的片段（edit 失败时的自纠提示）。

    背景：长任务里模型对「自己刚写的内容」会记忆漂移（把 tx 记成 db、变量
    重构后仍用旧锚点），edit 报「不存在」后模型看不到差异点，容易升级成
    「文件系统不可信」的错误结论并整文件重写——给它看真实片段即可当场自纠。

    锚定策略：取 old_string 里最长的一行当锚（区分度最高），在文件各行里找
    与它最相似的一行，按锚在 old_string 中的位置对齐出等长窗口；整窗相似度
    太低就不提示（宁缺毋滥，避免误导）。O(文件行数) 次短串比较，开销可忽略。
    """
    if not content or not old.strip():
        return ""
    old_lines = old.splitlines()
    if not old_lines:
        return ""
    anchor_idx = max(range(len(old_lines)), key=lambda i: len(old_lines[i].strip()))
    anchor = old_lines[anchor_idx].strip()
    if not anchor:
        return ""
    file_lines = content.splitlines()
    best_ratio, best_i = 0.0, -1
    for i, fl in enumerate(file_lines):
        r = difflib.SequenceMatcher(None, fl.strip(), anchor).ratio()
        if r > best_ratio:
            best_ratio, best_i = r, i
    if best_i < 0 or best_ratio < 0.5:
        return ""
    start = max(0, best_i - anchor_idx)
    window = file_lines[start:start + len(old_lines)]
    whole = difflib.SequenceMatcher(None, "\n".join(window), old).ratio()
    if whole < 0.4:
        return ""
    shown = window
    if len(shown) > max_lines:
        shown = shown[:max_lines - 2] + ["…（中间省略）"] + shown[-2:]
    first_no = start + 1
    last_no = start + len(window)
    body = "\n".join(f"{first_no + k:4d} | {ln}" for k, ln in enumerate(shown))
    return f"第 {first_no}–{last_no} 行（相似度 {int(whole * 100)}%）：\n{body}"


def tool_read_file(ctx: ToolContext, args: dict) -> str:
    """按行分页读取文件：大文件用 offset/limit 取需要的区段，避免整读进上下文。"""
    path = _resolve_in_root(ctx, str(args["path"]))
    if not path.is_file():
        return f"错误：文件不存在 {args['path']}"
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    total = len(lines)
    offset = max(1, int(args.get("offset", 1) or 1))
    limit = max(1, min(int(args.get("limit", DEFAULT_READ_LINES) or DEFAULT_READ_LINES), 1000))
    sel = lines[offset - 1:offset - 1 + limit]
    out = "\n".join(sel)
    if len(out) > MAX_FILE_READ_CHARS:
        out = out[:MAX_FILE_READ_CHARS]
        end = offset - 1 + out.count("\n") + 1
        return (out + f"\n\n…（已达单次读取上限；本次第 {offset}-{end} 行 / 共 {total} 行，"
                f"继续读取用 offset={end + 1}）")
    end = offset - 1 + len(sel)
    notes = []
    if offset > 1 or end < total:
        notes.append(f"第 {offset}-{end} 行 / 共 {total} 行")
        if end < total:
            notes.append(f"继续读取用 offset={end + 1}")
    head = f"[{args['path']}]\n" if offset > 1 else ""
    return head + out + (f"\n\n…（{ '；'.join(notes) }）" if notes else "")


def tool_write_file(ctx: ToolContext, args: dict) -> str:
    path = _resolve_in_root(ctx, str(args["path"]))
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(args.get("content", "")), encoding="utf-8")
    return f"已{'更新' if existed else '创建'} {args['path']}（{len(str(args.get('content', '')))} 字符）"


def tool_edit_file(ctx: ToolContext, args: dict) -> str:
    """精确文本替换：old_string 必须唯一匹配。适合小改动，避免整文件重写。"""
    path = _resolve_in_root(ctx, str(args["path"]))
    if not path.is_file():
        return f"错误：文件不存在 {args['path']}"
    text = path.read_text(encoding="utf-8", errors="replace")
    old, new = str(args["old_string"]), str(args["new_string"])
    count = text.count(old)
    if count == 0:
        msg = "错误：old_string 在文件中不存在（注意空白与换行需完全一致）"
        hint = _closest_snippet(text, old)
        if hint:
            msg += (f"\n文件中最接近的片段，{hint}\n"
                    "请以文件实际内容为准修正 old_string（变量名/空格/标点常是差异点），"
                    "或先 read_file 该区域确认后再编辑；不要凭记忆整文件重写。")
        return msg
    if count > 1:
        return f"错误：old_string 匹配了 {count} 处，请提供更长的上下文使其唯一"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return f"已修改 {args['path']}"


def tool_multi_edit(ctx: ToolContext, args: dict) -> str:
    """一次对同一文件做多处精确替换；全有或全无——任一条失败则文件不落盘。"""
    path = _resolve_in_root(ctx, str(args["path"]))
    if not path.is_file():
        return f"错误：文件不存在 {args['path']}"
    edits = args.get("edits")
    if not isinstance(edits, list) or not edits:
        return "错误：edits 必须是非空数组 [{old_string, new_string}, ...]"
    text = path.read_text(encoding="utf-8", errors="replace")
    for i, e in enumerate(edits, 1):
        if not isinstance(e, dict):
            return f"错误：第 {i} 条 edit 不是对象（文件未改动）"
        old, new = str(e.get("old_string", "")), str(e.get("new_string", ""))
        if not old:
            return f"错误：第 {i} 条 edit 的 old_string 为空（文件未改动）"
        count = text.count(old)
        if count == 0:
            msg = f"错误：第 {i} 条 edit 的 old_string 在文件中不存在（文件未改动）"
            hint = _closest_snippet(text, old)
            if hint:
                msg += (f"\n文件中最接近的片段，{hint}\n"
                        "以文件实际内容修正本条 old_string 后重试"
                        "（全部条目要么一起成功、要么都不落盘）。")
            return msg
        if count > 1:
            return (f"错误：第 {i} 条 edit 的 old_string 匹配了 {count} 处，"
                    f"请提供更长上下文使其唯一（文件未改动）")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")
    return f"已修改 {args['path']}（{len(edits)} 处替换）"


def tool_delete_file(ctx: ToolContext, args: dict) -> str:
    """删除项目内的文件或目录；目录必须显式带 recursive=true，项目根不可删。"""
    path = _resolve_in_root(ctx, str(args["path"]))
    if path == ctx.root:
        return "错误：不能删除项目根目录"
    if path.is_dir():
        if not bool(args.get("recursive", False)):
            return f"错误：{args['path']} 是目录，需带 recursive=true 才能删除"
        shutil.rmtree(path)
        return f"已删除目录 {args['path']}（递归）"
    if not path.exists():
        return f"错误：文件不存在 {args['path']}"
    path.unlink()
    return f"已删除 {args['path']}"


def tool_move_file(ctx: ToolContext, args: dict) -> str:
    """移动/重命名文件或目录，两端都限制在项目根内；目标已存在时需 overwrite=true。"""
    src = _resolve_in_root(ctx, str(args["source"]))
    dst = _resolve_in_root(ctx, str(args["destination"]))
    if src == ctx.root or dst == ctx.root:
        return "错误：不能移动项目根目录，也不能把内容移动到项目根本身"
    if not src.exists():
        return f"错误：源不存在 {args['source']}"
    if dst.exists() and not bool(args.get("overwrite", False)):
        return f"错误：目标已存在 {args['destination']}（覆盖需带 overwrite=true）"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_dir():
        shutil.rmtree(dst)
    shutil.move(str(src), str(dst))
    return f"已移动 {args['source']} → {args['destination']}"


def tool_list_dir(ctx: ToolContext, args: dict) -> str:
    rel = str(args.get("path", "."))
    base = _resolve_in_root(ctx, rel)
    if not base.is_dir():
        return f"错误：目录不存在 {rel}"
    ignores = {".git", ".worktree-arranger", "__pycache__", "node_modules", ".venv", "dist", "build"}
    lines: list[str] = []
    for p in sorted(base.rglob("*")):
        if any(part in ignores for part in p.parts):
            continue
        rel_display = p.relative_to(ctx.root).as_posix()
        lines.append(("d " if p.is_dir() else "f ") + rel_display[:120])
        if len(lines) >= 200:
            lines.append("…（目录条目过多，已截断）")
            break
    return "\n".join(lines) or "（空目录）"


def tool_find_files(ctx: ToolContext, args: dict) -> str:
    """按文件名 glob 查找（如 *.py、config*），大仓库里定位文件比 list_dir 高效。"""
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        return "错误：pattern 不能为空（文件名 glob，如 *.py）"
    rel = str(args.get("path", "."))
    base = _resolve_in_root(ctx, rel)
    if not base.is_dir():
        return f"错误：目录不存在 {rel}"
    ignores = {".git", ".worktree-arranger", "__pycache__", "node_modules", ".venv", "dist", "build"}
    lines: list[str] = []
    for p in sorted(base.rglob(pattern)):
        if any(part in ignores for part in p.parts):
            continue
        lines.append(("d " if p.is_dir() else "f ") + p.relative_to(ctx.root).as_posix()[:120])
        if len(lines) >= 200:
            lines.append("…（匹配过多，已截断）")
            break
    return "\n".join(lines) or "（无匹配）"


def tool_grep(ctx: ToolContext, args: dict) -> str:
    pattern = str(args["pattern"])
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"错误：正则不合法：{e}"
    rel = str(args.get("path", "."))
    base = _resolve_in_root(ctx, rel)
    if not base.is_dir():
        return f"错误：目录不存在 {rel}"
    ignores = {".git", ".worktree-arranger", "__pycache__", "node_modules", ".venv"}
    glob = str(args.get("glob", "*"))
    hits: list[str] = []
    for p in sorted(base.rglob(glob)):
        if not p.is_file() or any(part in ignores for part in p.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{p.relative_to(ctx.root).as_posix()}:{i}: {line.strip()[:200]}")
                if len(hits) >= 100:
                    hits.append("…（匹配过多，已截断）")
                    return "\n".join(hits)
    return "\n".join(hits) or "（无匹配）"


FS_TOOLS: dict[str, tuple[str, dict, object]] = {
    "list_dir": (
        "列出项目内的文件与目录（递归），用于了解项目结构。",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "相对项目根的目录，默认 ."}},
            "required": [],
        },
        tool_list_dir,
    ),
    "read_file": (
        "读取项目内一个文本文件的内容（按行分页）。大文件先用 grep 定位，"
        "再用 offset/limit 读取需要的区段，避免整读。",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对项目根的文件路径"},
                "offset": {"type": "integer", "description": "起始行（1-based），默认 1"},
                "limit": {"type": "integer", "description": "本次读取行数，默认 250，上限 1000"},
            },
            "required": ["path"],
        },
        tool_read_file,
    ),
    "write_file": (
        "创建或整体重写一个文件（UTF-8）。大改用这个；小改动优先用 edit_file。",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对项目根的文件路径"},
                "content": {"type": "string", "description": "完整文件内容"},
            },
            "required": ["path", "content"],
        },
        tool_write_file,
    ),
    "edit_file": (
        "对文件做精确文本替换：old_string 必须在文件中唯一匹配。",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对项目根的文件路径"},
                "old_string": {"type": "string", "description": "要替换的原文（须唯一）"},
                "new_string": {"type": "string", "description": "替换后的文本"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        tool_edit_file,
    ),
    "multi_edit": (
        "对同一文件一次做多处精确替换（全有或全无：任一条 old_string 不唯一或"
        "不存在则整个调用失败，文件不变）。多处小改动优先用它，省轮次。",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对项目根的文件路径"},
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_string": {"type": "string"},
                            "new_string": {"type": "string"},
                        },
                        "required": ["old_string", "new_string"],
                    },
                    "description": "替换列表，按顺序应用；每条 old_string 须唯一匹配",
                },
            },
            "required": ["path", "edits"],
        },
        tool_multi_edit,
    ),
    "delete_file": (
        "删除项目内的一个文件；删除目录必须显式带 recursive=true。",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对项目根的文件/目录路径"},
                "recursive": {"type": "boolean", "description": "目录递归删除，默认 false"},
            },
            "required": ["path"],
        },
        tool_delete_file,
    ),
    "move_file": (
        "移动或重命名文件/目录（两端都必须在项目内）；目标已存在需带 overwrite=true。",
        {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "源路径（相对项目根）"},
                "destination": {"type": "string", "description": "目标路径（相对项目根）"},
                "overwrite": {"type": "boolean", "description": "目标已存在时是否覆盖，默认 false"},
            },
            "required": ["source", "destination"],
        },
        tool_move_file,
    ),
    "grep": (
        "在项目内按正则搜索文本行，可用 glob 过滤文件。",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "path": {"type": "string", "description": "搜索起点目录，默认 ."},
                "glob": {"type": "string", "description": "文件名过滤，如 *.py，默认全部"},
            },
            "required": ["pattern"],
        },
        tool_grep,
    ),
    "find_files": (
        "按文件名 glob 在项目内查找文件（如 *.py、config*、test_*.md），"
        "比 list_dir 全量列举更高效。",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "文件名 glob，如 *.py"},
                "path": {"type": "string", "description": "查找起点目录，默认 ."},
            },
            "required": ["pattern"],
        },
        tool_find_files,
    ),
}

__all__ = [
    "DEFAULT_READ_LINES",
    "FS_TOOLS",
    "MAX_FILE_READ_CHARS",
    "_closest_snippet",
    "_resolve_in_root",
    "tool_delete_file",
    "tool_edit_file",
    "tool_find_files",
    "tool_grep",
    "tool_list_dir",
    "tool_move_file",
    "tool_multi_edit",
    "tool_read_file",
    "tool_write_file",
]
