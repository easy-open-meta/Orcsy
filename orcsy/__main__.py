"""CLI 入口：orcsy init | validate | run | scan"""

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
# orcsy 配置文件
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
  timeout_seconds: 300       # 单次模型请求超时（秒）；-1 = 不超时（挂起时看板「强制停止」
                             # 可打断）；思考型模型/长任务建议 600~900 或 -1

scheduler:
  interval_seconds: 300      # 每轮扫描间隔（秒）
  max_parallel_agents: 1     # 并行 Agent 数：1 串行；>1 时多项目同时执行
  run_once: false            # true 时只执行一轮就退出（适合手动触发/定时任务外部调度）

logging:
  level: INFO
  file: logs/orcsy.log
  retention_days: 7          # 日志保留天数：写满 N 天归档进 logs/archive/，超过 N 天的旧日志清理；0 关闭
  max_total_mb: 200          # 日志总占用上限（MB）：活动日志+滚动备份+归档合计超限即从最旧删起；0 不限制

web:
  host: 127.0.0.1            # 看板监听地址
  port: 5116                 # 看板端口（被占用时可改这里，或启动时用 --port 覆盖）
  open_browser: true         # 启动时自动打开浏览器
  auth_password: ""          # 看板登录密码：非空则所有页面/API 需先登录；
                             # host 非回环（0.0.0.0 / 局域网 IP）时必须设置，否则拒绝启动；
                             # 留空 = 关闭鉴权，仅限 127.0.0.1 本机使用（零打扰）；
                             # 密码以 PBKDF2 密文存储，忘记时运行 orcsy reset-password 快捷重置

# tools:                     # Agent 工具开关（全局默认）：未列出的常规工具默认启用。
#   browser: true            # 可一键按组开关：browser（10个浏览器工具）/ dev_server（3个后台服务工具）；
#   dev_server: true         # 亦可单独开关 http_request；本地调试类工具默认关闭，按需激活。
#   run_command: false       # 常规工具默认启用，可置 false 单独禁用：list_dir / read_file /
#   web_fetch: false         #   write_file / edit_file / multi_edit / delete_file / move_file /
#                            #   grep / find_files / run_command / read_skill / web_fetch；
#                            #   finish 是任务结束的协议工具，不可禁用

# commands:                  # 命令白名单开关（对默认白名单逐命令禁用/启用）：
#   git: false               # 例：禁用 git（run_command 将拒绝一切 git 命令）；
#   msbuild: false           # 未列出的命令保持默认放行。项目级 allowed_commands
#                            # 仍可整表替换默认（见 projects 示例）
#                            # 默认白名单含 python/node/cargo/go/dotnet/mvn/
#                            # gradle/cmake/git、winget/scoop/choco 等包管理器、
#                            # powershell/curl/tar/setx（env-setup 镜像直下需要）
#                            # 及 where/cmd/dir、ls/cat/wc/head/tail/grep 等
#                            # 只读探测/统计命令（sed/awk/rm 等可写盘的不放）

# env:                       # 注入 Agent 子进程（run_command）的环境变量：
#   vars:                    #   国内网络拉依赖失败（EOF/超时）时配镜像/代理即可，
#     GOPROXY: https://goproxy.cn,direct      # Go 模块镜像
#     # HTTP_PROXY: http://127.0.0.1:7890    # 有代理时（Node/Python 一并生效）
#     # PIP_INDEX_URL: https://pypi.tuna.tsinghua.edu.cn/simple
#                            # 不允许覆盖 PATH/SYSTEMROOT/COMSPEC/WINDIR
#   path_autofix: false      # 任务启动时自动扫描本机工具（node/npm/go/java 等），
#                            # 缺失目录追加写入用户 PATH（HKCU，只增不删、写前
#                            # 备份）；默认 false——系统级修改，看板设置页可开

# mirrors:                   # 镜像源管理（看板设置页「镜像源管理与测速选源」可视化编辑）：
#   custom:                  #   用户添加的镜像源（按类别分组）；Agent 下载安装包/配依赖
#     goproxy:               #   代理前测速，自动用最快的源。类别：go_dist（Go 安装包）/
#       - https://goproxy.internal.example.com/  # goproxy（Go 模块代理）/ npm / pip /
#   disabled:                #   node_dist（Node 安装包）/ jdk / python_dist（Python 安装包）
#     - https://goproxy.io   #   disabled 为禁用的源 URL（内置/自定义皆可禁用）；
#                            #   测速结果存同目录 mirror_speeds.json，不写入本文件

