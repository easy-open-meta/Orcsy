"""CLI 入口：task-arranger init | validate | run | scan"""

from __future__ import annotations

import argparse
import shutil
import os
import sys
from pathlib import Path

from . import __version__
from .templates import TEMPLATES_DIR

CONFIG_NAME = "config.yaml"

CONFIG_EXAMPLE = """\
# task-arranger 配置文件
# 复制为 config.yaml 后按需修改；含 api_key 的文件不要提交到仓库。

llm:
  # 协议类型：openai（OpenAI 兼容，覆盖 DeepSeek/GLM/Kimi/通义/Ollama/vLLM 等）或 anthropic
  provider: openai
  api_key: "sk-xxxx"
  # OpenAI 兼容服务示例：
  #   DeepSeek:  https://api.deepseek.com/v1        model: deepseek-chat
  #   GLM智谱:   https://open.bigmodel.cn/api/paas/v4  model: glm-4-plus
  #   Kimi:      https://api.moonshot.cn/v1         model: moonshot-v1-32k
  #   OpenAI:    https://api.openai.com/v1          model: gpt-4o
  #   Ollama:    http://127.0.0.1:11434/v1          model: qwen2.5-coder:14b
  base_url: https://api.deepseek.com/v1
  model: deepseek-chat
  temperature: 0.2
  max_tokens: 8192
  max_context_tokens: 131072 # 模型上下文窗口（tokens）；看板选模型时可自动填充
  enable_thinking: false     # 请求侧开启模型思考（anthropic 必须；多数兼容服务默认返回）
  timeout_seconds: 300

scheduler:
  interval_seconds: 300      # 每轮扫描间隔（秒）
  max_parallel_agents: 1     # 并行 Agent 数：1 串行；>1 时多项目同时执行
  run_once: false            # true 时只执行一轮就退出（适合手动触发/定时任务外部调度）

logging:
  level: INFO
  file: logs/task-arranger.log

web:
  host: 127.0.0.1            # 看板监听地址
  port: 5116                 # 看板端口（被占用时可改这里，或启动时用 --port 覆盖）
  open_browser: true         # 启动时自动打开浏览器

# 受管项目列表：每个项目由一个独立 Agent 负责。
# 推荐直接在看板「项目与设置」页可视化新建（会自动创建任务目录与 AGENT.md）；
# 也可手动添加，格式如下：
projects: []
#  - name: my-app                      # Agent 名称，日志与分支名使用
#    path: D:/Repos/my-app             # 项目根目录（绝对路径）
#    enabled: true
#    tasks_dir: tasks                  # 项目内任务清单目录（相对项目根）
#    guide_file: AGENT.md              # 项目内开发指引文件（相对项目根）
#    max_tasks_per_cycle: 1            # 每轮最多处理的任务数
#    max_agent_turns: 40               # 单个任务的 Agent 工具循环轮数上限
#    # allowed_commands:               # Agent 可执行的命令白名单（缺省用内置默认：
#    #   - python                      #   python/pytest/node/npm/cargo/go/dotnet/git/uv 等）
#    #   - pytest
#    # allowed_branch: main            # 只在该分支上工作；缺省用项目当前分支
"""


