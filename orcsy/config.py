"""系统运行的全部配置：模型、调度、日志与各受管项目。"""

from __future__ import annotations

import ipaddress
import re

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from .approvals import PERMISSION_MODES
from .issues import IssueError, normalize_repo
from .mirrors import validate_mirrors
from .tools import (DEFAULT_ALLOWED_COMMANDS, validate_commands_map,
                    validate_tools_map)

# 任务状态机：open → in_progress → done / blocked / cancelled
TaskStatus = Literal["open", "in_progress", "done", "blocked", "cancelled"]

# LLMConfig 的可覆盖字段（项目专属模型绑定按此白名单过滤）
_LLM_FIELDS = (
    "provider", "api_key", "base_url", "model", "temperature",
    "max_tokens", "max_context_tokens", "enable_thinking", "timeout_seconds",
)


@dataclass
class ProjectConfig:
    """一个受管项目：由一个专职 Agent 负责其功能开发与 bug 修复。"""

    name: str
    path: Path
    enabled: bool = True
    tasks_dir: str = "tasks"          # 项目内任务列表目录（相对项目根）
    guide_file: str = "AGENT.md"      # 项目内开发指引（相对项目根）
    max_tasks_per_cycle: int = 1      # 每轮调度最多完成的任务数
    allowed_branch: str | None = None # 仅在该分支上工作；None 表示当前分支
    max_agent_turns: int = 40         # 单个任务的工具循环轮数上限
    allowed_commands: list[str] | None = None  # Agent 可执行的命令白名单；None 用内置默认
    # 项目专属模型绑定：部分覆盖全局 llm 配置（只写需要换的字段，其余继承全局）。
    # 存原始字典而非 LLMConfig——全局配置改动后，未覆盖字段始终继承最新全局值。
    llm: dict[str, Any] | None = None
    # 远程仓库绑定（GitHub/Gitee/GitLab）：{url, token?, provider?, api_base?,
    # auto_pull_interval?}。绑定后可从仓库 Issues 导入任务队列（看板「拉取 Issues」）；
    # auto_pull_interval 为定时自动拉取间隔（秒），0/缺省=仅手动拉取。
    # token 可选（公开仓库免认证）。
    repo: dict[str, Any] | None = None
    # 项目级工具开关覆盖：{工具名: bool}。None = 跟随全局 tools 配置；
    # 字典 = 按工具名逐项覆盖全局（resolve_tools 合并，同 llm 绑定的部分覆盖语义）。
    # 存原始字典，全局开关改动后未覆盖的工具始终继承最新全局值。
    tools: dict[str, bool] | None = None
    # 项目级访问权限模式覆盖（ask_on_change / autonomous / full_access）；
    # None = 跟随全局 permissions.mode
    permission_mode: str | None = None
    # 项目级审查开关：None/缺省 = 跟随全局 review.enabled（任务归档前合并前审查，
    # 详见 ReviewConfig）
    review_enabled: bool | None = None
    # 项目级审查模型绑定：部分覆盖全局 review.llm（只写需要换的字段，其余继承；
    # 同任务模型 llm 绑定的部分覆盖语义）。存原始字典。
    review_llm: dict[str, Any] | None = None
    # 项目级返工次数覆盖：审查不通过的最大累计轮数；None = 跟随全局
    # review.max_review_rounds（两级均校验 1~10）
    review_max_rounds: int | None = None


@dataclass
class LLMConfig:
    """大模型接入配置，OpenAI 兼容协议（provider=anthropic 时走 Anthropic 协议）。"""

    provider: Literal["openai", "anthropic"] = "openai"
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    temperature: float = 0.2
    max_tokens: int = 8192
    # 模型上下文窗口（tokens）：看板选择模型时可自动填充；接近上限时
    # Agent 循环会裁剪早期工具结果，避免长任务撞 400
    max_context_tokens: int = 131072
    # 请求侧开启模型思考（Anthropic extended thinking / OpenAI 兼容服务的
    # enable_thinking 参数）；默认关闭——多数服务默认返回思考内容，无需开启
    enable_thinking: bool = False
    # 单次模型请求超时（秒）。-1（或任何 ≤0，解析时归一为 -1）= 不超时：
    # 无限等待响应，挂起时靠看板「强制停止」打断（HTTP 请求在子线程执行、
    # 主线程轮询停止信号，见 llm.py）。思考型模型/长任务建议 600~900 或 -1
    timeout_seconds: int = 300