permissions:
  mode: full_access          # Agent 访问权限（全局默认）：
                             #   full_access   完全访问——不询问，减少确认次数（默认，与历史行为一致）
                             #   autonomous    自主模式——仅高风险操作询问（delete_file / move_file / run_command）
                             #   ask_on_change 变更前询问——一切变更操作询问（写/改/删/移文件、执行命令）
                             # 询问 = 任务挂起等待看板顶栏「🔔 待审批」批准/拒绝；项目级可用
                             # projects[].permission_mode 覆盖
  timeout_minutes: 30        # 审批挂起超时（分钟，1~1440）：无人响应自动拒绝，任务不会永久挂起

update:
  repo: https://gitee.com/java-and-net/Orcsy   # Gitee 更新源仓库（默认官方源，可改成自己的镜像；Release 附件放安装包）
  token: ""                    # 私有仓库的私人令牌；公开仓库可留空
  auto_check: true             # 启动后自动检查一次更新

notify:
  sound: true                  # 任务结束提示音：完成/受阻时播程序内置提示音（cancelled 不提醒）
  system: true                 # 系统通知：完成/受阻时弹托盘气泡（看板没开也能收到；--no-tray 启动则无渠道）

# review:                    # 任务审查（Review Agent）：任务完成合并前由独立审查
#   enabled: true            #   agent 检查任务成果，通过才合并主分支并归档。
#   llm: ~                   #   审查模型绑定（provider/api_key/base_url/model 等，同 llm
#                            #   字段）；默认 ~（不配置）= 审查未启用，行为与旧版一致。
#                            #   配置后即对全部项目生效（项目级 review_enabled 可关）；
#                            #   审查模型必须与任务模型不同（同名配置拒绝启动/保存）
#   max_turns: 10            #   审查工具循环轮数上限（1~100，含跑测试的轮次）
#   max_review_rounds: 2     #   返工次数全局默认（1~10）：累计审查不通过达到上限即转
#                            #   blocked 等人工；项目级 review_max_rounds 可覆盖
#   allow_test_commands: true  # 审查 agent 可用 run_command 跑测试/构建验证（受项目
#                            #   命令白名单与访问权限审批门约束）；false = 纯只读审查
#   squash_merge: true       #   返工任务通过审查后 merge --squash 收敛 WIP 提交为
#                            #   单提交；false = 一律 --no-ff

# local_debug:               # 本地调试配置（驱动 Chrome/Edge 浏览器与管理后台服务）：
#   browser: auto            #   浏览器引擎：auto（优先 Chrome，无则回退 Edge）/ chrome / edge
#   browser_executable: ""   #   自定义可执行文件路径；留空自动探测
#   headless: true           #   true=无头静默；false=桌面弹窗（可直观围观 Agent 操作页面）
#   allowed_hosts:           #   允许访问的主机白名单（防内网渗透；硬禁区如元数据地址恒拒）
#     - localhost
#     - 127.0.0.1
#     - "::1"
#   navigation_timeout_seconds: 30    # 页面导航及等待超时（秒，5~300）
#   service_ready_timeout_seconds: 60 # 后台开发服务就绪探针超时（秒，5~600）
#   max_services: 3                   # 单任务最大后台并发服务数（1~10）
#   artifacts_retention_days: 7       # 截图工件保留天数（0=不清理）

# git:                         # Git 自动化工作流配置：
#   commit_and_merge: false    # 任务完成后是否自动提交代码并合并回主分支（默认关闭：
#                              # 改动直接留在工作区，方便人工检查与自由提交；
#                              # 开启后在独立 worktree 分支完成开发并自动提交合并）