def _write_templates(target_root: Path) -> None:
    """把内置模板复制到目标位置（不覆盖已有文件）。"""
    for src in TEMPLATES_DIR.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(TEMPLATES_DIR)
        dst = target_root / rel
        if dst.exists():
            print(f"  跳过（已存在）：{dst}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)
        print(f"  已生成：{dst}")


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    print(f"初始化 task-arranger 目录：{root}")

    config_path = root / CONFIG_NAME
    if config_path.exists():
        print(f"  跳过（已存在）：{config_path}")
    else:
        config_path.write_text(CONFIG_EXAMPLE, encoding="utf-8")
        print(f"  已生成：{config_path}")

    print("\n接着把 config.yaml 里 llm.api_key 改成你的真实密钥，")
    print("并把 projects 指向你的项目。每个受管项目内需要：")
    print(f"  - <项目>/AGENT.md      开发指引（可用模板：{TEMPLATES_DIR / 'AGENT.md'}）")
    print(f"  - <项目>/tasks/*.md    任务清单（可用模板：{TEMPLATES_DIR / 'tasks'}）")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from .config import load_config
    from .task_model import scan_tasks

    config_path = Path(args.config).resolve()
    try:
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"配置无效：{e}")
        return 1

    print(f"配置有效：{config_path}")
    print(f"  模型：{config.llm.provider} {config.llm.model} @ {config.llm.base_url}")
    print(f"  调度：间隔 {config.scheduler.interval_seconds}s，并行 {config.scheduler.max_parallel_agents}")

    problems = 0
    for p in config.projects:
        print(f"\n项目 [{p.name}] {p.path} {'(已禁用)' if not p.enabled else ''}")
        if not p.path.is_dir():
            print(f"  [错误] 项目目录不存在")
            problems += 1
            continue
        guide = p.path / p.guide_file
        if not guide.exists():
            print(f"  [警告] 缺少开发指引 {guide}（将使用默认指引）")
        tasks_dir = p.path / p.tasks_dir
        if not tasks_dir.is_dir():
            print(f"  [警告] 任务目录不存在：{tasks_dir}")
        else:
            open_tasks, errors = scan_tasks(tasks_dir)
            print(f"  open 任务 {len(open_tasks)} 个："
                  + (", ".join(f"{t.id}(P{t.rank})" for t in open_tasks[:10]) or "无"))
            for e in errors:
                print(f"  [警告] 任务文件问题 {e.id}：{e.error}")
    print()
    if problems:
        print(f"发现 {problems} 个错误")
        return 1
    print("校验通过")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .config import load_config
    from .logging_setup import setup_logging
    from .scheduler import Scheduler

    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    if config.scheduler.run_once or args.once:
        config.scheduler.run_once = True

    log_file = Path(config.logging.file)
    if not log_file.is_absolute():
        log_file = config_path.parent / log_file
    setup_logging(config.logging.level, str(log_file))
    scheduler = Scheduler(config)

    import atexit
    atexit.register(scheduler.stop)

    if config.scheduler.run_once:
        scheduler.run_cycle()
        return 0
    scheduler.run_forever()
    return 0


def _candidate_config_dirs() -> list[Path]:
    """配置可能出现的目录：安装器初始化约定位置（文档目录）等。"""
    dirs: list[Path] = []
    docs = Path(os.path.expandvars(r"%USERPROFILE%\Documents"))
    # OneDrive 可能重定向"文档"（如 Z:\OneDrive\文档），注册表读真身
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as k:
                raw, _ = winreg.QueryValueEx(k, "Personal")
                docs = Path(os.path.expandvars(raw))
        except OSError:
            pass
    dirs.append(docs / "TaskArranger")
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).parent)
    return dirs


def find_config(explicit: str | None) -> Path:
    """配置查找顺序：显式路径 > ./config.yaml > run/config.yaml > 文档\\TaskArranger > exe 目录。"""
    if explicit:
        p = Path(explicit).resolve()
        if not p.exists():
            print(f"配置文件不存在：{p}")
            raise SystemExit(1)
        return p
    candidates = [Path("config.yaml"), Path("run/config.yaml")]
    candidates += [d / "config.yaml" for d in _candidate_config_dirs()]
    for cand in candidates:
        if cand.exists():
            return cand.resolve()
    searched = ", ".join(str(c) for c in candidates[:3])
    print(f"未找到 config.yaml（已尝试：{searched} 等）。")
    print("请运行 task-arranger init 生成，或用 --config 指定路径；")
    print("推荐直接在看板「项目与设置」页可视化管理。")
    raise SystemExit(1)


