"""工具集：Agent 在受管项目内可用的原子操作。

安全边界：
- 文件读写被禁锢在项目根目录（worktree 或项目目录）内，路径穿越直接拒绝；
- 命令执行走 subprocess，限时长、可选白名单，不提供交互；
- web_fetch 是唯一突破项目沙箱的工具（联网取正文）：仅 http/https，
  且拒绝解析到回环/私网/保留地址的目标（SSRF 防护），只回文本类内容；
- git 的提交/合并/推送不在工具集里——那是 TaskExecutor 的职责，Agent 只管改代码。
"""

from __future__ import annotations

import fnmatch
import ipaddress
import json
import shutil
import socket
import subprocess
import threading
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from .logging_setup import get_logger

log = get_logger("tools")

# 无窗口实例（托盘 exe）派生控制台程序会闪黑窗，CREATE_NO_WINDOW 抑制之
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 只读命令白名单：测试、构建、lint 等 Agent 自验证所需的常见命令。
# 通配符按 argv[0] 的文件名匹配；不在表内的命令一律拒绝。
DEFAULT_ALLOWED_COMMANDS = [
    "python", "python3", "pytest", "node", "npm", "npx",
    "cargo", "go", "dotnet", "gradle*", "mvn", "make", "git",
    "uv", "pip",
]

MAX_OUTPUT_CHARS = 6000
MAX_FILE_READ_CHARS = 12000
MAX_SKILL_CHARS = 24000  # 技能全文上限，对齐 prompts 的 guide_max_chars
DEFAULT_READ_LINES = 250
MAX_WEB_FETCH_CHARS = 20000   # web_fetch 正文默认上限
MAX_WEB_FETCH_BYTES = 4_000_000  # web_fetch 下载字节硬上限（防超大响应撑爆内存）


@dataclass
class ToolContext:
    """一次任务执行的工具环境：根目录禁锢 + 命令白名单 + 工具开关 + 审批门。"""

    root: Path
    allowed_commands: list[str] | None = None  # None 用默认白名单
    # 技能名 → SKILL.md 正文（执行器从技能库预载；read_skill 工具专用，
    # 不受项目根沙箱限制——技能存放在配置目录，不在项目内）
    skills: dict[str, str] | None = None
    # 启用的工具名集合；None = 全部启用。finish 是协议工具，恒可用，
    # 不在此集合的约束范围内（dispatch_tool 对它放行）
    enabled_tools: frozenset[str] | None = None
    # 人工审批门：(工具名, 已解析 args) -> None 放行 / 字符串则作为错误结果
    # 回给模型（操作不执行、零副作用）。由执行器按访问权限模式构建；
    # None = 完全访问（不询问，与历史行为一致）
    approval_gate: object | None = None

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
            return f"错误：第 {i} 条 edit 的 old_string 在文件中不存在（文件未改动）"
        if count > 1:
            return (f"错误：第 {i} 条 edit 的 old_string 匹配了 {count} 处，"
                    f"请提供更长上下文使其唯一（文件未改动）")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")
    return f"已修改 {args['path']}（{len(edits)} 处替换）"


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
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | _NO_WINDOW,
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
        # 头尾保留：命令回显/进度在头部，报错摘要多在尾部
        out = out[:4000] + "\n…（中间输出过长，已省略）\n" + out[-2000:] + \
            f"\n…（输出过长，截断，退出码 {code}）"
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


def tool_read_skill(ctx: ToolContext, args: dict) -> str:
    """读取一个适用技能的 SKILL.md 全文（内容由执行器从技能库预载）。"""
    name = str(args.get("name", "")).strip()
    if not ctx.skills:
        return "错误：当前项目没有可用的技能"
    if name not in ctx.skills:
        return f"错误：技能 {name!r} 不存在或对当前项目不生效。可用技能：{', '.join(ctx.skills)}"
    body = ctx.skills[name]
    if len(body) > MAX_SKILL_CHARS:
        body = body[:MAX_SKILL_CHARS] + f"\n\n…（技能过长，已截断至前 {MAX_SKILL_CHARS} 字符）"
    return body