@dataclass
class SchedulerConfig:
    interval_seconds: int = 300
    max_parallel_agents: int = 1
    run_once: bool = False
    # 与 config.yaml 中 projects 的顺序配合，保证扫描顺序稳定
    # （字段保留以便扩展，例如未来加入每项目独立间隔）


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "logs/orcsy.log"
    # 日志归档与清理的保留天数：活动日志写满 N 天归档进 logs/archive/，
    # 归档及大小滚动的旧日志超过 N 天删除；0 表示关闭自动归档与清理
    retention_days: int = 7
    # 日志体系总占用上限（MB）：活动日志 + 滚动备份 + 归档合计超过即从最旧
    # 的备份/归档删起（活动日志不可删，另有 5MB 滚动兜底）；0 表示不限制。
    # 与 retention_days 相互独立，任一命中即清
    max_total_mb: int = 200


@dataclass
class WebConfig:
    """Web 可视化看板配置。"""

    host: str = "127.0.0.1"
    port: int = 5116
    open_browser: bool = True     # 启动时自动打开浏览器
    # 看板登录密码：非空时所有页面/API 需先登录（外网部署务必设置）；
    # 留空 = 关闭鉴权，本机使用零打扰
    auth_password: str = ""


# 官方更新源：配置未写 update.repo 时作为默认值（零配置即可检查更新）
DEFAULT_UPDATE_REPO = "https://gitee.com/java-and-net/Orcsy"


@dataclass
class UpdateConfig:
    """程序自更新配置：从 Gitee Release 检查并下载新版本安装包。"""

    # 默认官方 Gitee 源：装好即可用「检查更新」，免用户手动填写
    repo: str = DEFAULT_UPDATE_REPO
    token: str = ""               # 私有仓库的私人令牌；公开仓库可留空
    auto_check: bool = True       # 启动后自动检查一次更新


@dataclass
class NotifyConfig:
    """任务结束提醒配置：完成/受阻时提示音 + 系统通知（cancelled 不提醒）。"""

    sound: bool = True            # 提示音（程序内置：完成「叮咚」/ 受阻警示音；音频缺失回退系统音）
    system: bool = True           # 系统通知（托盘气泡；--no-tray 启动时无渠道自动跳过）


@dataclass
class ToolsConfig:
    """全局 Agent 工具开关：{工具名: bool}，未列出的工具默认启用。

    finish 是任务循环的协议工具，不在可配置范围内（validate_tools_map 拒绝）。
    项目级 ProjectConfig.tools 按工具名逐项覆盖这里的值。
    """

    enabled: dict[str, bool] = field(default_factory=dict)


@dataclass
class CommandsConfig:
    """全局命令白名单开关：{命令: bool}，未列出的默认启用。

    值域是 DEFAULT_ALLOWED_COMMANDS（默认白名单）的成员；置 false 的命令从
    默认白名单移除（run_command 直接拒绝）。项目级 allowed_commands 语义不变：
    显式列表整表替换、None 跟随全局（resolve_commands 解析）。
    """

    enabled: dict[str, bool] = field(default_factory=dict)


@dataclass
class EnvConfig:
    """注入 Agent 子进程（run_command）的自定义环境变量。

    典型用途：网络代理与国内镜像——HTTP_PROXY/HTTPS_PROXY、GOPROXY=
    https://goproxy.cn,direct（Go 模块）、npm config registry（Node）、
    PIP_INDEX_URL（Python）。键名校验为合法环境变量名；OS 常规安全变量
    （PATH/SYSTEMROOT 等由程序维护，不在此覆盖）。
    """

    vars: dict[str, str] = field(default_factory=dict)
    # 任务启动时把扫描到的缺失工具目录追加写入用户 PATH（HKCU，只增不删、
    # 写前备份）。默认关：这是系统级持久修改，由用户在看板显式开启
    path_autofix: bool = False


