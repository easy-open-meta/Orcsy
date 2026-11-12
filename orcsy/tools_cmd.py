"""终端命令执行、子进程生命周期管控与本机开发工具链环境探测。

从原 orcsy/tools.py 逐字拆分（Phase 3 渐进式重构 Stage 1）：
- run_command 工具执行；Windows 窗口隐藏、子进程树安全终止与强制停止信号响应；
- PATH 注册表现读与环境自适应补全；UTF-8 / ANSI (GBK) 跨平台编码自适应容错解码。
唯一有意增量：`_registry_path_entries` / `_TOOL_PATH_DIRS` 的测试打桩感知桥
（保持 `orcsy.tools` 命名空间 monkeypatch 行为与重构前逐字节一致）。
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .logging_setup import get_logger
from .tool_specs import DEFAULT_ALLOWED_COMMANDS

if TYPE_CHECKING:
    from .tools import ToolContext

log = get_logger("tools.cmd")

MAX_OUTPUT_CHARS = 6000

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


# 新装工具的可发现性 ----
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
    # 兼容桥：tests/test_env_setup.py 以 `T._registry_path_entries = ...`
    # 打桩 orcsy.tools 命名空间——还原到本模块实现时经此感知，行为与重构前一致
    tools_mod = sys.modules.get("orcsy.tools")
    if tools_mod is not None:
        patched = getattr(tools_mod, "_registry_path_entries", None)
        if patched is not None and patched is not _registry_path_entries:
            return patched()
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
    # 兼容桥：tests/test_env_setup.py 以 `T._TOOL_PATH_DIRS = ...` 打桩
    tools_mod = sys.modules.get("orcsy.tools")
    tool_dirs = _TOOL_PATH_DIRS
    if tools_mod is not None:
        patched_dirs = getattr(tools_mod, "_TOOL_PATH_DIRS", None)
        if patched_dirs is not None and patched_dirs is not _TOOL_PATH_DIRS:
            tool_dirs = patched_dirs
    seen = {_path_key(p) for p in os.environ.get("PATH", "").split(os.pathsep)
            if p.strip()}
    out: list[str] = []
    candidates = _registry_path_entries() + [
        os.path.expandvars(os.path.expanduser(raw)) for raw in tool_dirs]
    for p in candidates:
        key = _path_key(p)
        if key in seen or not os.path.isdir(p):
            continue
        seen.add(key)
        out.append(p)
    return out


def _augmented_env(ctx: ToolContext | None = None) -> dict[str, str]:
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


# 正在运行的 Agent 子进程（git/pytest/npm 等），程序退出时统一终止，避免孤儿进程
_CHILD_PROCS: set = set()
_CHILD_PROCS_LOCK = threading.Lock()


def _kill_process_tree(proc: subprocess.Popen) -> None:
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


def register_child_proc(proc: subprocess.Popen) -> None:
    """注册一个运行中的子进程，供程序退出时统一杀灭。"""
    with _CHILD_PROCS_LOCK:
        _CHILD_PROCS.add(proc)


def unregister_child_proc(proc: subprocess.Popen) -> None:
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
# cp936/GBK），用 utf-8 强解会把中文路径打成「乱码」。改为拿原始字节
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


def _wait_with_stop(ctx: ToolContext, proc: subprocess.Popen,
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
                raise _CommandTimeout(out, timeout)  # noqa: B904 — 与原实现逐字一致


def _drain_after_kill(proc: subprocess.Popen,
                      grace: float = 5.0) -> tuple[bytes, bytes]:
    """杀掉进程树后回收管道内容；EOF 仍不来的话到点放弃并返回空。"""
    try:
        return proc.communicate(timeout=grace)
    except subprocess.TimeoutExpired:
        return b"", b""


class TaskStopped(Exception):
    """用户在看板强制停止任务：循环在轮次/工具边界抛出，执行器按 cancelled 收尾。

    不放在 dispatch_tool 的兜底 except 里吞掉——必须一路冒到执行器。
    """


CMD_TOOLS: dict[str, tuple[str, dict, object]] = {
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
}

__all__ = [
    "CMD_TOOLS",
    "MAX_OUTPUT_CHARS",
    "REG_PATH_KEYS",
    "TaskStopped",
    "_CHILD_PROCS",
    "_NO_WINDOW",
    "_TOOL_PATH_DIRS",
    "_augmented_env",
    "_decode_proc_output",
    "_drain_after_kill",
    "_kill_process_tree",
    "_path_key",
    "_registry_path_entries",
    "_resolve_new_install",
    "_tool_path_dirs",
    "_wait_with_stop",
    "_windows_startupinfo",
    "check_command_allowed",
    "kill_running_children",
    "persist_path_dirs",
    "register_child_proc",
    "scan_tool_env",
    "tool_run_command",
    "unregister_child_proc",
]