# ---- web_fetch：唯一联网的工具，SSRF 防护是硬边界 ----

def _assert_public_url(url: str) -> None:
    """只允许 http/https，且解析出的所有 IP 都必须是公网地址。

    外网部署时模型可能被页面内容诱导去探内网（元数据服务 169.254.169.254、
    回环管理端口等），解析后逐一判定，命中回环/私网/链路本地/保留/组播即拒绝。
    """
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ValueError(f"只支持 http/https（当前：{u.scheme or '空'}）")
    host = u.hostname
    if not host:
        raise ValueError("URL 缺少主机名")
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port)
    except socket.gaierror as e:
        raise ValueError(f"无法解析主机名 {host}：{e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ValueError(f"目标 {host} 解析到非公网地址（{ip}），已拒绝访问")


class _HTMLText(HTMLParser):
    """把 HTML 剥成可读正文：丢弃 script/style，块级标签转换行，压缩空白。"""

    _SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    _BREAK = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section",
              "article", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BREAK:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self._BREAK:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0 and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        import re
        raw = "".join(self._parts)
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        raw = re.sub(r" ?\n ?", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _html_to_text(html: str) -> str:
    parser = _HTMLText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — 畸形 HTML 也尽量给出已解析部分
        pass
    return parser.text()


def tool_web_fetch(ctx: ToolContext, args: dict) -> str:
    """抓取一个公网 URL 的正文（HTML 剥标签 / 文本类原样），供查文档、读 issue 等。"""
    url = str(args.get("url", "")).strip()
    if not url:
        return "错误：url 不能为空"
    try:
        max_chars = max(1000, min(int(args.get("max_chars") or MAX_WEB_FETCH_CHARS), 100000))
    except (TypeError, ValueError):
        return "错误：max_chars 必须是整数"
    try:
        _assert_public_url(url)
    except ValueError as e:
        return f"错误：{e}"
    import httpx  # 延迟导入：不联网的进程不为它付启动成本
    try:
        with httpx.Client(follow_redirects=True, timeout=20.0,
                          headers={"User-Agent": "Mozilla/5.0 (compatible; OrcsyAgent)"}) as client:
            with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    return f"错误：HTTP {resp.status_code}（{url}）"
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                ok_types = ("text/", "application/json", "application/xml",
                            "application/xhtml+xml", "application/javascript")
                if ctype and not any(ctype.startswith(t) or ctype == t for t in ok_types):
                    return f"错误：不支持的内容类型 {ctype}（只抓取文本/HTML/JSON 类资源）"
                buf = bytearray()
                for chunk in resp.iter_bytes(65536):
                    buf.extend(chunk)
                    if len(buf) >= MAX_WEB_FETCH_BYTES:
                        break
                encoding = resp.encoding or "utf-8"
    except httpx.HTTPError as e:
        return f"错误：请求失败：{e}"
    text = bytes(buf).decode(encoding, errors="replace")
    if ctype in ("text/html", "application/xhtml+xml") or (
            not ctype and text.lstrip()[:1] == "<"):
        text = _html_to_text(text)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n…（正文过长，已截断至前 {max_chars} 字符）"
    return f"[HTTP {resp.status_code} · {ctype or '未知类型'}]\n{text or '（空正文）'}"


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
    "read_skill": (
        "读取一个技能（Skill）的全文。系统提示词的技能索引里列出的技能适用时，"
        "先读取全文再严格按其内容执行；与任务无关的技能不要读取。",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "技能名，见系统提示词的技能索引"},
            },
            "required": ["name"],
        },
        tool_read_skill,
    ),
    "web_fetch": (
        "抓取一个公网 URL 的正文文本（HTML 自动剥标签；文档/issue/参考资料等）。"
        "仅限公网 http/https，内网与本机地址会被拒绝；不要用它访问需要登录的页面。",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http/https 地址"},
                "max_chars": {"type": "integer",
                              "description": f"正文上限（默认 {MAX_WEB_FETCH_CHARS}，最大 100000）"},
            },
            "required": ["url"],
        },
        tool_web_fetch,
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