def validate_env_vars(data: object, context: str = "env") -> dict[str, str]:
    """校验 env.vars：键必须像环境变量名（大写字母/数字/下划线开头），值转字符串。"""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{context} 必须是「变量名: 值」的键值对")
    out: dict[str, str] = {}
    for key, value in data.items():
        name = str(key).strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"{context} 含非法变量名：{name}")
        if name.upper() in ("PATH", "SYSTEMROOT", "COMSPEC", "WINDIR"):
            raise ValueError(f"{context} 不允许覆盖 {name}（由程序维护）")
        out[name] = str(value)
    return out


@dataclass
class PermissionsConfig:
    """全局访问权限：Agent 敏感操作的人工审批策略。

    mode 三档（详见 orcsy/approvals.py）：ask_on_change 变更前询问 /
    autonomous 自主模式（仅高风险询问）/ full_access 完全访问（默认，不询问）。
    timeout_minutes：审批挂起超时，到期自动拒绝（防无人值守时任务永久挂起）。
    """

    mode: str = "full_access"
    timeout_minutes: int = 30


@dataclass
class MirrorsConfig:
    """镜像源管理：用户在内置源之外追加的自定义源 + 禁用源清单。

    custom：{类别: [URL]}，类别见 orcsy/mirrors.py 的 CATEGORY_KEYS（go_dist /
    goproxy / npm / pip / node_dist / jdk / python_dist）；disabled：被关掉的源
    URL（内置或自定义皆可）。测速结果不落这里——存边车 mirror_speeds.json
    （高频变动、看板与 Agent 共享，不污染用户手编的 config.yaml）。
    """

    custom: dict[str, list[str]] = field(default_factory=dict)
    disabled: list[str] = field(default_factory=list)


# ReviewConfig.llm 与项目 review_llm 的可覆盖字段（同任务模型 _LLM_FIELDS 白名单）
_REVIEW_LLM_FIELDS = _LLM_FIELDS


@dataclass
class ReviewConfig:
    """任务审查（Review Agent）全局配置：合并前审查门，详见 docs/design-review-agent.md。

    llm 默认 None = 未配置审查模型 = 审查未启用（默认态）：所有项目照常执行与
    归档，与旧版行为一致。用户显式配置审查模型后，审查门即对全部项目默认生效
    （项目级 review_enabled 可关）；审查模型必须与各项目的生效任务模型不同
    （同名模型即冲突，加载期报错 + 运行时守卫双重拦截，不让模型自审自批）。
    """

    enabled: bool = True           # 全局总开关（false = 全局暂停，保留模型配置）
    llm: LLMConfig | None = None   # 审查模型；None = 审查未启用（默认态）
    max_turns: int = 10            # 审查工具循环轮数上限（含跑测试的轮次）
    # 返工次数全局默认：累计审查不通过达到上限即转 blocked 等人工
    # （项目级 review_max_rounds 可覆盖，resolve_review_rounds 解析）
    max_review_rounds: int = 2
    # 审查 agent 可用 run_command 跑测试/构建验证（受项目命令白名单与访问
    # 权限审批门约束）；false = 纯只读审查
    allow_test_commands: bool = True
    # 返工任务（review_round>0）通过审查后 merge --squash 收敛 WIP 提交为
    # 单提交；false = 一律 --no-ff
    squash_merge: bool = True


def validate_permission_mode(value: object, context: str = "permissions.mode") -> str:
    mode = str(value or "").strip()
    if mode not in PERMISSION_MODES:
        raise ValueError(f"{context} 只能是 {' / '.join(PERMISSION_MODES)}（当前：{mode or '空'}）")
    return mode