# agent_profiles:              # 全局 Agent 角色预设池（多模型项目小组使用，可选）：
#   pm_default:                #   预设 ID
#     name: "项目经理 PM"        #   显示名称
#     role: pm                 #   角色类型：pm | backend | frontend | qa | custom
#     llm:                     #   独立模型绑定（缺省字段继承全局 llm）
#       model: claude-3-7-sonnet-20250219
#     max_turns: 20            #   工具循环轮数上限
#     extra_prompt: "拆分子任务时写清接口契约与验收命令"
#   backend_default:
#     name: "后端开发"
#     role: backend
#     llm:
#       model: deepseek-chat
#   frontend_default:
#     name: "前端开发"
#     role: frontend
#     llm:
#       model: claude-3-7-sonnet-20250219
#   qa_default:
#     name: "测试工程师"
#     role: qa
#     llm:
#       model: gpt-4o
#     allow_test_scripts: false # 是否允许编写自动化测试脚本（默认只读不改源码）

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
#    max_agent_turns: 40               # 单个任务的 Agent 工具循环轮数上限（-1 = 不设上限）
#    # allowed_commands:               # Agent 可执行的命令白名单（缺省用内置默认：
#    #   - python                      #   python/pytest/node/npm/pnpm/yarn/cargo/go/
#    #   - pytest                      #   dotnet/mvn/gradle/cmake/ctest/git 等）
#    # allowed_branch: main            # 只在该分支上工作；缺省用项目当前分支
#    # llm:                            # 项目专属模型绑定（部分覆盖：只写要换的字段，
#    #   provider: openai              # 其余继承全局 llm 配置；删除本节即回到全局）
#    #   base_url: https://open.bigmodel.cn/api/paas/v4
#    #   api_key: sk-yyy
#    #   model: glm-4-plus
#    # review_enabled: true            # 项目级审查开关（true/false/缺省 = 跟随全局 review.enabled）
#    # review_llm:                     # 项目级审查模型绑定（部分覆盖全局 review.llm；
#    #   model: deepseek-chat          #   删除本节即回到全局审查模型）
#    # review_max_rounds: 3            # 项目级返工次数覆盖（1~10；缺省 = 跟随全局
#    #                                 # review.max_review_rounds）
#    # debug_allowed_hosts:            # 项目级追加的调试主机白名单（只增不减）：
#    #   - 192.168.1.50                #   局域网测试机 / 内部测试域名
#    #   - dev.local
#    # repo:                           # 绑定远程仓库（GitHub/Gitee/GitLab/Gitea），
#    #   url: https://github.com/owner/repo   # 看板「拉取 Issues」把 open Issue 导入任务
#    #   token: ghp_xxx               # token 可选：私有仓库必填，公开仓库可省
#    #   auto_pull_interval: 600      # 定时自动拉取间隔（秒），最小 60；缺省 = 仅手动拉取
#    # tools:                          # 项目级工具开关覆盖：按工具名逐项覆盖全局 tools
#    #   browser: true                 # 一键开启本地浏览器相关工具（默认关闭）
#    #   dev_server: true              # 一键开启后台开发服务管理工具（默认关闭）
#    #   http_request: true            # 开启本地 API 请求调试工具（默认关闭）
#    # permission_mode: autonomous     # 项目级访问权限覆盖（ask_on_change / autonomous /
#    #                                 # full_access）；缺省 = 跟随全局 permissions.mode
#    # commit_and_merge: true          # 项目级覆盖：任务完成后自动提交并合并代码（缺省 = 跟随全局 git.commit_and_merge 关闭）
#    # team:                           # 多模型 Agent 项目小组编排（自由组合角色）：
#    #   enabled: true                 #   开启小组模式（强制开启按任务 Worktree 沙盒隔离）
#    #   execution_mode: parallel_worktree # 并发策略：parallel_worktree（安全并发）/ sequential（串行）
#    #   chain_within_cycle: true      #   同周期内依赖解锁后是否链式推进下游任务（默认开）
#    #   max_qa_rounds: 3              #   QA 发现 Bug 后的最大回归测试轮数（熔断防死循环）
#    #   fallback_role: backend        #   无 PM 时的未指派任务兜底认领角色
#    #   members:                      #   小组成员列表：
#    #     - id: pm
#    #       profile: pm_default       #   引用全局预设
#    #     - id: backend
#    #       profile: backend_default
#    #       llm:                       #   项目内可单独覆盖特定成员的模型
#    #         model: deepseek-reasoner
#    #     - id: frontend
#    #       profile: frontend_default
#    #     - id: qa
#    #       profile: qa_default

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
    root = Path(args.dir).resolve() if args.dir else default_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    print(f"初始化 orcsy 目录：{root}")

    config_path = root / CONFIG_NAME
    if getattr(args, "no_config", False):
        # 安装器「初始化示例任务」走这里：只落模板；config 由应用首次启动自动生成
        print("  跳过 config.yaml（应用首次启动时会在默认数据目录自动生成）")
    elif config_path.exists():
        print(f"  跳过（已存在）：{config_path}")
    else:
        config_path.write_text(CONFIG_EXAMPLE, encoding="utf-8")
        print(f"  已生成：{config_path}")

    # 任务/指引模板落到数据目录作参考样本（不覆盖已有），往项目 tasks/ 里复制即可用
    _write_templates(root)

    print("\n接着把 config.yaml 里 llm.api_key 改成你的真实密钥，")
    print("并把 projects 指向你的项目。每个受管项目内需要：")
    print(f"  - <项目>/AGENT.md      开发指引（示例：{root / 'AGENT.md'}）")
    print(f"  - <项目>/tasks/*.md    任务清单（示例：{root / 'tasks'}）")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from .config import load_config
    from .task_model import scan_tasks

    config_path = find_config(args.config)
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
    from .logging_setup import anchored_log_path, rotate_and_cleanup, setup_logging
    from .scheduler import Scheduler

    config_path = find_config(args.config)
    try:
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as e:
        _config_fatal(e)
        return 1
    if config.scheduler.run_once or args.once:
        config.scheduler.run_once = True

    log_file = anchored_log_path(config)
    rotate_msg = rotate_and_cleanup(log_file, config.logging.retention_days,
                                config.logging.max_total_mb)
    if rotate_msg:
        print(f"日志维护：{rotate_msg}", file=sys.stderr, flush=True)
    setup_logging(config.logging.level, str(log_file))
    scheduler = Scheduler(config)

    import atexit
    atexit.register(scheduler.stop)

    if config.scheduler.run_once:
        scheduler.run_cycle()
        return 0
    scheduler.run_forever()
    return 0


