"""工具集：Agent 在受管项目内可用的原子操作。

安全边界：
- 文件读写被禁锢在项目根目录（worktree 或项目目录）内，路径穿越直接拒绝；
- 命令执行走 subprocess，限时长、可选白名单，不提供交互；
- web_fetch 是唯一突破项目沙箱的工具（联网取正文）：仅 http/https，
  且拒绝解析到回环/私网/保留地址的目标（SSRF 防护），只回文本类内容；
- git 的提交/合并/推送不在工具集里——那是 TaskExecutor 的职责，Agent 只管改代码。
"""

from __future__ import annotations

import difflib
import fnmatch
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse, urljoin

from .logging_setup import get_logger
from .mirrors import CATEGORY_BY_KEY, CATEGORY_KEYS

log = get_logger("tools")

# 无窗口实例（托盘 exe）派生控制台程序会闪黑窗，CREATE_NO_WINDOW 抑制之
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _windows_startupinfo() -> subprocess.STARTUPINFO | None:
    """Windows 下彻底隐藏控制台子进程窗口的 STARTUPINFO。

    配合 CREATE_NO_WINDOW 与 stdin=DEVNULL 使用，确保 Windows 窗口管理器
    在创建进程第一帧即以 SW_HIDE 初始，彻底杜绝控制台黑框闪烁。
    """
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        si.wShowWindow = 0  # SW_HIDE
        return si
    return None

# 命令白名单：测试、构建、lint 等 Agent 自验证所需的常见命令。
# 通配符按 argv[0] 的文件名匹配（小写、fnmatch）；不在表内的命令一律拒绝。
# 列表需覆盖 orcsy/probe.py 为各技术栈推荐的命令（pnpm/yarn 项目、CMake
# 项目的 ctest）——否则自动写进 AGENT.md 的测试命令会被自家白名单拦下。
DEFAULT_ALLOWED_COMMANDS = [
    # Python 生态（python* 覆盖 python3.11 等带版本解释器）
    "python*", "py", "pip*", "pytest", "uv", "uvx", "poetry", "tox",
    "ruff", "mypy", "black", "flake8",
    # Node / 前端
    "node", "npm", "npx", "pnpm", "yarn", "bun",
    "tsc", "eslint", "prettier", "vitest", "jest",
    # Rust / Go / .NET / Java
    "cargo", "go", "dotnet", "gradle*", "mvn", "java", "javac",
    # C/C++
    "make", "cmake", "ctest", "ninja",
    "gcc", "g++", "clang", "clang++", "msbuild",
    # 通用
    "git",
    # 包管理器（环境引导：缺工具链时静默安装；受访问权限审批约束——
    # autonomous/ask_on_change 模式下安装会挂起等人工批准，详见 env-setup 技能）
    "winget*", "choco", "scoop", "brew", "apt*", "apt-get", "dnf", "nvm",
    # 系统 shell/下载/解压（env-setup 阶段二B 镜像直下流程需要；cmd 本就在
    # 名单内，白名单挡的是误操作而非恶意，真正的安全边界是访问权限审批门）
    "powershell", "pwsh", "curl", "tar", "setx",
    # Windows 环境探测/文件查看（模型排障常用：找解释器、看目录、查环境变量）
    "where", "cmd", "dir", "type", "echo", "set", "tasklist", "systeminfo",
    "findstr", "more", "ver", "hostname", "whoami",
    # POSIX 只读探测/统计（Git Bash / macOS / Linux 常用；模型高频使用——
    # 统计行数、看文件片段、按文本处理等。只收只读与无害统计类：能写文件的
    # sed/find/xargs、能执行任意命令的 env/awk/bash/sh 一律不放，模型该用
    # write_file/edit_file 工具或让项目自身的构建命令干活）
    "ls", "cat", "wc", "head", "tail", "grep", "sort", "uniq", "cut", "tr",
    "diff", "stat", "du", "df", "pwd", "printf", "date", "sleep", "uname",
    "which", "id", "basename", "dirname", "touch", "md5sum", "sha256sum",
    "printenv",
]

# 命令白名单生态分类（供设置页与 API 结构化呈现及批量管理）
COMMAND_CATEGORIES = [
    {
        "key": "python",
        "label": "Python 生态",
        "icon": "🐍",
        "description": "Python 解释器、包管理与测试/Lint 工具",
        "commands": ["python*", "py", "pip*", "pytest", "uv", "uvx", "poetry", "tox", "ruff", "mypy", "black", "flake8"],
    },
    {
        "key": "node",
        "label": "Node / 前端",
        "icon": "📦",
        "description": "Node 运行时、包管理器与前端构建/测试工具",
        "commands": ["node", "npm", "npx", "pnpm", "yarn", "bun", "tsc", "eslint", "prettier", "vitest", "jest"],
    },
    {
        "key": "compiled",
        "label": "Rust / Go / .NET / Java",
        "icon": "⚙️",
        "description": "系统级与强类型后端编译型语言工具链",
        "commands": ["cargo", "go", "dotnet", "gradle*", "mvn", "java", "javac"],
    },
    {
        "key": "cpp",
        "label": "C / C++ 构建",
        "icon": "🔨",
        "description": "C/C++ 编译器、构建工具与测试套件",
        "commands": ["make", "cmake", "ctest", "ninja", "gcc", "g++", "clang", "clang++", "msbuild"],
    },
    {
        "key": "vcs",
        "label": "版本控制",
        "icon": "🌿",
        "description": "Git 代码仓库与分支版本管理",
        "commands": ["git"],
    },
    {
        "key": "package_managers",
        "label": "包管理器",
        "icon": "📥",
        "description": "系统级包管理器（缺工具链时按需环境引导，受审批约束）",
        "commands": ["winget*", "choco", "scoop", "brew", "apt*", "apt-get", "dnf", "nvm"],
    },
    {
        "key": "system_shell",
        "label": "系统 Shell / 实用工具",
        "icon": "💻",
        "description": "系统 Shell、网络下载与归档解压",
        "commands": ["powershell", "pwsh", "curl", "tar", "setx"],
    },
    {
        "key": "windows_probe",
        "label": "Windows 探测与排障",
        "icon": "🪟",
        "description": "Windows 环境排障、进程查看与文件探测（只读/信息性）",
        "commands": ["where", "cmd", "dir", "type", "echo", "set", "tasklist", "systeminfo", "findstr", "more", "ver", "hostname", "whoami"],
    },
    {
        "key": "posix_probe",
        "label": "POSIX 只读探测与统计",
        "icon": "🐧",
        "description": "跨平台/Unix 常用只读与文本统计命令（只读无害）",
        "commands": ["ls", "cat", "wc", "head", "tail", "grep", "sort", "uniq", "cut", "tr", "diff", "stat", "du", "df", "pwd", "printf", "date", "sleep", "uname", "which", "id", "basename", "dirname", "touch", "md5sum", "sha256sum", "printenv"],
    },
]

MAX_OUTPUT_CHARS = 6000
MAX_FILE_READ_CHARS = 12000
MAX_SKILL_CHARS = 24000  # 技能全文上限，对齐 prompts 的 guide_max_chars
DEFAULT_READ_LINES = 250
MAX_WEB_FETCH_CHARS = 20000   # web_fetch 正文默认上限
MAX_WEB_FETCH_BYTES = 4_000_000  # web_fetch 下载字节硬上限（防超大响应撑爆内存）