@dataclass
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    web: WebConfig = field(default_factory=WebConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    commands: CommandsConfig = field(default_factory=CommandsConfig)
    env: EnvConfig = field(default_factory=EnvConfig)
    permissions: PermissionsConfig = field(default_factory=PermissionsConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    mirrors: MirrorsConfig = field(default_factory=MirrorsConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    projects: list[ProjectConfig] = field(default_factory=list)
    source_path: Path | None = None   # 配置文件来源（web 界面提示用户编辑位置）


def _coerce_llm(data: dict[str, Any], *, require_api_key: bool = False,
                context: str = "llm") -> LLMConfig:
    """把 YAML/JSON 字典映射为 LLMConfig：类型收窄、provider/base_url 值校验、未知键警告。

    全局 llm 与项目专属 llm 绑定共用此入口，保证两路行为一致。
    """
    unknown = sorted(k for k in data if k not in _LLM_FIELDS)
    if unknown:
        print(f"警告：{context} 中存在未知字段（已忽略）：{', '.join(unknown)}")
    provider = str(data.get("provider") or "openai")
    if provider not in ("openai", "anthropic"):
        raise ValueError(
            f"{context}.provider 只能是 openai 或 anthropic（当前：{provider}）。")
    base_url = str(data.get("base_url") or LLMConfig.base_url)
    if not base_url.startswith(("http://", "https://")):
        raise ValueError(
            f"{context}.base_url 必须以 http:// 或 https:// 开头（当前：{base_url}）。")
    try:
        # 超时 ≤0（如 -1）归一为 -1 = 不超时（无限等待，靠看板强制停止兜底）；
        # 键缺失/空值回退默认 300
        raw_timeout = data.get("timeout_seconds")
        timeout = 300 if raw_timeout is None else int(raw_timeout)
        if timeout <= 0:
            timeout = -1
        cfg = LLMConfig(
            provider=provider,  # type: ignore[arg-type]
            api_key=str(data.get("api_key") or ""),
            base_url=base_url,
            model=str(data.get("model") or LLMConfig.model),
            temperature=float(data.get("temperature", 0.2)),
            max_tokens=int(data.get("max_tokens", 8192)),
            max_context_tokens=int(data.get("max_context_tokens", 131072)),
            enable_thinking=bool(data.get("enable_thinking", False)),
            timeout_seconds=timeout,
        )
    except (TypeError, ValueError) as e:
        raise ValueError(f"{context} 配置项类型有误：{e}") from e
    if require_api_key and not cfg.api_key:
        raise ValueError(f"{context}.api_key 未配置。")
    return cfg


def resolve_llm(global_llm: LLMConfig, override: dict[str, Any] | None,
                context: str = "llm") -> LLMConfig:
    """解析项目专属模型：override 未写的字段继承全局（部分覆盖语义）。

    override 为 None / 空字典 / 全空值时原样返回全局对象，未绑定项目行为
    与旧版完全一致。字段级继承规则：字符串空或缺失=继承；数值与布尔
    写了即覆盖（数值无「空」概念）。
    """
    if not override:
        return global_llm
    merged: dict[str, Any] = asdict(global_llm)
    applied = False
    for key in _LLM_FIELDS:
        value = override.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        merged[key] = value
        applied = True
    if not applied:
        return global_llm  # 全空绑定等价于未绑定：保持对象同一性
    return _coerce_llm(merged, require_api_key=False, context=context)


def normalize_llm_override(data: dict[str, Any]) -> dict[str, Any]:
    """清洗项目 llm 绑定字典：仅保留已知字段、丢弃空值；返回空字典=无有效绑定。

    供 Web 保存路径使用——未知键直接丢弃（_coerce_llm 只在读取/解析时警告，
    存盘就不该再保留脏数据反复触发警告）。
    """
    return {
        k: data[k] for k in _LLM_FIELDS
        if k in data and data[k] is not None
        and not (isinstance(data[k], str) and not data[k].strip())
    }


def resolve_review_llm(global_review: ReviewConfig,
                       override: dict[str, Any] | None,
                       context: str = "review.llm") -> LLMConfig | None:
    """解析生效审查模型：override 未写的字段继承全局 review.llm（部分覆盖语义）。

    两级都未配置 → None（=审查未启用）；override 全空值也等价于未配置。
    override 含任何有效字段但全局未配置模型时，以 override 重建（api_key
    允许为空，校验交给 resolve 之后的互斥检查与实际调用）。
    """
    if not override:
        return global_review.llm
    merged: dict[str, Any] = asdict(global_review.llm) if global_review.llm else {
        k: LLMConfig.__dataclass_fields__[k].default for k in _LLM_FIELDS
    }
    applied = False
    for key in _REVIEW_LLM_FIELDS:
        value = override.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        merged[key] = value
        applied = True
    if not applied:
        return global_review.llm
    return _coerce_llm(merged, require_api_key=False, context=context)


def normalize_review_llm(data: dict[str, Any]) -> dict[str, Any]:
    """清洗项目 review_llm 绑定字典（同 normalize_llm_override 语义）。"""
    return normalize_llm_override(data)


def resolve_review_rounds(review_cfg: ReviewConfig, project: ProjectConfig) -> int:
    """生效返工上限：项目 review_max_rounds 覆盖优先，None = 跟随全局默认。

    两级值都在加载期校验过范围（1~10），此处不重复校验。
    """
    if project.review_max_rounds is not None:
        return project.review_max_rounds
    return review_cfg.max_review_rounds


def review_model_normalized(model: str) -> str:
    """模型名规范化（互斥判重用）：strip + casefold——同名模型经不同
    base_url 部署仍是同一模型，不得自审自批。"""
    return (model or "").strip().casefold()


def assert_review_model_distinct(task_llm: LLMConfig | None,
                                 review_llm: LLMConfig | None,
                                 context: str = "review") -> None:
    """校验生效审查模型与生效任务模型不是同一个（需求硬约束 R4）。

    任一未配置直接通过（未配置审查模型 = 审查未启用，无冲突可言）。
    """
    if task_llm is None or review_llm is None:
        return
    if review_model_normalized(task_llm.model) == review_model_normalized(review_llm.model):
        raise ValueError(
            f"{context}: 审查模型不能与任务模型相同（{review_llm.model}）。"
            f"请为审查 agent 配置一个不同的模型。")


def review_enabled_for(review_cfg: ReviewConfig, project: ProjectConfig) -> bool:
    """生效审查开关：项目 review_enabled 覆盖优先，None = 跟随全局。

    注意：开关开着但未配置审查模型时仍不审查（审查未启用态），
    调用方须同时检查 resolve_review_llm 的结果。
    """
    if project.review_enabled is not None:
        return project.review_enabled
    return review_cfg.enabled


def resolve_tools(global_tools: ToolsConfig,
                  override: dict[str, bool] | None) -> dict[str, bool]:
    """解析项目生效的工具开关：项目覆盖逐项合并到全局之上（部分覆盖语义）。

    override 为 None/空字典时原样返回全局 map（保持同一性，未绑定项目行为
    与旧版完全一致）；否则返回 {**全局, **项目} 的新字典。
    """
    if not override:
        return global_tools.enabled
    return {**global_tools.enabled, **override}


def resolve_permission_mode(global_perms: PermissionsConfig,
                            project: ProjectConfig) -> str:
    """项目生效的访问权限模式：项目覆盖优先，None = 跟随全局。"""
    return project.permission_mode or global_perms.mode


def resolve_commands(global_cfg: CommandsConfig,
                     project: ProjectConfig) -> list[str]:
    """解析项目生效的命令白名单。

    项目 allowed_commands 显式列表 = 整表替换（历史语义不变）；None = 默认
    白名单剔除全局禁用项后的列表。
    """
    if project.allowed_commands is not None:
        return list(project.allowed_commands)
    disabled = {k for k, v in global_cfg.enabled.items() if not v}
    if not disabled:
        return list(DEFAULT_ALLOWED_COMMANDS)
    return [c for c in DEFAULT_ALLOWED_COMMANDS if c not in disabled]


def validate_web_security(host: str, auth_password: str,
                          context: str = "web") -> None:
    """非回环绑定必须配置登录密码（安全审计 2026-11-04 M1）。

    看板含全盘文件浏览、任务写档与 Agent 启停控制，把无鉴权的服务暴露到
    局域网等同把机器交给同网段任何人——host 非回环时拒绝（而非仅告警，
    托盘应用里告警几乎不会被看到）。三条配置入口（配置文件加载 / 看板保存 /
    CLI --host 覆盖）都须经过本守卫。
    """
    h = (host or "127.0.0.1").strip().lower()
    try:
        loopback = h == "localhost" or ipaddress.ip_address(h).is_loopback
    except ValueError:
        loopback = False  # 无法解析的主机名一律按非回环从严处理
    if not loopback and not (auth_password or "").strip():
        raise ValueError(
            f"{context}: web.host 绑定到非回环地址（{host}）时必须设置 web.auth_password "
            f"登录密码。请设置密码，或将 host 改回 127.0.0.1（仅本机访问）。")


def load_config(path: Path, require_api_key: bool = True) -> AppConfig:
    """解析 YAML 配置文件为 AppConfig，缺失字段回退到默认值。

    require_api_key=False 时允许空密钥（Web 模式先起界面，模型可后配）。
    """
    if not path.exists():
        raise FileNotFoundError(
            f"配置文件不存在：{path}\n"
            f"请先复制 config.example.yaml 为 config.yaml 并填写模型 API 信息。"
        )

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        # OneDrive「文档」目录的云端占位文件在 OneDrive 未运行/离线时
        # exists() 为真但读取报 EINVAL——给出可行动的提示而非裸 traceback
        raise FileNotFoundError(
            f"配置文件无法读取：{path}\n错误：{e}\n"
            f"若该文件位于 OneDrive「文档」目录，请确认 OneDrive 正在运行且文件已下载到本地。"
        ) from e

    raw: dict[str, Any] = yaml.safe_load(text) or {}

    llm = _coerce_llm(raw.get("llm") or {}, require_api_key=require_api_key)

    sched_raw = raw.get("scheduler") or {}
    scheduler = SchedulerConfig(
        interval_seconds=int(sched_raw.get("interval_seconds", 300)),
        max_parallel_agents=int(sched_raw.get("max_parallel_agents", 1)),
        run_once=bool(sched_raw.get("run_once", False)),
    )

    log_raw = raw.get("logging") or {}
    logging_cfg = LoggingConfig(
        level=str(log_raw.get("level", "INFO")).upper(),
        file=log_raw.get("file", "logs/orcsy.log"),
        retention_days=max(0, min(int(log_raw.get("retention_days", 7)), 365)),
        # 0 = 不限制；上限给个 sanity 夹取（100GB）
        max_total_mb=max(0, min(int(log_raw.get("max_total_mb", 200)), 100000)),
    )

    web_raw = raw.get("web") or {}
    web = WebConfig(
        host=str(web_raw.get("host", "127.0.0.1")),
        port=int(web_raw.get("port", 5116)),
        open_browser=bool(web_raw.get("open_browser", True)),
        auth_password=str(web_raw.get("auth_password") or ""),
    )
    # 非回环绑定 + 无鉴权的组合直接拒绝启动（M1）：错误经 _config_fatal 呈现，
    # 文案给出两条可行动出路（设密码 / 改回 127.0.0.1）
    validate_web_security(web.host, web.auth_password, context="配置文件")

    upd_raw = raw.get("update") or {}
    update = UpdateConfig(
        # 缺失/留空都回退官方源：老配置文件（没有 update 节）也享受零配置检查更新
        repo=str(upd_raw.get("repo") or DEFAULT_UPDATE_REPO).strip() or DEFAULT_UPDATE_REPO,
        token=str(upd_raw.get("token") or ""),
        auto_check=bool(upd_raw.get("auto_check", True)),
    )

    # 全局工具开关：加载期即校验（未知工具名/禁用 finish 立刻暴露，风格同 llm）
    try:
        tools = ToolsConfig(enabled=validate_tools_map(raw.get("tools"), context="tools"))
    except ValueError as e:
        raise ValueError(str(e)) from e

    # 全局命令白名单开关：键必须是默认白名单成员，加载期即校验
    commands = CommandsConfig(
        enabled=validate_commands_map(raw.get("commands"), context="commands"))

    # Agent 子进程自定义环境变量（代理/国内镜像等），加载期即校验。
    # YAML 形态两种都收：env: {vars: {KEY: value}}（看板保存结构）与
    # env: {KEY: value}（手写平铺）——平铺形态此前会被读成空、下次保存静默丢失
    env_raw = raw.get("env") or {}
    if not isinstance(env_raw, dict):
        raise ValueError("env 配置必须是字典（含 vars 键值对）。")
    env_map = env_raw.get("vars") if "vars" in env_raw else env_raw
    # path_autofix 只认规范形态（env.vars 嵌套）：平铺形态的键全部是环境变量，
    # 多出的 path_autofix 键按变量处理（宁可不生效也不误读）
    env = EnvConfig(
        vars=validate_env_vars(env_map, context="env.vars"),
        path_autofix=bool(env_raw.get("path_autofix", False)) if "vars" in env_raw else False,
    )

    # 镜像源管理（内置 + 用户添加源，HTTP 测速选最快），加载期即校验
    mirrors_clean = validate_mirrors(raw.get("mirrors"), context="mirrors")
    mirrors = MirrorsConfig(custom=mirrors_clean["custom"],
                            disabled=mirrors_clean["disabled"])

    # 任务审查（Review Agent）：llm 默认 None = 审查未启用（默认态，行为同旧版）。
    # 加载期即校验字段类型与范围；互斥校验在项目循环里逐项目做（依赖各自
    # 生效的任务模型）
    rev_raw = raw.get("review") or {}
    if not isinstance(rev_raw, dict):
        raise ValueError("review 配置必须是字典（键值对）。")
    rev_llm_raw = rev_raw.get("llm")
    if rev_llm_raw is not None and not isinstance(rev_llm_raw, dict):
        raise ValueError("review.llm 必须是键值对字典或 null。")
    rev_llm = _coerce_llm(rev_llm_raw, require_api_key=False, context="review.llm") \
        if rev_llm_raw else None
    try:
        rev_rounds = int(rev_raw.get("max_review_rounds", 2))
        rev_turns = int(rev_raw.get("max_turns", 10))
    except (TypeError, ValueError) as e:
        raise ValueError(f"review 数值字段有误：{e}") from e
    if not (1 <= rev_rounds <= 10):
        raise ValueError(f"review.max_review_rounds 应为 1~10（当前：{rev_rounds}）")
    if not (1 <= rev_turns <= 100):
        raise ValueError(f"review.max_turns 应为 1~100（当前：{rev_turns}）")
    review_cfg = ReviewConfig(
        enabled=bool(rev_raw.get("enabled", True)),
        llm=rev_llm,
        max_turns=rev_turns,
        max_review_rounds=rev_rounds,
        allow_test_commands=bool(rev_raw.get("allow_test_commands", True)),
        squash_merge=bool(rev_raw.get("squash_merge", True)),
    )

    # 全局访问权限：模式枚举 + 超时范围，加载期即校验
    perm_raw = raw.get("permissions") or {}
    if not isinstance(perm_raw, dict):
        raise ValueError("permissions 配置必须是字典（键值对）。")
    try:
        perm_timeout = int(perm_raw.get("timeout_minutes", 30))
    except (TypeError, ValueError) as e:
        raise ValueError(f"permissions.timeout_minutes 必须是整数：{e}") from e
    if not (1 <= perm_timeout <= 1440):
        raise ValueError(f"permissions.timeout_minutes 应为 1~1440 分钟（当前：{perm_timeout}）")
    permissions = PermissionsConfig(
        mode=validate_permission_mode(perm_raw.get("mode", "full_access")),
        timeout_minutes=perm_timeout,
    )

    # 任务结束提醒开关：布尔直取，缺失/非法一律回退默认（旁路功能不做强校验）
    not_raw = raw.get("notify") or {}
    if not isinstance(not_raw, dict):
        raise ValueError("notify 配置必须是字典（键值对）。")
    notify = NotifyConfig(
        sound=bool(not_raw.get("sound", True)),
        system=bool(not_raw.get("system", True)),
    )

    projects: list[ProjectConfig] = []
    for i, item in enumerate(raw.get("projects") or []):
        p_path = Path(str(item.get("path", ""))).expanduser()
        if not p_path.is_absolute():
            # 相对路径以配置文件所在目录为基准，避免受启动工作目录影响
            p_path = (path.parent / p_path).resolve()
        p_name = str(item.get("name") or p_path.name or f"project-{i + 1}")
        p_llm = item.get("llm")
        if p_llm is not None and not isinstance(p_llm, dict):
            raise ValueError(f"项目 {p_name} 的 llm 配置必须是字典（键值对）。")
        if p_llm:
            # 加载期即解析校验：typo / 非法值立刻暴露，而不是等首个任务执行时才炸
            resolve_llm(llm, p_llm, context=f"项目 {p_name} 的 llm")
        p_repo = item.get("repo")
        if p_repo is not None and not isinstance(p_repo, dict):
            raise ValueError(f"项目 {p_name} 的 repo 配置必须是字典（键值对）。")
        if p_repo:
            # repo 无继承语义，加载即归一化（补 provider、洗 token），存取一致
            try:
                p_repo = normalize_repo(p_repo, context=f"项目 {p_name} 的 repo")
            except IssueError as e:
                raise ValueError(str(e)) from e
        p_tools_raw = item.get("tools")
        if p_tools_raw is not None and not isinstance(p_tools_raw, dict):
            raise ValueError(f"项目 {p_name} 的 tools 配置必须是字典（工具名: true/false）。")
        # 加载期即校验（未知工具名/禁用 finish 立刻暴露，同 llm 绑定的 eager-validate）
        p_tools = validate_tools_map(p_tools_raw, context=f"项目 {p_name} 的 tools") or None
        # 项目级访问权限模式覆盖：缺省/空 = 跟随全局；非法值加载期即报错
        p_perm_raw = item.get("permission_mode")
        p_perm = (validate_permission_mode(p_perm_raw,
                                           context=f"项目 {p_name} 的 permission_mode")
                  if p_perm_raw else None)
        # 项目级审查配置：开关（None=跟随全局）、模型绑定（部分覆盖全局
        # review.llm）、返工次数（None=跟随全局 max_review_rounds）
        p_review_enabled_raw = item.get("review_enabled")
        if p_review_enabled_raw is not None and not isinstance(p_review_enabled_raw, bool):
            raise ValueError(f"项目 {p_name} 的 review_enabled 必须是布尔值或 null")
        p_review_llm_raw = item.get("review_llm")
        if p_review_llm_raw is not None and not isinstance(p_review_llm_raw, dict):
            raise ValueError(f"项目 {p_name} 的 review_llm 配置必须是字典（键值对）。")
        if p_review_llm_raw:
            # 加载期即解析校验（typo/非法值立刻暴露，同任务模型 llm 绑定）
            resolve_review_llm(review_cfg, p_review_llm_raw,
                               context=f"项目 {p_name} 的 review_llm")
        p_review_rounds_raw = item.get("review_max_rounds")
        p_review_rounds = None
        if p_review_rounds_raw is not None:
            try:
                p_review_rounds = int(p_review_rounds_raw)
            except (TypeError, ValueError) as e:
                raise ValueError(f"项目 {p_name} 的 review_max_rounds 必须是整数：{e}") from e
            if not (1 <= p_review_rounds <= 10):
                raise ValueError(
                    f"项目 {p_name} 的 review_max_rounds 应为 1~10（当前：{p_review_rounds}）")
        projects.append(
            ProjectConfig(
                name=p_name,
                path=p_path,
                enabled=bool(item.get("enabled", True)),
                tasks_dir=str(item.get("tasks_dir", "tasks")),
                guide_file=str(item.get("guide_file", "AGENT.md")),
                max_tasks_per_cycle=int(item.get("max_tasks_per_cycle", 1)),
                allowed_branch=item.get("allowed_branch"),
                max_agent_turns=int(item.get("max_agent_turns", 40)),
                allowed_commands=(
                    [str(c) for c in item["allowed_commands"]]
                    if isinstance(item.get("allowed_commands"), list) else None
                ),
                llm=p_llm or None,
                repo=p_repo or None,
                tools=p_tools,
                permission_mode=p_perm,
                review_enabled=p_review_enabled_raw,
                review_llm=p_review_llm_raw or None,
                review_max_rounds=p_review_rounds,
            )
        )
        # 项目级审查模型互斥校验（R4）：审查模型已配置且与该项目生效任务模型
        # 同名即报错拒启（未配置审查模型 = 审查未启用，无冲突可言）
        p_task_llm = resolve_llm(llm, p_llm or None, context=f"项目 {p_name} 的 llm")
        p_review_llm = resolve_review_llm(
            review_cfg, p_review_llm_raw or None, context=f"项目 {p_name} 的 review_llm")
        if p_review_llm is not None and review_enabled_for(review_cfg, projects[-1]):
            assert_review_model_distinct(
                p_task_llm, p_review_llm, context=f"项目 {p_name} 的 review")

    cfg = AppConfig(llm=llm, scheduler=scheduler, logging=logging_cfg, web=web,
                    update=update, tools=tools, commands=commands, env=env,
                    permissions=permissions, notify=notify, mirrors=mirrors,
                    review=review_cfg, projects=projects, source_path=path.resolve())
    return cfg