def default_data_dir() -> Path:
    """默认数据目录：config.yaml / skills/ / logs/ 的家。

    刻意避开「文档」目录——它常被 OneDrive 重定向，云端占位文件在 OneDrive
    未就绪时读不了（EINVAL，开机自启的托盘尤其容易撞上）。%APPDATA%（Roaming）
    是 Windows 应用数据的标准位置：本地实体、始终可写、不受 OneDrive 干扰。
    环境变量 ORCSY_DATA_DIR 可整体改址（测试 / 便携 / 自定义用法）。
    """
    override = os.environ.get("ORCSY_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "Orcsy"
        return Path.home() / "AppData" / "Roaming" / "Orcsy"
    return Path.home() / ".orcsy"


def _candidate_config_dirs() -> list[Path]:
    """配置可能出现的目录：默认数据目录 > 安装器旧约定（文档目录，兼容待迁移）等。"""
    dirs: list[Path] = [default_data_dir()]
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
    dirs.append(docs / "Orcsy")
    # 改名前的旧数据目录：0.9.0 起启动时自动迁移到默认数据目录，这里兜底兜旧
    dirs.append(docs / "TaskArranger")
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).parent)
    return dirs


def _readable_config(cand: Path) -> bool:
    """候选配置是否为「当场可读」的普通文件（探针读 1 字节暴露 EINVAL）。"""
    if not cand.is_file():
        return False
    try:
        with cand.open("rb") as f:
            f.read(1)
        return True
    except OSError:
        return False