def tool_definitions_openai(enabled: frozenset[str] | None = None) -> list[dict]:
    return [
        {"type": "function", "function": {"name": name, "description": desc, "parameters": schema}}
        for name, (desc, schema, _) in TOOLS.items()
        if enabled is None or name in enabled
    ]


def tool_definitions_anthropic(enabled: frozenset[str] | None = None) -> list[dict]:
    return [
        {"name": name, "description": desc, "input_schema": schema}
        for name, (desc, schema, _) in TOOLS.items()
        if enabled is None or name in enabled
    ]


# ---- 工具开关：全局配置 + 项目级覆盖的公共校验 ----

# 可开关的工具（finish 是任务循环的协议工具，不可禁用，不在此列）
TOGGLEABLE_TOOLS: tuple[str, ...] = tuple(n for n in TOOLS if n != "finish")


def validate_tools_map(data: object, context: str = "tools") -> dict[str, bool]:
    """校验「工具名 → bool」映射：未知工具名/禁用 finish/非布尔值都直接报错。

    返回清洗后的 dict（空 dict = 全默认启用）。风格对齐 config._coerce_llm：
    加载/保存期即失败，中文错误信息经 Web 层透传到看板。
    """
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{context} 必须是「工具名: true/false」的键值对")
    out: dict[str, bool] = {}
    for key, value in data.items():
        name = str(key).strip()
        if name not in TOOLS:
            raise ValueError(f"{context} 含未知工具：{name}"
                             f"（可用：{', '.join(TOOLS)}）")
        if name == "finish":
            # UI 把 finish 展示为「常开且禁用」的开关；true 容忍并剥掉
            # （前端全量 map 带上它也不算错），false/非布尔一律拒绝
            if value is True:
                continue
            raise ValueError(f"{context}：finish 是任务结束的协议工具，不可禁用")
        if not isinstance(value, bool):
            raise ValueError(f"{context}.{name} 必须是布尔值（true/false）")
        out[name] = value
    return out


def dispatch_tool(ctx: ToolContext, name: str, args_json: str | dict) -> str:
    """执行一个工具调用，任何异常都转成错误文本回给模型（不让单次调用崩掉循环）。"""
    if name not in TOOLS:
        return f"错误：未知工具 {name}"
    # 开关兜底拦截：schema 已按启用集过滤，这里防模型幻觉调用被禁工具
    if (name != "finish" and ctx.enabled_tools is not None
            and name not in ctx.enabled_tools):
        return f"错误：工具 {name} 已被配置禁用，请改用其他工具或调用 finish 说明受阻原因"
    try:
        args = json.loads(args_json) if isinstance(args_json, str) else (args_json or {})
        if not isinstance(args, dict):
            return "错误：工具参数必须是 JSON 对象"
    except json.JSONDecodeError as e:
        return f"错误：工具参数不是合法 JSON：{e}"
    # 人工审批门：按访问权限模式可能阻塞等待看板批准；拒绝/超时以错误文本
    # 回给模型（本工具不执行，零副作用）
    if ctx.approval_gate is not None:
        try:
            denial = ctx.approval_gate(name, args)
        except Exception as e:  # noqa: BLE001 — 审批基础设施故障按拒绝处理，宁严勿松
            log.exception("审批门异常，按拒绝处理")
            denial = f"错误：审批系统异常（{e}），该操作未执行"
        if denial:
            return str(denial)
    try:
        return str(TOOLS[name][2](ctx, args))
    except FinishSignal:
        raise  # finish 由 Agent 循环捕获
    except Exception as e:  # noqa: BLE001 — 工具错误回给模型自行调整
        return f"错误：{e}"