# ---- 新装工具的可发现性 ----
# 本进程的 PATH 是启动时的快照：env-setup 技能用 winget 等装完工具后，新命令
# 不在快照里，run_command 会误报「命令不存在」。三招兜底：
# 1) 现读注册表用户/系统 PATH（Windows）：技能用 setx / SetEnvironmentVariable
#    (…,"User") 注册后，下一条命令立即可见，无需重启看板（ZCode 的 login-shell
#    环境捕获在 Windows 的等价物——GUI 进程的 PATH 不经过 shell 初始化）；
# 2) 常见安装目录（下表）每次调用现查，装到这些位置的工具立即可用；
# 3) 绝对路径调用按 basename（去 .exe/.cmd/.bat 后缀）过白名单，basename
#    在名单内即放行（如 D:\...\go.exe 等价于 go）。
_TOOL_PATH_DIRS = (
    r"%ProgramFiles%\Go\bin", r"%ProgramFiles%\nodejs", r"%ProgramFiles%\CMake\bin",
    r"%LOCALAPPDATA%\Programs\Go\bin", r"%LOCALAPPDATA%\Programs\Python",
    r"%LOCALAPPDATA%\Microsoft\WinGet\Links",   # winget 的命令符号链接目录
    r"%LOCALAPPDATA%\Microsoft\WindowsApps",
    r"%ProgramFiles%\Eclipse Adoptium",         # Temurin JDK（winget 装的 JDK 家）
    r"%ProgramFiles%\Java",                     # Oracle/Zulu 等其它 JDK
    r"~\.cargo\bin", r"~\go\bin", r"~\scoop\shims", r"~\.local\bin",
    "/usr/local/bin", "/usr/local/go/bin", "/opt/homebrew/bin",
)
_EXE_SUFFIX_RE = re.compile(r"\.(exe|cmd|bat)$", re.IGNORECASE)

# Windows 注册表 PATH 的两个来源：用户级（HKCU\Environment，setx 与
# [Environment]::SetEnvironmentVariable(…,"User") 写这里）与系统级
REG_PATH_KEYS = (
    ("HKEY_CURRENT_USER", r"Environment"),
    ("HKEY_LOCAL_MACHINE",
     r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
)


def _registry_path_entries() -> list[str]:
    """现读注册表里的用户/系统 PATH 目录（仅 Windows；失败静默返回空）。

    进程 PATH 是启动快照，Agent 装完工具注册新用户级 PATH 后看不到——这里
    每次调用现读（注册表查询开销可忽略），让新目录下一条命令即生效。
    REG_EXPAND_SZ 里的 %VAR% 用 expandvars 展开；跳过空串与未展开残留。
    """
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - 非 Windows 平台
        return []
    out: list[str] = []
    for hive_name, subkey in REG_PATH_KEYS:
        try:
            with winreg.OpenKey(getattr(winreg, hive_name), subkey) as key:
                raw, _ = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        for entry in str(raw).split(os.pathsep):
            p = os.path.expandvars(entry.strip().strip('"'))
            if p and "%" not in p:
                out.append(p)
    return out

# 依赖拉取类网络失败的常见特征（用于给出镜像/代理提示，而不是让模型瞎重试）
_NETWORK_FAIL_RE = re.compile(
    r"unexpected EOF|i/o timeout|connection reset|connection refused|"
    r"TLS handshake|proxyconnect|dial tcp|no such host|temporary failure|"
    r"ETIMEDOUT|ECONNRESET|ECONNREFUSED|network is unreachable|"
    r"Failed to establish a new connection|Read timed out|"
    r"could not fetch|cannot download|连接被重置|连接超时|网络不可达",
    re.IGNORECASE,
)


def _path_key(p: str) -> str:
    """PATH 目录去重键（Windows 大小写不敏感，尾部分隔符归一）。"""
    p = p.rstrip("/\\")
    return p.lower() if os.name == "nt" else p


def _tool_path_dirs() -> list[str]:
    """子进程 PATH 需要追加的目录：注册表最新 PATH + 常见安装目录（每次现查）。

    已在本进程 PATH 里的目录不重复追加；只保留真实存在的（目录可能刚被装出来，
    也可能卸载后残留注册表项）。注册表目录在前——用户真实 PATH 的优先级高于
    硬编码兜底目录。
    """
    seen = {_path_key(p) for p in os.environ.get("PATH", "").split(os.pathsep)
            if p.strip()}
    out: list[str] = []
    candidates = _registry_path_entries() + [
        os.path.expandvars(os.path.expanduser(raw)) for raw in _TOOL_PATH_DIRS]
    for p in candidates:
        key = _path_key(p)
        if key in seen or not os.path.isdir(p):
            continue
        seen.add(key)
        out.append(p)
    return out


def _augmented_env(ctx: "ToolContext | None" = None) -> dict[str, str]:
    """子进程环境：PATH 追加常见安装目录 + 注入配置的自定义变量（代理/镜像等）。

    工具链 home 类约定变量（JAVA_HOME 等）若在系统环境里已存在则原样保留；
    用户可在 config env.vars 显式设置（如 JAVA_HOME 指向新装 JDK）以影响
    sdkmanager/gradle 这类按约定变量找工具链的命令。
    """
    env = dict(os.environ)
    extra = os.pathsep.join(_tool_path_dirs())
    if extra:
        env["PATH"] = env.get("PATH", "") + os.pathsep + extra
    if ctx is not None and ctx.extra_env:
        env.update(ctx.extra_env)  # 用户显式配置优先于默认
    return env


def _resolve_new_install(command: list[str]) -> list[str]:
    """命令在本进程 PATH 找不到时，到常见安装目录里找（裸名与 .exe/.cmd/.bat）。

    PATH 里已能解析的命令绝不改写（shutil.which 优先）——WindowsApps 的 0 字节
    执行别名等坑不碰；本目录候选也跳过 0 字节文件（同款别名陷阱）。
    """
    exe = command[0]
    if os.sep in exe or "/" in exe or Path(exe).anchor:
        return command  # 已是路径形态，交给 Popen
    if shutil.which(exe):
        return command  # 进程 PATH（含父进程注入的部分）能找到：保持原样
    suffixes = ("", ".exe", ".cmd", ".bat") if os.name == "nt" else ("",)
    for d in _tool_path_dirs():
        if "windowsapps" in d.lower():
            continue  # Microsoft Store 执行别名目录：0 字节占位，调了必 9009
        for suffix in suffixes:
            cand = Path(d) / (exe + suffix)
            if cand.is_file() and cand.stat().st_size > 0:
                return [str(cand), *command[1:]]
    return command


# ---- 任务启动扫描：本机工具地图（进系统提示词）+ 可选写用户 PATH ----

# 常见开发命令：启动项目时逐个解析，结果作为「本机工具」节注入系统提示词。
# 教训：npm 装在 %ProgramFiles%\nodejs 但看板进程 PATH 快照里没有，Agent
# 只能自己一轮轮探测绝对路径——扫描 + 提示词把这条路彻底省掉
_KNOWN_TOOLS = (
    "node", "npm", "npx", "pnpm", "yarn", "deno", "bun",
    "python", "pip", "uv", "poetry",
    "git", "go", "cargo", "rustc", "java", "javac", "dotnet", "cmake",
)


def scan_tool_env() -> dict:
    """扫描本机常用开发命令的绝对路径与需注入会话的目录（每次调用现查）。

    which 在扩充 PATH 的语义下解析（注册表最新 PATH + 常见安装目录，与
    run_command 实际用的 env 一致）；不跑 --version 子进程（太重）。
    返回 {tools: {命令: 绝对路径}, extra_dirs: [本次会话注入的目录]}。
    """
    path_value = _augmented_env().get("PATH", "")
    tools: dict[str, str] = {}
    for name in _KNOWN_TOOLS:
        found = shutil.which(name, path=path_value)
        if found:
            tools[name] = found
    return {"tools": tools, "extra_dirs": _tool_path_dirs()}


def _broadcast_env_change() -> None:
    """广播 WM_SETTINGCHANGE：让 Explorer 等后续新进程立即看到新用户 PATH。"""
    try:
        import ctypes
        HWND_BROADCAST = 0xFFFF
        SMTO_ABORTIFHUNG = 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, 0x001A, 0, "Environment", SMTO_ABORTIFHUNG, 2000, None)
    except Exception:  # noqa: BLE001 — 广播失败无碍，新登录会话自然生效
        pass


