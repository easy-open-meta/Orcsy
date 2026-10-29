"""工具集：Agent 在受管项目内可用的原子操作。

安全边界：
- 文件读写被禁锢在项目根目录（worktree 或项目目录）内，路径穿越直接拒绝；
- 命令执行走 subprocess，限时长、可选白名单，不提供交互；
- git 的提交/合并/推送不在工具集里——那是 TaskExecutor 的职责，Agent 只管改代码。
"""

from __future__ import annotations

import fnmatch
import json
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from .logging_setup import get_logger

log = get_logger("tools")

# 只读命令白名单：测试、构建、lint 等 Agent 自验证所需的常见命令。
# 通配符按 argv[0] 的文件名匹配；不在表内的命令一律拒绝。
DEFAULT_ALLOWED_COMMANDS = [
    "python", "python3", "pytest", "node", "npm", "npx",
    "cargo", "go", "dotnet", "gradle*", "mvn", "make", "git",
    "uv", "pip",
]

MAX_OUTPUT_CHARS = 8000
MAX_FILE_READ_CHARS = 40000


@dataclass
class ToolContext:
    """一次任务执行的工具环境：根目录禁锢 + 命令白名单。"""

    root: Path
    allowed_commands: list[str] | None = None  # None 用默认白名单

    def __post_init__(self) -> None:
        self.root = self.root.resolve()


def _resolve_in_root(ctx: ToolContext, rel_path: str) -> Path:
    """把相对路径解析到根目录内，拒绝绝对路径与路径穿越。"""
    p = Path(rel_path)
    if p.is_absolute():
        raise ValueError(f"只允许相对路径（相对项目根）：{rel_path}")
    resolved = (ctx.root / p).resolve()
    if ctx.root != resolved and ctx.root not in resolved.parents:
        raise ValueError(f"路径越界，超出项目根目录：{rel_path}")
    return resolved


# ---- 工具实现：每个函数 (ctx, args) -> str ----

def tool_read_file(ctx: ToolContext, args: dict) -> str:
    path = _resolve_in_root(ctx, str(args["path"]))
    if not path.is_file():
        return f"错误：文件不存在 {args['path']}"
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_FILE_READ_CHARS:
        return text[:MAX_FILE_READ_CHARS] + f"\n\n…（文件过长，截断至 {MAX_FILE_READ_CHARS} 字符）"
    return text


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
        return "错误：old_string 在文件中不存在（注意空白与换行需完全一致）"
    if count > 1:
        return f"错误：old_string 匹配了 {count} 处，请提供更长的上下文使其唯一"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return f"已修改 {args['path']}"


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
        lines.append(("d " if p.is_dir() else "f ") + rel_display)
        if len(lines) >= 300:
            lines.append("…（目录条目过多，已截断）")
            break
    return "\n".join(lines) or "（空目录）"


# 正在运行的 Agent 子进程（git/pytest/npm 等），程序退出时统一终止，避免孤儿进程
_CHILD_PROCS: set = set()
_CHILD_PROCS_LOCK = threading.Lock()


def kill_running_children() -> int:
    """终止所有仍在运行的 Agent 子进程，返回终止数量。供程序退出时调用。"""
    killed = 0
    with _CHILD_PROCS_LOCK:
        for proc in list(_CHILD_PROCS):
            try:
                proc.kill()
                killed += 1
            except OSError:
                pass
        _CHILD_PROCS.clear()
    return killed


def tool_run_command(ctx: ToolContext, args: dict) -> str:
    command = [str(c) for c in args.get("command", [])]
    if not command:
        return "错误：command 不能为空"
    exe = Path(command[0]).name.lower()
    allowed = ctx.allowed_commands if ctx.allowed_commands is not None else DEFAULT_ALLOWED_COMMANDS
    if not any(fnmatch.fnmatch(exe, pat.lower()) for pat in allowed):
        return f"错误：命令 {exe} 不在白名单内（{', '.join(allowed)}），如需放开请修改配置 allowed_commands"
    timeout = min(int(args.get("timeout_seconds", 120)), 600)
    log.debug("run_command: %s (cwd=%s)", command, ctx.root)
    try:
        # Windows 上用任务对象/进程树终止：start_new_session 让整组进程可一并 kill
        proc = subprocess.Popen(
            command, cwd=str(ctx.root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", shell=False,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except FileNotFoundError:
        return f"错误：命令不存在：{command[0]}"
    with _CHILD_PROCS_LOCK:
        _CHILD_PROCS.add(proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        code = -1
        out = (stdout or "") + (("\n[stderr]\n" + stderr) if stderr else "")
        return f"错误：命令超时（>{timeout}s）\n{out[:MAX_OUTPUT_CHARS]}"
    finally:
        with _CHILD_PROCS_LOCK:
            _CHILD_PROCS.discard(proc)
    out = (stdout or "") + (("\n[stderr]\n" + stderr) if stderr else "")
    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS] + f"\n…（输出过长，截断，退出码 {code}）"
    return f"退出码 {code}\n{out or '（无输出）'}"


def tool_grep(ctx: ToolContext, args: dict) -> str:
    pattern = str(args["pattern"])
    import re
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


def tool_finish(ctx: ToolContext, args: dict) -> str:
    """Agent 主动结束任务并给出结构化结论；由执行器捕获处理。"""
    raise FinishSignal(args)


class FinishSignal(Exception):
    """工具 finish 抛出的任务结束信号，payload 即结论 JSON 参数。"""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        super().__init__("finish")


# ---- 工具注册表 ----

# name -> (描述, 参数 JSON Schema, 实现)
TOOLS: dict[str, tuple[str, dict, object]] = {
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
        "读取项目内一个文本文件的内容。",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "相对项目根的文件路径"}},
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
    "run_command": (
        "在项目根目录执行白名单内的命令（测试/构建/lint 等），返回退出码与输出。",
        {
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"},
                            "description": "argv 形式，如 [\"pytest\", \"-x\"]"},
                "timeout_seconds": {"type": "integer", "description": "超时秒数，默认 120，上限 600"},
            },
            "required": ["command"],
        },
        tool_run_command,
    ),
    "finish": (
        "任务完成或受阻时调用，给出最终结论。必须提供 action/summary 等字段。",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["completed", "blocked", "cannot_do"]},
                "summary": {"type": "string"},
                "files_changed": {"type": "array", "items": {"type": "string"}},
                "commit_message": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["action", "summary"],
        },
        tool_finish,
    ),
}


def tool_definitions_openai() -> list[dict]:
    return [
        {"type": "function", "function": {"name": name, "description": desc, "parameters": schema}}
        for name, (desc, schema, _) in TOOLS.items()
    ]


def tool_definitions_anthropic() -> list[dict]:
    return [
        {"name": name, "description": desc, "input_schema": schema}
        for name, (desc, schema, _) in TOOLS.items()
    ]


def dispatch_tool(ctx: ToolContext, name: str, args_json: str | dict) -> str:
    """执行一个工具调用，任何异常都转成错误文本回给模型（不让单次调用崩掉循环）。"""
    if name not in TOOLS:
        return f"错误：未知工具 {name}"
    try:
        args = json.loads(args_json) if isinstance(args_json, str) else (args_json or {})
        if not isinstance(args, dict):
            return "错误：工具参数必须是 JSON 对象"
    except json.JSONDecodeError as e:
        return f"错误：工具参数不是合法 JSON：{e}"
    try:
        return str(TOOLS[name][2](ctx, args))
    except FinishSignal:
        raise  # finish 由 Agent 循环捕获
    except Exception as e:  # noqa: BLE001 — 工具错误回给模型自行调整
        return f"错误：{e}"