def _migrate_legacy_config() -> None:
    """把旧「文档」目录的配置一次性迁移到默认数据目录（仅冻结版触发）。

    0.8.x 及更早版本把配置放在 文档\\Orcsy（更早为 文档\\TaskArranger），而
    「文档」常被 OneDrive 重定向，云端占位文件会让开机自启读配置失败。这里把
    旧配置**复制**（不删原件，留作备份）到 %APPDATA%\\Orcsy，此后候选顺序保证
    默认数据目录优先。已迁移过 / 无旧配置时为无操作。
    """
    target_cfg = default_data_dir() / "config.yaml"
    if _readable_config(target_cfg):
        return
    legacy = [d / "config.yaml" for d in _candidate_config_dirs()
              if d != default_data_dir()]
    src = next((c for c in legacy if _readable_config(c)), None)
    if src is None:
        return
    try:
        default_data_dir().mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target_cfg)
        # 技能库与配置同目录存放（0.8.0 起），一并迁移
        src_skills, dst_skills = src.parent / "skills", default_data_dir() / "skills"
        if src_skills.is_dir() and not dst_skills.exists():
            shutil.copytree(src_skills, dst_skills)
    except OSError as e:
        # 迁移失败不阻断启动：find_config 仍会按候选顺序用旧位置
        print(f"提示：旧配置迁移到 {target_cfg} 失败（{e}），暂继续使用 {src}")
        return
    print(f"已把配置迁移到 {target_cfg}\n（原文件保留未动：{src}）")


def _ensure_default_config() -> Path | None:
    """在默认数据目录生成一份模板 config.yaml（0.9.1 起配置不再依赖安装器初始化）。

    仅当**所有候选都不存在**时调用——已有候选只是暂时读不了（OneDrive 占位等）
    时不生成，否则新生成的空配置会遮蔽用户真实配置。生成失败返回 None。
    """
    target = default_data_dir() / "config.yaml"
    if target.exists():
        return None  # 存在但读不了：不覆盖，走失败提示
    try:
        default_data_dir().mkdir(parents=True, exist_ok=True)
        target.write_text(CONFIG_EXAMPLE, encoding="utf-8")
    except OSError as e:
        print(f"自动生成默认配置失败（{e}）。")
        return None
    print(f"已自动生成默认配置：{target}\n"
          f"首次使用请在看板「项目与设置」页填写 llm.api_key 等接入信息。")
    return target.resolve()


def find_config(explicit: str | None) -> Path:
    """配置查找顺序：显式路径 > [开发模式 ./config.yaml > ./run/config.yaml]
    > %APPDATA%\\Orcsy > 文档\\Orcsy > 文档\\TaskArranger（旧） > exe 目录（frozen）。

    只认普通文件且要求当场可读：配置若躺在 OneDrive「文档」目录，开机自启的
    托盘常抢在 OneDrive 就绪之前，云端占位文件 exists() 为真但读取报 EINVAL——
    此时记下原因换下一个候选，而不是带着裸 OSError 崩掉。
    安装版（frozen）不按 CWD 找配置：托盘/快捷方式启动时 CWD 不可控（System32、
    安装目录等），误绑到无关 config.yaml 反而是坑；开发模式保留 CWD 候选。
    所有候选都不存在时，在默认数据目录自动生成模板配置（零配置开箱即用）。
    """
    if getattr(sys, "frozen", False) and not explicit:
        _migrate_legacy_config()  # 一次性把旧「文档」配置迁到 %APPDATA%（已迁移则无操作）
    if explicit:
        p = Path(explicit).resolve()
        if not p.is_file():
            print(f"配置文件不存在或不可读：{p}")
            raise SystemExit(1)
        return p
    candidates: list[Path] = []
    if not getattr(sys, "frozen", False):
        candidates += [Path("config.yaml"), Path("run/config.yaml")]
    candidates += [d / "config.yaml" for d in _candidate_config_dirs()]
    failures: list[str] = []
    any_file = False
    for cand in candidates:
        if not cand.is_file():
            continue
        any_file = True
        try:
            with cand.open("rb") as f:
                f.read(1)  # 探针读一字节：暴露 EINVAL / 触发 OneDrive 水合
        except OSError as e:
            failures.append(f"{cand}：{e}")
            continue
        return cand.resolve()
    if not any_file:
        created = _ensure_default_config()
        if created is not None:
            return created
    searched = "、".join(str(c) for c in candidates)
    print(f"未找到可读取的 config.yaml（已尝试：{searched}）。")
    for f in failures:
        print(f"  读取失败 —— {f}")
    if failures:
        print("若配置位于 OneDrive「文档」目录：请确认 OneDrive 正在运行且文件已下载到本地。")
    print(f"可运行 orcsy init 在 {default_data_dir()} 生成，或用 --config 指定路径；")
    print("推荐直接在看板「项目与设置」页可视化管理。")
    raise SystemExit(1)