def persist_path_dirs(dirs: list[str]) -> list[str]:
    """把缺失目录追加写入 HKCU 用户 PATH（等价 setx，无需管理员；只增不删）。

    看板 env.path_autofix 开启时任务启动调用：Agent 会话内可用 ≠ 用户自己的
    终端可用，这里做持久修复。保留原值类型（REG_EXPAND_SZ 的 %VAR% 条目
    原样保留），写回前旧值先打日志（可回滚），写回后广播。幂等：无缺失时
    不写不广播。任何失败记日志返回空，绝不阻断任务启动。
    """
    if os.name != "nt" or not dirs:
        return []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as key:
            try:
                raw, vtype = winreg.QueryValueEx(key, "Path")
            except OSError:
                raw, vtype = "", winreg.REG_EXPAND_SZ
            raw_entries = [e for e in str(raw).split(os.pathsep) if e.strip()]
            existing = {_path_key(os.path.expandvars(e)) for e in raw_entries}
            add: list[str] = []
            for d in dirs:
                if _path_key(d) in existing or "windowsapps" in d.lower():
                    continue
                existing.add(_path_key(d))
                add.append(d)
            if not add:
                return []
            log.info("写用户 PATH 前的旧值（可回滚）：%s", raw)
            winreg.SetValueEx(key, "Path", 0,
                              vtype or winreg.REG_EXPAND_SZ,
                              os.pathsep.join(raw_entries + add))
    except OSError as e:
        log.warning("写用户 PATH 失败（不阻断任务）：%s", e)
        return []
    _broadcast_env_change()
    log.info("用户 PATH 已追加 %d 个目录：%s", len(add), ", ".join(add))
    return add


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
    # 强制停止信号（看板「强制停止」设置）：工具循环在轮次/工具边界检查，
    # run_command 等待期间按 0.2s 切片轮询、置位即杀子进程并抛 TaskStopped
    stop_event: object | None = None
    # 注入子进程的自定义环境变量（config env.vars，如代理/国内镜像 GOPROXY）
    extra_env: dict[str, str] | None = None
    # 镜像源运行时（orcsy/mirrors.MirrorRuntime）：speedtest_mirrors 工具专用；
    # None = 配置来源未知（如单元测试直构），工具返回指引而非崩溃
    mirror_runtime: object | None = None
    # 当前正在执行的任务（task_model.Task）：create_tasks 拆分工具用它定位
    # 任务目录并关闭原任务；None（如单元测试直构）时工具拒绝拆分
    current_task: object | None = None
    # 本地调试会话（orcsy.debug_session.DebugSession）：持有开发服务管理器、
    # 会话级 HTTP 客户端与浏览器 CDP 客户端；None = 未启用本地调试
    session: object | None = None
    # 项目小组角色路由上下文：{role_id: {"name": str, "role": str, "produces_code": bool}}
    # None = 单兵模式（create_tasks 保持旧行为，不强校验 assignee）
    team_roles: dict[str, dict] | None = None
    # 当前执行者的角色 ID（如 "pm" / "backend" / "frontend" / "qa"）
    current_role: str = ""
    # 本任务执行者是否产出代码（单兵/后端/前端/自定义=True；PM/QA=False）。
    # 由执行器显式传入（默认 True）——不能靠 current_role 推断：单兵模式
    # current_role 为空串，会被误判为 False 导致「连续只读熔断」失效。
    produces_code: bool = True
    # PM 最大拆分深度上限（防无限套娃拆分）
    max_split_depth: int = 1
    # QA 在当前任务中新建的 Bug 任务 ID 列表（供 finish(action="wait_retest") 绑定回归依赖）
    created_bugs: list[str] = field(default_factory=list)
    # PM 调用 reassign_task 直接指派的目标角色（防止 finish(action="split") 关闭原任务）
    reassigned_to: str = ""
    # 是否为子代理运行态（如果是子代理，严禁再次调用 delegate_explore，深度锁死为 1）
    is_subagent: bool = False
    # 当前任务内已派生子代理的批次数（单任务最多允许调用 2 批次，防止失控）
    subagent_call_count: int = 0
    # 子代理累积消耗的用量：{"turns": int, "prompt_tokens": int, "completion_tokens": int}
    subagent_usage: dict[str, int] = field(default_factory=lambda: {"turns": 0, "prompt_tokens": 0, "completion_tokens": 0})
    # 执行当前任务所使用的 LLM 配置（供 subagent 派生时继承调用）
    llm_config: object | None = None

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


# 正在运行的 Agent 子进程（git/pytest/npm 等），程序退出时统一终止，避免孤儿进程
_CHILD_PROCS: set = set()
_CHILD_PROCS_LOCK = threading.Lock()


def _kill_process_tree(proc: "subprocess.Popen") -> None:
    """终止子进程及其整棵进程树。

    proc.kill() 只杀直接子进程：Windows 上孙进程（如 powershell 里 Start-Job
    拉起的服务器）继承 stdout/stderr 管道句柄，仍握着管道就等不到 EOF，
    调用方随后的 communicate() 会永久挂起。taskkill /T 按父子关系整树终止，
    管道句柄随之释放；非 Windows 无此问题（进程组已隔离），kill 即可。
    """
    if not proc.poll():
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                startupinfo=_windows_startupinfo(),
                creationflags=_NO_WINDOW,
            )
            if proc.poll() is None:  # taskkill 失败（如权限）：兜底直杀
                proc.kill()
        else:
            proc.kill()


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


def register_child_proc(proc: "subprocess.Popen") -> None:
    """注册一个运行中的子进程，供程序退出时统一杀灭。"""
    with _CHILD_PROCS_LOCK:
        _CHILD_PROCS.add(proc)


def unregister_child_proc(proc: "subprocess.Popen") -> None:
    """注销已结束的子进程。"""
    with _CHILD_PROCS_LOCK:
        _CHILD_PROCS.discard(proc)


def check_command_allowed(command: list[str],
                          allowed: list[str] | None = None) -> tuple[bool, str]:
    """校验命令是否符合白名单约束（run_command 与 dev_server_start 共用）。"""
    if not command:
        return False, "错误：command 不能为空"
    exe = _EXE_SUFFIX_RE.sub("", Path(command[0]).name.lower())
    eff_allowed = allowed if allowed is not None else DEFAULT_ALLOWED_COMMANDS
    if not any(fnmatch.fnmatch(exe, pat.lower()) for pat in eff_allowed):
        return False, f"错误：命令 {exe} 不在白名单内（{', '.join(eff_allowed)}），如需放开请修改配置 allowed_commands"
    return True, ""


# 子进程输出乱码自愈：utf-8 优先，疑似乱码过多时回退系统 ANSI 代码页。
# 背景：Windows 原生命令（cmd/findstr/dir）按 ANSI 代码页输出（简中系统为
# cp936/GBK），用 utf-8 强解会把中文路径打成「���»ʵ��」。改为拿原始字节
# 自适应解码——utf-8 解出过多 U+FFFD 时按 ANSI 重解，两边都保真。
_ANSI_CP = "mbcs" if os.name == "nt" else "utf-8"


def _decode_proc_output(raw: bytes) -> str:
    """自适应解码子进程输出：utf-8 优先，U+FFFD 过多（>2%）回退 ANSI 代码页。"""
    if not raw:
        return ""
    text = raw.decode("utf-8", errors="replace")
    if os.name != "nt":
        return text
    bad = text.count("\ufffd")
    if bad and bad / max(1, len(text)) > 0.02:
        try:
            alt = raw.decode(_ANSI_CP, errors="replace")
            # 回退解码的乱码比例更低才算改善（避免把真 utf-8 输出解坏）
            if alt.count("\ufffd") < bad:
                return alt
        except (LookupError, UnicodeDecodeError):
            pass
    return text