def cmd_web(args: argparse.Namespace) -> int:
    import socket
    import threading
    import webbrowser

    from .config import load_config
    from .logging_setup import setup_logging
    from .scheduler import Scheduler
    from .web import AppState, _load_page, make_server

    # 安装器辅助：只写自启注册表后退出（不启服务）
    if getattr(args, "tray_autostart_on", False):
        from .autostart import enable

        enable()
        return 0

    config_path = find_config(args.config)
    # Web 模式允许暂无 api_key：先把看板跑起来，模型参数可后配
    config = load_config(config_path, require_api_key=False)

    # 端口/地址：命令行 > 配置文件；端口冲突时自动换一个空闲端口
    host = args.host or config.web.host
    port = args.port if args.port is not None else config.web.port
    if not (0 < port < 65536):
        print(f"端口号不合法：{port}（应为 1~65535）")
        return 1

    def port_free(h: str, p: int) -> bool:
        # 不设 SO_REUSEADDR：Windows 上它允许绑到正在监听的端口，探测会失真
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((h, p))
                return True
            except OSError:
                return False

    fallback_notice = ""
    if not port_free(host, port):
        import urllib.request

        # 端口被占：可能是已有的本系统实例，探测一下
        existing = False
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/api/state", timeout=2) as r:
                existing = r.status == 200
        except Exception:
            existing = False
        if existing and not args.port:
            print(f"端口 {port} 上已有任务编排看板在运行：http://{host}:{port}")
            print("如需另起一个实例，用 --port 指定其它端口。")
            return 0
        if args.port:
            print(f"端口 {port} 被其它程序占用，无法使用。")
            print(f"请换一个端口（如 task-arranger web --port {port + 1}），"
                  f"或结束占用该端口的程序。")
            return 1
        # 配置文件里的端口被占且不是本系统：自动挑一个空闲端口
        import random
        for _ in range(20):
            candidate = random.randint(20000, 60000)
            if port_free(host, candidate):
                fallback_notice = (
                    f"提示：配置的端口 {port} 已被占用，本次改用空闲端口 {candidate}；"
                    f"想固定端口可改 config.yaml 的 web.port 或启动时加 --port。"
                )
                print(fallback_notice)
                port = candidate
                break
        else:
            print(f"端口 {port} 被占用，且自动寻找空闲端口失败，请手动指定 --port。")
            return 1

    config.web.host, config.web.port = host, port
    # 相对日志路径锚定到配置文件所在目录：工作目录可能在 Program Files（不可写）
    log_file = Path(config.logging.file)
    if not log_file.is_absolute() and config.source_path:
        log_file = config.source_path.parent / log_file
    setup_logging(config.logging.level, str(log_file))

    scheduler = Scheduler(config)
    state = AppState(config, scheduler, log_path=str(log_file))
    _load_page()  # 启动即缓存看板页面（冻结环境下 _MEI 文件可能稍后不可读）
    server = make_server(state, host, port)
    url = f"http://{host}:{port}"

    log = __import__("logging").getLogger("task_arranger.web")
    log.info("看板服务启动：%s（配置：%s）", url, config_path)
    # 提示信息同时打到 stderr，避免被 logging 配置吞掉或缓冲
    print(f"任务编排看板已启动：{url} （Ctrl+C 或右键托盘图标退出）", file=sys.stderr, flush=True)
    if fallback_notice:
        print(fallback_notice, file=sys.stderr, flush=True)
    print(f"配置文件：{config_path}", file=sys.stderr, flush=True)
    if not config.llm.api_key:
        print("提示：config.yaml 中 llm.api_key 尚未配置，Agent 执行任务前请先填写。",
              file=sys.stderr, flush=True)

    # 调度循环：run_once 配置优先，否则按间隔持续跑
    if config.scheduler.run_once or args.no_sched:
        pass  # 只由看板按钮手动触发
    else:
        scheduler.run_forever_background()

    if config.web.open_browser and not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    # 退出清理：无论 Ctrl+C、窗口关闭、托盘退出还是异常，都摘图标、停调度、关端口、收子进程
    def _cleanup() -> None:
        scheduler.stop()
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
        try:
            from .tray import stop_tray

            stop_tray()
        except Exception:
            pass
        from .tools import kill_running_children
        killed = kill_running_children()
        if killed:
            print(f"已终止 {killed} 个执行中的 Agent 子进程", file=sys.stderr, flush=True)

    import atexit
    atexit.register(_cleanup)

    # Windows 控制台事件（Ctrl+C / 关窗口 / 注销）：Ctrl+C 已由 KeyboardInterrupt 覆盖，
    # 这里主要兜住"直接关窗口"——Python 默认对此只给 5 秒且不做清理，导致退出不彻底。
    if sys.platform == "win32":
        import ctypes

        _HANDLER_REF = None  # 防 GC

        def _console_handler(event_type):  # noqa: ANN001
            threading.Thread(target=_cleanup, daemon=True).start()
            return False  # 让默认终止流程继续

        _HANDLER_REF = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)(_console_handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_HANDLER_REF, True)

    # 托盘图标与控制台并存：图标放后台线程常驻，主线程留在 serve_forever 上，
    # 控制台 Ctrl+C 才能以 KeyboardInterrupt 打断。托盘「退出」菜单反向调
    # server.shutdown() 唤醒主线程，两种退出统一走 _cleanup。
    if not args.no_tray:
        def _run_tray() -> None:
            from .tray import run_tray

            try:
                run_tray(state, scheduler, url, server)
            except Exception:  # noqa: BLE001 托盘起不来不应拖垮看板服务
                __import__("logging").getLogger("task_arranger.web").exception(
                    "托盘图标启动失败（看板服务不受影响）")

        threading.Thread(target=_run_tray, name="tray", daemon=True).start()

    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        _cleanup()
        atexit.unregister(_cleanup)
        print("看板服务已停止", file=sys.stderr, flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    # 托盘 exe 允许省略子命令：`task-arranger-tray.exe --tray --config ...`
    # 等价于 `web --tray ...`（自启动注册表与快捷方式都这样写；--tray 现为兼容参数）
    if argv and "--tray" in argv and argv[0] not in ("init", "validate", "run", "web"):
        argv = ["web", *argv]
    parser = argparse.ArgumentParser(
        prog="task-arranger",
        description="项目任务编排系统：定时扫描各项目任务列表，由大模型 Agent 按优先级完成功能开发与 bug 修复。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="初始化运行目录：生成 config.yaml 与项目模板")
    p_init.add_argument("--dir", default=".", help="目标目录（默认当前目录）")
    p_init.set_defaults(func=cmd_init)

    p_val = sub.add_parser("validate", help="校验配置与各项目任务清单")
    p_val.add_argument("--config", default=CONFIG_NAME, help="配置文件路径（默认 ./config.yaml）")
    p_val.set_defaults(func=cmd_validate)

    p_run = sub.add_parser("run", help="启动调度：默认持续运行，--once 只执行一轮")
    p_run.add_argument("--config", default=CONFIG_NAME, help="配置文件路径（默认 ./config.yaml）")
    p_run.add_argument("--once", action="store_true", help="只执行一轮扫描后退出")
    p_run.set_defaults(func=cmd_run)

    p_web = sub.add_parser("web", help="启动 Web 可视化看板（含调度，默认自动打开浏览器）")
    p_web.add_argument("--config", default=None,
                       help="配置文件路径（默认自动发现 ./config.yaml 或 ./run/config.yaml）")
    p_web.add_argument("--port", type=int, default=None,
                       help="看板端口（覆盖 config.yaml 的 web.port；被占时自动换空闲端口）")
    p_web.add_argument("--host", default=None,
                       help="监听地址（覆盖 config.yaml 的 web.host，默认 127.0.0.1）")
    p_web.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p_web.add_argument("--no-sched", action="store_true", help="不启动定时调度（仅手动触发）")
    p_web.add_argument("--tray", action="store_true",
                       help="兼容参数：托盘图标已默认随看板启动（开机自启动仍带此参数）")
    p_web.add_argument("--no-tray", action="store_true",
                       help="不启动托盘图标（纯控制台运行）")
    p_web.add_argument("--tray-autostart-on", action="store_true",
                       help=argparse.SUPPRESS)  # 安装器专用：写自启注册表后退出
    p_web.set_defaults(func=cmd_web)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