def _config_fatal(err: Exception) -> None:
    """配置加载失败的统一出口：控制台打印；冻结环境（托盘无控制台）弹消息框。"""
    msg = f"Orcsy 无法加载配置：\n\n{err}"
    print(msg, file=sys.stderr)
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, msg, "Orcsy 启动失败", 0x10)  # MB_ICONERROR
        except OSError:
            pass


def cmd_web(args: argparse.Namespace) -> int:
    import socket
    import threading
    import webbrowser

    from .config import load_config, validate_web_security
    from .logging_setup import anchored_log_path, rotate_and_cleanup, setup_logging
    from .scheduler import Scheduler
    from .web import AppState, _load_page, make_server

    # 安装器辅助：只写自启注册表后退出（不启服务）
    if getattr(args, "tray_autostart_on", False):
        from .autostart import enable

        enable()
        return 0

    config_path = find_config(args.config)
    # Web 模式允许暂无 api_key：先把看板跑起来，模型参数可后配
    try:
        config = load_config(config_path, require_api_key=False)
    except (FileNotFoundError, ValueError) as e:
        _config_fatal(e)
        return 1

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
            print(f"端口 {port} 上已有 Orcsy 看板在运行：http://{host}:{port}")
            print("如需另起一个实例，用 --port 指定其它端口。")
            return 0
        if args.port:
            print(f"端口 {port} 被其它程序占用，无法使用。")
            print(f"请换一个端口（如 orcsy web --port {port + 1}），"
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

    # CLI --host 覆盖绕过了配置文件加载期的 bind 守卫，必须单独再验一次（M1）
    try:
        validate_web_security(host, config.web.auth_password, context="命令行")
    except ValueError as e:
        print(str(e))
        return 1
    config.web.host, config.web.port = host, port
    # 相对日志路径锚定到配置文件所在目录：工作目录可能在 Program Files（不可写）
    log_file = anchored_log_path(config)
    # 启动即做一次日志归档/清理（此刻文件尚未打开，改名无阻）
    rotate_msg = rotate_and_cleanup(log_file, config.logging.retention_days,
                                config.logging.max_total_mb)
    if rotate_msg:
        print(f"日志维护：{rotate_msg}", file=sys.stderr, flush=True)
    setup_logging(config.logging.level, str(log_file))

    # 启动清理：清理系统临时目录遗留的历史调试浏览器 profile 目录
    try:
        import glob
        for legacy_profile in glob.glob(str(Path(tempfile.gettempdir()) / "orcsy-browser-profile-*")):
            shutil.rmtree(legacy_profile, ignore_errors=True)
    except Exception:
        pass

    scheduler = Scheduler(config)
    state = AppState(config, scheduler, log_path=str(log_file))
    _load_page()  # 启动即缓存看板页面（冻结环境下 _MEI 文件可能稍后不可读）
    server = make_server(state, host, port)
    url = f"http://{host}:{port}"

    log = __import__("logging").getLogger("orcsy.web")
    log.info("看板服务启动：%s（配置：%s）", url, config_path)
    # 提示信息同时打到 stderr，避免被 logging 配置吞掉或缓冲
    print(f"Orcsy 看板已启动：{url} （Ctrl+C 或右键托盘图标退出）", file=sys.stderr, flush=True)
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
    # 定时拉取 Issues：独立线程，与调度周期解耦（间隔可短于调度周期）
    scheduler.run_issues_forever_background()

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
                __import__("logging").getLogger("orcsy.web").exception(
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


def cmd_reset_password(args: argparse.Namespace) -> int:
    """重置看板访问密码为管理员输入的密码（PBKDF2-SHA256 密文写入 config.yaml）。"""
    import getpass
    from .auth import update_config_auth_password
    from .config import load_config, validate_web_security

    try:
        cfg_path = find_config(args.config)
    except FileNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1

    # 1. 判定新密码
    if args.clear:
        new_pwd = ""
    elif args.password is not None:
        new_pwd = str(args.password)
    elif args.flag_password is not None:
        new_pwd = str(args.flag_password)
    else:
        # 未传参数时：交互式隐藏输入
        if sys.stdin.isatty():
            print(f"目标配置文件: {cfg_path.resolve()}")
            try:
                p1 = getpass.getpass("请输入新的看板访问密码: ")
                if not p1:
                    print("提示: 输入为空。如需关闭密码鉴权，请执行 orcsy reset-password --clear；本次未做任何修改。")
                    return 0
                p2 = getpass.getpass("请再次输入以确认新密码: ")
            except (KeyboardInterrupt, EOFError):
                print("\n操作已取消。")
                return 1
            if p1 != p2:
                print("错误: 两次输入的密码不一致，未做修改。", file=sys.stderr)
                return 1
            new_pwd = p1
        else:
            line = sys.stdin.readline().rstrip("\r\n")
            new_pwd = line

    # 2. 安全合规检查：若清空密码，检查当前 host 是否为非回环地址（非回环地址严禁免密）
    try:
        cur_cfg = load_config(cfg_path, require_api_key=False)
        validate_web_security(cur_cfg.web.host, new_pwd, context="重置密码安全检查")
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1

    # 3. 密文写入 config.yaml
    try:
        update_config_auth_password(cfg_path, new_pwd)
        load_config(cfg_path, require_api_key=False)
    except Exception as e:
        print(f"错误: 写入配置文件失败: {e}", file=sys.stderr)
        return 1

    if new_pwd:
        print(f"[OK] 看板访问密码已成功重置为安全密文并保存至：\n     {cfg_path.resolve()}")
        print("     存储方式: PBKDF2-HMAC-SHA256 (100,000 次加盐哈希，明文不入库)")
        print("     请使用新设置的密码登录看板。")
    else:
        print(f"[OK] 已成功清除看板访问密码（配置文件：{cfg_path.resolve()}）。")
        print("     看板已切换回本机免密访问模式。")
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    # 托盘 exe 允许省略子命令：`orcsy-tray.exe --tray --config ...`
    # 等价于 `web --tray ...`（自启动注册表与快捷方式都这样写；--tray 现为兼容参数）
    if argv and "--tray" in argv and argv[0] not in ("init", "validate", "run", "web"):
        argv = ["web", *argv]
    parser = argparse.ArgumentParser(
        prog="orcsy",
        description="Orcsy — Agent 任务编排系统：定时扫描各项目任务列表，由大模型 Agent 按优先级完成功能开发与 bug 修复。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="初始化运行目录：生成 config.yaml 与项目模板")
    p_init.add_argument("--dir", default=None,
                        help="目标目录（默认 %APPDATA%\\Orcsy 数据目录）")
    p_init.add_argument("--no-config", action="store_true",
                        help="只生成任务/指引模板，不生成 config.yaml"
                             "（配置由应用首次启动自动创建；安装器「初始化示例任务」用）")
    p_init.set_defaults(func=cmd_init)

    p_val = sub.add_parser("validate", help="校验配置与各项目任务清单")
    p_val.add_argument("--config", default=None, help="配置文件路径（默认自动发现）")
    p_val.set_defaults(func=cmd_validate)

    p_run = sub.add_parser("run", help="启动调度：默认持续运行，--once 只执行一轮")
    p_run.add_argument("--config", default=None, help="配置文件路径（默认自动发现）")
    p_run.add_argument("--once", action="store_true", help="只执行一轮扫描后退出")
    p_run.set_defaults(func=cmd_run)

    p_web = sub.add_parser("web", help="启动 Web 可视化看板（含调度，默认自动打开浏览器）")
    p_web.add_argument("--config", default=None,
                       help="配置文件路径（默认自动发现：开发模式含 CWD，安装版为 %APPDATA%\\Orcsy）")
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

    p_pwd = sub.add_parser("reset-password", aliases=["passwd"],
                           help="重置看板访问密码为管理员输入的密码（密文存储至 config.yaml）")
    p_pwd.add_argument("password", nargs="?", default=None,
                       help="新密码（未提供时进入交互式隐藏输入）")
    p_pwd.add_argument("--password", dest="flag_password", default=None,
                       help="新密码（命令行参数形式，与直接传参等价）")
    p_pwd.add_argument("--clear", action="store_true",
                       help="清除访问密码（关闭登录鉴权，切回本地免密模式）")
    p_pwd.add_argument("--config", default=None, help="配置文件路径（默认自动发现）")
    p_pwd.set_defaults(func=cmd_reset_password)

    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