def tool_run_command(ctx: ToolContext, args: dict) -> str:
    command = [str(c) for c in args.get("command", [])]
    ok, err = check_command_allowed(command, ctx.allowed_commands)
    if not ok:
        return err
    timeout = min(int(args.get("timeout_seconds", 120)), 600)
    # 新装工具（本进程 PATH 快照里没有）自动到常见安装目录解析
    command = _resolve_new_install(command)
    log.debug("run_command: %s (cwd=%s)", command, ctx.root)
    try:
        # Windows 上用任务对象/进程树终止：start_new_session 让整组进程可一并 kill；
        # env 带上扩充 PATH：go 等工具派生子进程（编译器/链接器）时也能找到兄弟工具。
        # 输出按字节捕获后自适应解码（utf-8 → ANSI 回退）：原生命令（cmd/findstr）
        # 按 ANSI 代码页输出中文路径，utf-8 强解会打成乱码
        proc = subprocess.Popen(
            command, cwd=str(ctx.root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=False, shell=False,
            env=_augmented_env(ctx),
            startupinfo=_windows_startupinfo(),
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | _NO_WINDOW,
        )
    except FileNotFoundError:
        # 给可行动指引：模型常据此判断工具链缺失并转 blocked/调整方案，
        # 而不是反复盲试同一条命令浪费轮次
        return (f"错误：命令 {command[0]} 不存在（未安装或不在 PATH）。"
                "若是刚通过包管理器安装的工具，用绝对路径调用（如 "
                "\"C:\\Program Files\\Go\\bin\\go.exe\" version，白名单按文件名匹配）；"
                "若确认未安装，按 env-setup 技能安装；该项目若无法安装此工具链，"
                "不要反复重试，调用 finish（action=blocked）说明缺少的环境。")
    register_child_proc(proc)
    try:
        stdout, stderr = _wait_with_stop(ctx, proc, timeout)
        code = proc.returncode
    except _CommandTimeout as e:
        return f"错误：命令超时（>{timeout}s）\n{e.out[:MAX_OUTPUT_CHARS]}"
    finally:
        unregister_child_proc(proc)
    stdout_text = _decode_proc_output(stdout)
    stderr_text = _decode_proc_output(stderr)
    out = (stdout_text or "") + (("\n[stderr]\n" + stderr_text) if stderr_text else "")
    if len(out) > MAX_OUTPUT_CHARS:
        # 头尾保留：命令回显/进度在头部，报错摘要多在尾部
        out = out[:4000] + "\n…（中间输出过长，已省略）\n" + out[-2000:] + \
            f"\n…（输出过长，截断，退出码 {code}）"
    # 网络类失败（模块/依赖拉取 EOF、超时）：提示国内镜像与代理，别反复重试
    if code != 0 and _NETWORK_FAIL_RE.search(out):
        out += ("\n\n[环境提示] 该失败疑似网络问题（依赖拉取中断/超时）。"
                "国内网络建议在 config.yaml 的 env.vars 配置：\n"
                "  GOPROXY: https://goproxy.cn,direct   # Go 模块镜像\n"
                "  HTTP_PROXY / HTTPS_PROXY: http://127.0.0.1:<port>   # 有代理时\n"
                "Node 可用 npm config set registry https://registry.npmmirror.com；"
                "Python 可用 PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple。"
                "若是安装包本身下载失败（winget/官网不可达），按 env-setup 技能"
                "阶段二B 改国内镜像直下（如 Go 用 https://golang.google.cn/dl/，"
                "无需代理）。改配置后重试即可，不要盲目重复同一条命令。")
    return f"退出码 {code}\n{out or '（无输出）'}"


class _CommandTimeout(Exception):
    """run_command 超时的内部控制流异常（携带已捕获输出，由调用方转错误文本）。"""

    def __init__(self, out: str, timeout: int) -> None:
        self.out = out
        super().__init__(f"命令超时（>{timeout}s）")


def _wait_with_stop(ctx: ToolContext, proc: "subprocess.Popen",
                    timeout: int) -> tuple[bytes, bytes]:
    """等待子进程结束，期间按 0.2s 切片检查强制停止信号。

    置位即整树杀进程并抛 TaskStopped（让工具循环立刻收尾，而不是等命令
    跑完）；超时仍返回 (b"", b"") 并让调用方按超时错误回报——回滚语义与
    旧版一致。两处收尾都必须用带超时的 communicate：杀进程树后若仍有
    句柄遗留（如孙进程未被 taskkill 覆盖的边角情况），无界 communicate
    会把执行线程永久挂住，停止信号也就再无人检查。
    输出按原始字节返回，由调用方 _decode_proc_output 自适应解码
    （utf-8 → ANSI 回退），兼顾跨平台编码差异。
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            return proc.communicate(timeout=0.2)
        except subprocess.TimeoutExpired:
            if ctx.stop_event is not None and ctx.stop_event.is_set():
                _kill_process_tree(proc)
                _drain_after_kill(proc)
                log.info("任务被用户强制停止：已终止子进程 %s", proc.args)
                raise TaskStopped() from None
            if time.monotonic() >= deadline:
                _kill_process_tree(proc)
                stdout, stderr = _drain_after_kill(proc)
                out = _decode_proc_output(stdout) + \
                    (("\n[stderr]\n" + _decode_proc_output(stderr)) if stderr else "")
                raise _CommandTimeout(out, timeout)


def _drain_after_kill(proc: "subprocess.Popen",
                      grace: float = 5.0) -> tuple[bytes, bytes]:
    """杀掉进程树后回收管道内容；EOF 仍不来的话到点放弃并返回空。"""
    try:
        return proc.communicate(timeout=grace)
    except subprocess.TimeoutExpired:
        return b"", b""


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


class _WebFetchReject(Exception):
    """web_fetch 的业务性拒绝（HTTP 状态/内容类型），消息直接回给模型。"""


# 手动重定向循环：跳数上限与报告建议一致，超限报错而非静默返回
_REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))
_MAX_REDIRECT_HOPS = 5


def _fetch_with_guard(client, url: str) -> tuple[int, str, bytearray, str, str]:
    """手动重定向循环：每一跳都重过 SSRF 校验（含第 0 跳）。

    本工具的威胁模型就是攻击者控制的页面——重定向目标同样不可信，故关闭
    httpx 自动跟随、逐跳校验后再发，任何一跳解析到非公网 IP 即拒绝。
    返回 (status, content_type, body, encoding, final_url)。
    """
    import httpx  # 延迟导入，同 tool_web_fetch

    for _hop in range(_MAX_REDIRECT_HOPS):
        _assert_public_url(url)
        with client.stream("GET", url) as resp:
            if resp.status_code in _REDIRECT_STATUSES:
                loc = (resp.headers.get("location") or "").strip()
                if not loc:
                    raise httpx.HTTPError(
                        f"重定向缺少 Location（HTTP {resp.status_code}）")
                url = urljoin(url, loc)  # Location 可能是相对路径
                scheme = urlparse(url).scheme
                if scheme not in ("http", "https"):
                    raise httpx.HTTPError(f"重定向到不支持的协议 {scheme or '空'}")
                continue  # with 块退出即关闭未读完的流（连接归还连接池）
            if resp.status_code >= 400:
                raise _WebFetchReject(f"HTTP {resp.status_code}（{url}）")
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            ok_types = ("text/", "application/json", "application/xml",
                        "application/xhtml+xml", "application/javascript")
            if ctype and not any(ctype.startswith(t) or ctype == t for t in ok_types):
                raise _WebFetchReject(
                    f"不支持的内容类型 {ctype}（只抓取文本/HTML/JSON 类资源）")
            buf = bytearray()
            for chunk in resp.iter_bytes(65536):
                buf.extend(chunk)
                if len(buf) >= MAX_WEB_FETCH_BYTES:
                    break
            return resp.status_code, ctype, buf, (resp.encoding or "utf-8"), url
    raise httpx.HTTPError(
        f"重定向超过 {_MAX_REDIRECT_HOPS} 跳，已中止（可能的重定向循环）")


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
        # follow_redirects=False：重定向目标同样不可信，自动跟随等于绕过上面的
        # SSRF 硬边界（恶意页面可 302 → 内网/元数据服务）。_fetch_with_guard
        # 手动循环，每一跳都重新过校验
        with httpx.Client(follow_redirects=False, timeout=20.0,
                          headers={"User-Agent": "Mozilla/5.0 (compatible; OrcsyAgent)"}) as client:
            status, ctype, buf, encoding, final_url = _fetch_with_guard(client, url)
    except httpx.HTTPError as e:
        return f"错误：请求失败：{e}"
    except (_WebFetchReject, ValueError) as e:
        return f"错误：{e}"
    text = bytes(buf).decode(encoding, errors="replace")
    if ctype in ("text/html", "application/xhtml+xml") or (
            not ctype and text.lstrip()[:1] == "<"):
        text = _html_to_text(text)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n…（正文过长，已截断至前 {max_chars} 字符）"
    return f"[HTTP {status} · {ctype or '未知类型'}]\n{text or '（空正文）'}"


def tool_speedtest_mirrors(ctx: ToolContext, args: dict) -> str:
    """测速镜像源（内置+用户添加），返回最快优先排序；结果落盘与看板共享。

    纯只读探测（HTTP Range GET 前 1KB，绕过系统代理直连），不改项目文件——
    不属于 MUTATING_TOOLS，各审批模式下都不挂起。
    """
    rt = ctx.mirror_runtime
    if rt is None:
        return ("错误：镜像源运行时不可用（配置来源未知）。"
                "请按 env-setup 技能文档的默认镜像顺序下载。")
    category = str(args.get("category") or "").strip() or None
    if category is not None and category not in CATEGORY_KEYS:
        return (f"错误：未知镜像类别 {category}"
                f"（可用：{', '.join(CATEGORY_KEYS)}；留空测全部）")
    results = rt.run_speedtest(category)
    if not results:
        return "没有可测速的镜像源（可能全部被禁用），按 env-setup 技能文档默认顺序处理。"
    ok = sum(1 for v in results.values() if v is not None)
    lines = [f"测速完成：{ok}/{len(results)} 个源可达（HTTP 直连探测，ms 越低越快）。"
             "各类别最快优先："]
    for cat in ([category] if category else CATEGORY_KEYS):
        parts = []
        for r in rt.preferred(cat):
            if r["url"] not in results:
                continue  # 本次没测的（禁用/未涉及类别）不列
            ms = results[r["url"]]
            parts.append(f"{r['url']} {ms:.0f}ms" if isinstance(ms, (int, float))
                         else f"{r['url']} 失败")
        if parts:
            lines.append(f"- {CATEGORY_BY_KEY[cat].label}：" + " | ".join(parts))
    lines.append("下载安装包/配置依赖代理（GOPROXY、pip、npm）时按上面顺序用最快的可达源；"
                 "结果已同步到看板设置页与 env-setup 技能的动态优选段。")
    return "\n".join(lines)


def tool_create_tasks(ctx: ToolContext, args: dict) -> str:
    """把当前任务拆分为若干子任务（写入本项目任务队列），并可选关闭当前任务。

    支持：
    - assignee：指派负责角色（项目小组模式下校验是否在已启用可指派角色内）；
    - depends_on / depends_on_refs：声明外部前置任务 ID 或批内下标依赖，落盘前校验环依赖；
    - parent_id / split_depth / created_by：防递归无限拆分与链路追踪。

    延迟导入 task_model：它经 config 依赖本模块，模块级导入会成环。
    """
    from .task_model import (
        append_progress,
        detect_dependency_cycle,
        parse_task_file,
        priority_rank,
        set_task_file_status,
    )

    task = ctx.current_task
    if task is None or getattr(task, "path", None) is None:
        return "错误：无法确定当前任务（上下文未注入任务信息），不能拆分"
    items = args.get("tasks")
    if not isinstance(items, list) or not (1 <= len(items) <= 10):
        return "错误：tasks 必须是 1~10 个 {title, description?, priority?} 的数组"

    # 项目小组模式下的防无限递归拆分校验
    parent_depth = int(getattr(task, "split_depth", 0) or 0)
    if ctx.team_roles is not None and ctx.current_role:
        cur_info = ctx.team_roles.get(ctx.current_role, {})
        if cur_info.get("role") == "pm" and parent_depth >= max(1, int(ctx.max_split_depth or 1)):
            return (
                f"错误：当前任务拆分深度已达上限（split_depth={parent_depth}），"
                "禁止继续拆分，请使用 reassign_task 直接指派给执行角色。"
            )

    tasks_dir = Path(task.path).parent
    done_dir = tasks_dir / "done"
    existing_stems = {p.stem for p in tasks_dir.glob("*.md")} if tasks_dir.is_dir() else set()
    done_stems = {p.stem for p in done_dir.glob("*.md")} if done_dir.is_dir() else set()
    known_task_ids = existing_stems | done_stems
    used_stems = set(known_task_ids)

    orders = []
    if tasks_dir.is_dir():
        for p in tasks_dir.glob("*.md"):
            try:
                o = parse_task_file(p).order
            except Exception:  # noqa: BLE001 — 坏文件跳过，不挡拆分
                continue
            if o is not None:
                orders.append(o)
    next_order = (max(orders) + 1) if orders else 1
    # 优先级缺省继承当前任务；合法性与 web 层同标准（P0~P3）
    default_priority = str(getattr(task, "priority", "") or "P2").upper()

    # 小组模式下解析可指派的目标角色清单（排除 PM 自身，除非组里全为 PM）
    assignable_roles: set[str] | None = None
    if ctx.team_roles is not None:
        non_pm = {k for k, v in ctx.team_roles.items() if v.get("role") != "pm"}
        assignable_roles = non_pm if non_pm else set(ctx.team_roles.keys())

    # 第一遍：字段校验与文件名预分配（保证校验失败零半成品落盘）
    prepared: list[dict] = []
    for idx, it in enumerate(items):
        if not isinstance(it, dict):
            return "错误：tasks 的每项必须是 {title, description?, priority?} 对象"
        title = str(it.get("title") or "").strip()
        if not title:
            return "错误：子任务 title 不能为空"
        if len(title) > 80:
            return f"错误：子任务 title 过长（≤80 字符）：{title[:30]}…"
        priority = str(it.get("priority") or default_priority).strip().upper()
        if priority_rank(priority) > 4:
            return f"错误：子任务优先级不合法：{priority}（P0~P3）"
        description = str(it.get("description") or "").strip()

        assignee = str(it.get("assignee") or "").strip()
        if assignable_roles is not None:
            if not assignee:
                return (
                    f"错误：第 {idx + 1} 个子任务「{title}」必须指定 assignee 负责角色。"
                    f"本项目当前可用角色：{', '.join(sorted(assignable_roles))}"
                )
            if assignee not in assignable_roles:
                return (
                    f"错误：第 {idx + 1} 个子任务指派的角色 {assignee!r} 不可用或未启用。"
                    f"本项目当前可用角色：{', '.join(sorted(assignable_roles))}"
                )

        labels_raw = it.get("labels")
        labels: list[str] = []
        if isinstance(labels_raw, list):
            labels = [str(x).strip() for x in labels_raw if str(x).strip()]

        ext_deps_raw = it.get("depends_on")
        ext_deps: list[str] = []
        if isinstance(ext_deps_raw, list):
            ext_deps = [str(x).strip() for x in ext_deps_raw if str(x).strip()]

        refs_raw = it.get("depends_on_refs")
        refs: list[int] = []
        if refs_raw is not None:
            if not isinstance(refs_raw, list):
                return f"错误：第 {idx + 1} 个子任务的 depends_on_refs 必须是整数下标数组"
            for r in refs_raw:
                if not isinstance(r, int) or isinstance(r, bool) or not (0 <= r < len(items)):
                    return f"错误：第 {idx + 1} 个子任务的 depends_on_refs 下标 {r!r} 越界（有效 0~{len(items) - 1}）"
                if r == idx:
                    return f"错误：第 {idx + 1} 个子任务不能依赖自身（depends_on_refs={r}）"
                refs.append(r)

        # 文件名与 web 层 _auto_task_id 同规则：空白转 -、去非法字符、截 40
        slug = re.sub(r"\s+", "-", title)
        slug = re.sub(r"[^\w\-]", "", slug).strip("-")[:40].strip("-") or "task"
        base = f"{priority}-{slug}"
        stem, n = base, 2
        while stem in used_stems:
            stem = f"{base}-{n}"
            n += 1
        used_stems.add(stem)

        prepared.append({
            "stem": stem,
            "title": title,
            "priority": priority,
            "description": description,
            "assignee": assignee,
            "labels": labels,
            "ext_deps": ext_deps,
            "refs": refs,
        })

    # 第二遍：翻译批内下标依赖 depends_on_refs + 校验存在性 + 环检测
    planned_stems = {p["stem"] for p in prepared}
    all_valid_ids = known_task_ids | planned_stems
    extra_edges: dict[str, list[str]] = {}
    for p in prepared:
        merged_deps: list[str] = []
        seen_d: set[str] = set()
        for r in p["refs"]:
            target = prepared[r]["stem"]
            if target not in seen_d:
                seen_d.add(target)
                merged_deps.append(target)
        for d in p["ext_deps"]:
            if d == p["stem"]:
                return f"错误：子任务 {p['stem']} 不能依赖自身"
            if d not in all_valid_ids:
                return f"错误：子任务「{p['title']}」引用的前置任务 {d!r} 不存在"
            if d not in seen_d:
                seen_d.add(d)
                merged_deps.append(d)
        p["depends_on"] = merged_deps
        if merged_deps:
            extra_edges[p["stem"]] = merged_deps

    if extra_edges:
        cyc = detect_dependency_cycle(tasks_dir, extra_edges=extra_edges)
        if cyc:
            return f"错误：子任务依赖存在循环（{' -> '.join(cyc)}），请调整 depends_on / depends_on_refs"

    # 第三遍：统一落盘
    created: list[str] = []
    for p in prepared:
        t_val = p["title"]
        t_yaml = (json.dumps(t_val, ensure_ascii=False)
                  if any(ch in t_val for ch in (":", "#", "[", "]", "{", "}", '"', "'"))
                  else t_val)
        fm = [
            "---",
            f"title: {t_yaml}",
            f"priority: {p['priority']}",
            "status: open",
            f"order: {next_order}",
        ]
        if p["assignee"]:
            fm.append(f"assignee: {p['assignee']}")
        if p["labels"]:
            fm.append("labels: [" + ", ".join(p["labels"]) + "]")
        if p["depends_on"]:
            fm.append("depends_on: [" + ", ".join(p["depends_on"]) + "]")
        if ctx.team_roles is not None or parent_depth > 0:
            fm.append(f"parent_id: {task.id}")
            fm.append(f"split_depth: {parent_depth + 1}")
            if ctx.current_role:
                fm.append(f"created_by: {ctx.current_role}")
        fm.append("---")
        next_order += 1
        body = p["description"] or p["title"]
        (tasks_dir / f"{p['stem']}.md").write_text(
            "\n".join(fm) + "\n\n" + body + "\n", encoding="utf-8")
        created.append(p["stem"])

    lines = [f"已创建 {len(created)} 个子任务（status=open，按优先级与手动序领取）："
             + "、".join(created)]
    if bool(args.get("close_current", True)):
        set_task_file_status(Path(task.path), "done")
        append_progress(Path(task.path),
                        f"✂ 已成功拆分为 {len(created)} 个子任务：{'、'.join(created)}"
                        "（原任务拆分完成并归档，由子任务接力推进）")
        lines.append(f"已创建 {len(created)} 个子任务并指派。原任务已圆满完成拆分，"
                     "请立即调用 finish(action=\"split\", summary=一句话拆分总结) 进行归档交接。")
    else:
        lines.append("当前任务保持原状态。")
    log.info("create_tasks：%s（当前任务 %s）", created, task.id)
    return "\n".join(lines)


def tool_report_bug(ctx: ToolContext, args: dict) -> str:
    """测试工程师（QA）在联调中发现问题时，向项目任务队列新建 Bug 任务。

    - 强制校验复现步骤、期望与实际现象，避免无效空泛 Bug；
    - 登记到 ctx.created_bugs，配合 finish(action="wait_retest") 自动挂靠为本 QA
      任务的回归前置依赖（待全部 Bug 修复归档后自动唤醒 QA 复测）。
    """
    from .task_model import append_progress, parse_task_file, priority_rank

    task = ctx.current_task
    if task is None or getattr(task, "path", None) is None:
        return "错误：无法确定当前任务，不能创建 Bug 任务"

    items = args.get("bugs")
    if not isinstance(items, list) or not (1 <= len(items) <= 10):
        return "错误：bugs 必须是 1~10 个 Bug 描述对象的数组"

    tasks_dir = Path(task.path).parent
    done_dir = tasks_dir / "done"
    existing_stems = {p.stem for p in tasks_dir.glob("*.md")} if tasks_dir.is_dir() else set()
    done_stems = {p.stem for p in done_dir.glob("*.md")} if done_dir.is_dir() else set()
    used_stems = existing_stems | done_stems

    orders = []
    if tasks_dir.is_dir():
        for p in tasks_dir.glob("*.md"):
            try:
                o = parse_task_file(p).order
            except Exception:  # noqa: BLE001
                continue
            if o is not None:
                orders.append(o)
    next_order = (max(orders) + 1) if orders else 1

    fix_roles: set[str] | None = None
    if ctx.team_roles is not None:
        devs = {k for k, v in ctx.team_roles.items() if v.get("role") not in ("pm", "qa")}
        fix_roles = devs if devs else {k for k in ctx.team_roles if k != ctx.current_role}

    prepared: list[dict] = []
    for idx, it in enumerate(items):
        if not isinstance(it, dict):
            return "错误：bugs 每项必须是对象"
        title = str(it.get("title") or "").strip()
        if not title:
            return f"错误：第 {idx + 1} 个 Bug 缺少 title"
        if len(title) > 80:
            return f"错误：第 {idx + 1} 个 Bug 标题过长（≤80 字符）"
        repro = str(it.get("repro_steps") or "").strip()
        expected = str(it.get("expected") or "").strip()
        actual = str(it.get("actual") or "").strip()
        if not repro or not expected or not actual:
            return f"错误：第 {idx + 1} 个 Bug「{title}」必须填写 repro_steps、expected 与 actual"

        severity = str(it.get("severity") or "major").strip().lower()
        if severity not in ("blocker", "major", "minor"):
            return f"错误：第 {idx + 1} 个 Bug 的 severity 不合法：{severity}（应为 blocker/major/minor）"

        priority = str(it.get("priority") or "P1").strip().upper()
        if priority_rank(priority) > 4:
            return f"错误：第 {idx + 1} 个 Bug 优先级不合法：{priority}（P0~P3）"

        assignee = str(it.get("assignee") or "").strip()
        if fix_roles is not None:
            if not assignee:
                return (
                    f"错误：第 {idx + 1} 个 Bug「{title}」必须指派修复角色 assignee。"
                    f"可用开发角色：{', '.join(sorted(fix_roles))}"
                )
            if assignee not in fix_roles:
                return (
                    f"错误：第 {idx + 1} 个 Bug 指派的角色 {assignee!r} 无效或不可接 Bug。"
                    f"可用开发角色：{', '.join(sorted(fix_roles))}"
                )
        elif not assignee:
            assignee = "backend"

        evidence = str(it.get("evidence") or "").strip()
        slug = re.sub(r"\s+", "-", title)
        slug = re.sub(r"[^\w\-]", "", slug).strip("-")[:36].strip("-") or "issue"
        base = f"{priority}-bug-{slug}"
        stem, n = base, 2
        while stem in used_stems:
            stem = f"{base}-{n}"
            n += 1
        used_stems.add(stem)

        prepared.append({
            "stem": stem,
            "title": title,
            "priority": priority,
            "severity": severity,
            "assignee": assignee,
            "repro": repro,
            "expected": expected,
            "actual": actual,
            "evidence": evidence,
        })

    created: list[str] = []
    parent_depth = int(getattr(task, "split_depth", 0) or 0)
    for p in prepared:
        fm = [
            "---",
            f"title: {json.dumps('[Bug] ' + p['title'], ensure_ascii=False)}",
            f"priority: {p['priority']}",
            "status: open",
            f"order: {next_order}",
            f"assignee: {p['assignee']}",
            f"labels: [bug, qa-found, {p['severity']}]",
            f"parent_id: {task.id}",
            f"split_depth: {max(1, parent_depth)}",
            f"created_by: {ctx.current_role or 'qa'}",
            "---",
        ]
        next_order += 1
        body_parts = [
            f"## 缺陷说明\n\n- 来源联调任务：{task.id}\n- 严重程度：{p['severity']}\n- 责任角色：{p['assignee']}",
            f"## 复现步骤\n\n{p['repro']}",
            f"## 期望结果\n\n{p['expected']}",
            f"## 实际现象\n\n{p['actual']}",
        ]
        if p["evidence"]:
            body_parts.append(f"## 调试证据\n\n{p['evidence']}")
        (tasks_dir / f"{p['stem']}.md").write_text(
            "\n".join(fm) + "\n\n" + "\n\n".join(body_parts) + "\n",
            encoding="utf-8",
        )
        created.append(p["stem"])
        ctx.created_bugs.append(p["stem"])

    append_progress(
        Path(task.path),
        f"🐞 联调发现 {len(created)} 个 Bug 已派发：{'、'.join(created)}",
    )
    log.info("report_bug：%s（来源任务 %s）", created, task.id)
    summary_items = ", ".join(f"{p['stem']} -> {p['assignee']}" for p in prepared)
    return (
        f"已创建 {len(created)} 个 Bug 任务：{summary_items}。\n"
        "请结束本轮调试（如先停止临时开发服务），然后调用 "
        "finish(action=\"wait_retest\", summary=\"发现 Bug 已派发修复，等待回归验证\")，"
        "系统会自动将以上 Bug 挂靠为本测试任务的前置依赖，待修复完成后自动唤醒回归测试。"
    )


def tool_reassign_task(ctx: ToolContext, args: dict) -> str:
    """项目经理（PM）判定无需拆分时，直接将当前任务指派给对应开发/测试角色。"""
    from .task_model import (
        _set_frontmatter_field,
        append_progress,
        priority_rank,
        set_task_file_assignee,
    )

    task = ctx.current_task
    if task is None or getattr(task, "path", None) is None:
        return "错误：无法确定当前任务，不能直接指派"

    assignee = str(args.get("assignee") or "").strip()
    if not assignee:
        return "错误：assignee 不能为空"

    if ctx.team_roles is not None:
        non_pm = {k for k, v in ctx.team_roles.items() if v.get("role") != "pm"}
        valid = non_pm if non_pm else set(ctx.team_roles.keys())
        if assignee not in valid:
            return (
                f"错误：目标角色 {assignee!r} 不在本项目可用角色列表内。"
                f"可用角色：{', '.join(sorted(valid))}"
            )

    priority = str(args.get("priority") or "").strip().upper()
    if priority:
        if priority_rank(priority) > 4:
            return f"错误：优先级不合法：{priority}（P0~P3）"
        _set_frontmatter_field(Path(task.path), None, "priority", priority)

    note = str(args.get("note") or "").strip()
    if not set_task_file_assignee(Path(task.path), assignee):
        return "错误：写入任务指派字段失败"

    ctx.reassigned_to = assignee
    msg = f"👑 项目经理直接指派给 [{assignee}]" + (f"：{note}" if note else "")
    append_progress(Path(task.path), msg)
    log.info("reassign_task：%s -> %s", task.id, assignee)
    return (
        f"已将当前任务直接指派给 {assignee}（无需拆分子任务）。"
        f"请立即调用 finish(action=\"split\", summary=\"单任务直接指派给 {assignee}\") 交接。"
    )


def tool_finish(ctx: ToolContext, args: dict) -> str:
    """Agent 主动结束任务并给出结构化结论；由执行器捕获处理。"""
    raise FinishSignal(args)


class FinishSignal(Exception):
    """工具 finish 抛出的任务结束信号，payload 即结论 JSON 参数。"""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        super().__init__("finish")


class TaskStopped(Exception):
    """用户在看板强制停止任务：循环在轮次/工具边界抛出，执行器按 cancelled 收尾。

    不放在 dispatch_tool 的兜底 except 里吞掉——必须一路冒到执行器。
    """


def tool_delegate_explore(ctx: ToolContext, args: dict) -> str:
    """并发派生多个轻量只读子代理，执行并行代码检索与架构摸底。"""
    # 1. 递归深度熔断：子代理禁止再次派生
    if ctx.is_subagent:
        return (
            "错误：当前已处于子代理执行环境中，严禁递归派生更多子代理（深度上限=1）。"
            "请使用基础只读工具（list_dir / grep / read_file）完成本次调研并 finish。"
        )

    # 2. 调用频次熔断：单任务最多允许调用 2 批次
    if ctx.subagent_call_count >= 2:
        return "错误：当前任务内子代理并发探索已达上限（已调用 2 批次）。请基于已有探索报告推进后续开发或拆单决策。"

    subagents_data = args.get("subagents")
    if not isinstance(subagents_data, list) or not subagents_data:
        return "错误：必须通过 subagents 数组指定 1~4 个子代理任务（每项包含 name 和 focus）。"

    if len(subagents_data) > 4:
        return f"错误：单次最多允许派生 4 个子代理（当前：{len(subagents_data)} 项）。请合并精简后重试。"

    llm_cfg = getattr(ctx, "llm_config", None)
    if not llm_cfg:
        return "错误：当前执行环境未绑定大模型配置，无法派生子代理。"

    from .subagent import run_subagents_fan_out
    ctx.subagent_call_count += 1
    report, usage = run_subagents_fan_out(
        parent_ctx=ctx,
        subagents_data=subagents_data,
        llm_cfg=llm_cfg,  # type: ignore[arg-type]
    )

    if ctx.subagent_usage is not None:
        ctx.subagent_usage["turns"] += usage["turns"]
        ctx.subagent_usage["prompt_tokens"] += usage["prompt_tokens"]
        ctx.subagent_usage["completion_tokens"] += usage["completion_tokens"]

    return report


# ---- 工具注册表 ----

# name -> (描述, 参数 JSON Schema, 实现)
TOOLS: dict[str, tuple[str, dict, object]] = {
    "delegate_explore": (
        "并发派生 2~4 个轻量只读子代理替自己去探索项目的不同模块或排查问题。子代理并行完成代码检索后，系统自动将调查结论汇总返回给你。适合大范围代码摸底、跨端架构调研或对比排查，大幅压缩单兵逐轮阅读的轮数与耗时。",
        {
            "type": "object",
            "properties": {
                "subagents": {
                    "type": "array",
                    "description": "子代理任务清单（建议 2~4 个，最多 4 个）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "子任务简短标题（如「调研后端用户数据模型」）"},
                            "focus": {"type": "string", "description": "子代理需要调研的核心关注点与需要回答的问题"},
                            "paths": {"type": "array", "items": {"type": "string"}, "description": "建议重点关注的相对文件或目录路径（可选）"},
                        },
                        "required": ["name", "focus"],
                    },
                },
            },
            "required": ["subagents"],
        },
        tool_delegate_explore,
    ),
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
    "speedtest_mirrors": (
        "测速镜像源的 HTTP 延迟（内置+用户添加的语言安装包/依赖代理源），返回最快"
        "优先排序并持久化结果（与看板设置页共享）。下载安装包（Go/Node/Python/JDK）"
        "或配置依赖代理（GOPROXY/pip/npm）前调用它选最快的源；若 env-setup 技能正文"
        "已带「镜像源测速优选」段且时间较新，直接用即可、不必重测。",
        {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["", *CATEGORY_KEYS],
                    "description": "只测某类别：go_dist=Go 安装包 / goproxy=Go 模块代理 / "
                                   "npm / pip / node_dist=Node 安装包 / jdk / "
                                   "python_dist=Python 安装包；留空 = 测全部",
                },
            },
            "required": [],
        },
        tool_speedtest_mirrors,
    ),
    "create_tasks": (
        "把当前任务拆分为若干子任务并写入本项目任务队列（每条生成规范任务文件，"
        "状态 open，优先级缺省继承当前任务，手动序接在现有任务之后）。用于执行"
        "**早期**（前几轮）就发现任务体量明显超出单轮预算（预计 15+ 轮、大量新"
        "文件、或设计文档明确要 2 天+ 工作量）时：先拆分，再 finish(action=split)"
        "收尾，拆分后不要再继续当前任务的实现。子任务 title 建议带执行序号"
        "（如「1 model 层补全」）；description 写清范围、对应设计文档章节与"
        "可执行的验收命令（如 go build ./internal/model），并把已验证的现状"
        "（哪些文件已完成可用）写给后续任务，避免重复劳动或误判损坏。",
        {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string",
                                      "description": "子任务标题（≤80 字符，建议带序号）"},
                            "description": {"type": "string",
                                            "description": "任务正文：范围/接口契约/验收命令/已知现状"},
                            "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"],
                                         "description": "缺省继承当前任务优先级"},
                            "assignee": {"type": "string",
                                         "description": "负责执行该子任务的角色 ID（项目小组模式下必填，如 backend / frontend / qa）"},
                            "depends_on": {"type": "array", "items": {"type": "string"},
                                           "description": "依赖的既有前置任务 ID 列表"},
                            "depends_on_refs": {"type": "array", "items": {"type": "integer"},
                                                "description": "依赖本批 tasks 数组内其他子任务的 0-based 下标（如 [0, 1] 表示依赖第 1、2 项）"},
                            "labels": {"type": "array", "items": {"type": "string"},
                                       "description": "任务标签列表（如 [\"backend\", \"api\"]）"},
                        },
                        "required": ["title"],
                    },
                    "description": "1~10 个子任务，按建议执行顺序排列",
                },
                "close_current": {"type": "boolean",
                                  "description": "拆分后完成并归档当前任务（默认 true）"},
            },
            "required": ["tasks"],
        },
        tool_create_tasks,
    ),
    "report_bug": (
        "测试工程师（QA）在联调验证中发现缺陷时，向项目任务列表新建 Bug 修复任务"
        "并指派给对应开发角色（backend/frontend 等）。需写明复现步骤、期望与实际现象。",
        {
            "type": "object",
            "properties": {
                "bugs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Bug 简要标题（≤80 字符）"},
                            "assignee": {"type": "string", "description": "指派修复的开发角色 ID（如 backend / frontend）"},
                            "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"],
                                         "description": "优先级（默认 P1）"},
                            "severity": {"type": "string", "enum": ["blocker", "major", "minor"],
                                         "description": "严重度（默认 major）"},
                            "repro_steps": {"type": "string", "description": "详细复现步骤"},
                            "expected": {"type": "string", "description": "期望正常行为"},
                            "actual": {"type": "string", "description": "实际错误表现"},
                            "evidence": {"type": "string", "description": "接口状态码/Console 报错/截图路径等证据"},
                        },
                        "required": ["title", "repro_steps", "expected", "actual"],
                    },
                    "description": "1~10 个待修复 Bug 清单",
                },
            },
            "required": ["bugs"],
        },
        tool_report_bug,
    ),
    "reassign_task": (
        "项目经理（PM）判断当前需求粒度合适、无需拆分子任务时，直接将其指派给"
        "目标开发/测试角色，避免产生多余子任务文件。",
        {
            "type": "object",
            "properties": {
                "assignee": {"type": "string", "description": "目标责任角色 ID（如 backend / frontend）"},
                "note": {"type": "string", "description": "技术评估与交接说明（将写入任务进度）"},
                "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"],
                             "description": "可选：调整任务优先级"},
            },
            "required": ["assignee"],
        },
        tool_reassign_task,
    ),
    "finish": (
        "任务完成或受阻时调用，给出最终结论。必须提供 action/summary 等字段。",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["completed", "blocked", "cannot_do", "split", "wait_retest"],
                           "description": "completed=已完成且验证通过；blocked=受阻；"
                                          "cannot_do=超出能力或需人工决策；"
                                          "split=任务过大，已用 create_tasks 拆分为子任务；"
                                          "wait_retest=QA 联调发现 Bug 已派发修复，等待修复后回归测试"},
                "summary": {"type": "string"},
                "files_changed": {"type": "array", "items": {"type": "string"}},
                "commit_message": {"type": "string"},
                "notes": {"type": "string"},
                # 可选结构化问题清单：仅审查 agent 的提示词要求提供（任务
                # agent 不提及该字段，不受影响）；不通过时逐条列出问题
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string",
                                     "description": "相对项目根的文件路径"},
                            "line": {"type": "integer",
                                     "description": "行号（可省略）"},
                            "severity": {"type": "string",
                                         "enum": ["blocker", "major", "minor"],
                                         "description": "blocker=阻断验收；major=功能缺陷；minor=次要问题"},
                            "message": {"type": "string",
                                        "description": "问题描述与建议"},
                        },
                        "required": ["file", "severity", "message"],
                    },
                    "description": "结构化问题清单（审查场景使用；每条含 file/severity/message）",
                },
            },
            "required": ["action", "summary"],
        },
        tool_finish,
    ),
}

# 动态载入本地调试工具集（14 个：dev_server 组 + http_request + browser 组）
from .tools_debug import DEBUG_TOOLS
TOOLS.update(DEBUG_TOOLS)

# 工具分组（看板与配置支持按组一键开关）
TOOL_GROUPS: dict[str, tuple[str, ...]] = {
    "browser": (
        "browser_navigate", "browser_snapshot", "browser_click",
        "browser_type", "browser_wait", "browser_screenshot",
        "browser_console", "browser_network", "browser_eval",
        "browser_close",
    ),
    "dev_server": (
        "dev_server_start", "dev_server_logs", "dev_server_stop",
    ),
}

# 默认关闭的工具集合（本地调试相关工具 + 小组协作专属工具；未显式启用时默认关闭，保持单兵模式洁净）
DEFAULT_OFF_TOOLS: frozenset[str] = frozenset(
    TOOL_GROUPS["browser"] + TOOL_GROUPS["dev_server"] + ("http_request", "report_bug", "reassign_task")
)

# 工具业务功能分类定义（供设置页与 API 结构化呈现及批量管理）
TOOL_CATEGORIES = [
    {
        "key": "file_ops",
        "label": "文件操作与编辑",
        "icon": "📁",
        "description": "读取、创建、编辑、重命名及删除工作区代码和文本文件",
        "tools": ["read_file", "write_file", "edit_file", "multi_edit", "delete_file", "move_file", "list_dir"],
    },
    {
        "key": "code_search",
        "label": "代码搜索与定位",
        "icon": "🔍",
        "description": "按通配符模式查找文件以及跨文件正则搜索内容",
        "tools": ["find_files", "grep"],
    },
    {
        "key": "command_exec",
        "label": "终端命令执行",
        "icon": "💻",
        "description": "在项目根目录执行白名单内的构建、测试、检查与系统命令",
        "tools": ["run_command"],
    },
    {
        "key": "task_collab",
        "label": "任务管理与协议",
        "icon": "⚡",
        "description": "拆解多阶段子任务、角色直派、Bug 报告并在任务完成或受阻时给出结构化答复",
        "tools": ["create_tasks", "report_bug", "reassign_task", "finish"],
    },
    {
        "key": "subagent",
        "label": "子代理并发协同",
        "icon": "👥",
        "description": "派生只读子代理并行探索多个模块/目录，单轮拿到汇总结论，压缩串行摸底耗时",
        "tools": ["delegate_explore"],
    },
    {
        "key": "external_net",
        "label": "网络与外部知识",
        "icon": "🌐",
        "description": "查阅专业技能库文档、抓取公网页面正文与镜像源延迟测速",
        "tools": ["read_skill", "web_fetch", "speedtest_mirrors"],
    },
    {
        "key": "local_debug",
        "label": "本地开发服务与 API",
        "icon": "🚀",
        "description": "本地后台常驻服务管理（开发服务启动/日志/停止）与本地 HTTP API 调试",
        "tools": ["dev_server_start", "dev_server_logs", "dev_server_stop", "http_request"],
    },
    {
        "key": "browser_auto",
        "label": "Chromium 浏览器自动化",
        "icon": "🧭",
        "description": "通过 CDP 驱动本地 Chrome/Edge 浏览器进行页面导航、快照、点击、输入与截图",
        "tools": [
            "browser_navigate", "browser_snapshot", "browser_click",
            "browser_type", "browser_wait", "browser_screenshot",
            "browser_console", "browser_network", "browser_eval",
            "browser_close",
        ],
    },
]


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
    """校验「工具名或工具组名 → bool」映射：未知工具名/禁用 finish/非布尔值都直接报错。

    返回清洗后的 dict（空 dict = 默认配置）。风格对齐 config._coerce_llm：
    加载/保存期即失败，中文错误信息经 Web 层透传到看板。
    """
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{context} 必须是「工具名/组名: true/false」的键值对")
    out: dict[str, bool] = {}
    valid_names = set(TOOLS) | set(TOOL_GROUPS)
    for key, value in data.items():
        name = str(key).strip()
        if name not in valid_names:
            raise ValueError(f"{context} 含未知工具或工具组：{name}"
                             f"（可用：{', '.join(sorted(valid_names))}）")
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


def validate_commands_map(data: object, context: str = "commands") -> dict[str, bool]:
    """校验「命令 → bool」映射（全局命令白名单开关）：键必须是默认白名单成员。

    返回清洗后的 dict（空 dict = 默认全放行）。项目级 allowed_commands（整表
    替换语义）不经过这里——那是自由列表，不属于开关。
    """
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{context} 必须是「命令: true/false」的键值对")
    out: dict[str, bool] = {}
    for key, value in data.items():
        name = str(key).strip()
        if name not in DEFAULT_ALLOWED_COMMANDS:
            raise ValueError(f"{context} 含未知命令：{name}"
                             f"（默认白名单：{', '.join(DEFAULT_ALLOWED_COMMANDS)}）")
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
