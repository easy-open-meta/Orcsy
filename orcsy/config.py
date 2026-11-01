"""系统运行的全部配置：模型、调度、日志与各受管项目。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from .approvals import PERMISSION_MODES
from .issues import IssueError, normalize_repo
from .tools import validate_tools_map

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


@dataclass
class WebConfig:
    """Web 可视化看板配置。"""

    host: str = "127.0.0.1"
    port: int = 5116
    open_browser: bool = True     # 启动时自动打开浏览器
    # 看板登录密码：非空时所有页面/API 需先登录（外网部署务必设置）；
    # 留空 = 关闭鉴权，本机使用零打扰
    auth_password: str = ""


@dataclass
class UpdateConfig:
    """程序自更新配置：从 Gitee Release 检查并下载新版本安装包。"""

    repo: str = ""                # 更新源仓库 URL，如 https://gitee.com/xxx/orcsy
    token: str = ""               # 私有仓库的私人令牌；公开仓库可留空
    auto_check: bool = True       # 启动后自动检查一次更新


@dataclass
class ToolsConfig:
    """全局 Agent 工具开关：{工具名: bool}，未列出的工具默认启用。

    finish 是任务循环的协议工具，不在可配置范围内（validate_tools_map 拒绝）。
    项目级 ProjectConfig.tools 按工具名逐项覆盖这里的值。
    """

    enabled: dict[str, bool] = field(default_factory=dict)


@dataclass
class PermissionsConfig:
    """全局访问权限：Agent 敏感操作的人工审批策略。

    mode 三档（详见 orcsy/approvals.py）：ask_on_change 变更前询问 /
    autonomous 自主模式（仅高风险询问）/ full_access 完全访问（默认，不询问）。
    timeout_minutes：审批挂起超时，到期自动拒绝（防无人值守时任务永久挂起）。
    """

    mode: str = "full_access"
    timeout_minutes: int = 30


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
    permissions: PermissionsConfig = field(default_factory=PermissionsConfig)
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
        cfg = LLMConfig(
            provider=provider,  # type: ignore[arg-type]
            api_key=str(data.get("api_key") or ""),
            base_url=base_url,
            model=str(data.get("model") or LLMConfig.model),
            temperature=float(data.get("temperature", 0.2)),
            max_tokens=int(data.get("max_tokens", 8192)),
            max_context_tokens=int(data.get("max_context_tokens", 131072)),
            enable_thinking=bool(data.get("enable_thinking", False)),
            timeout_seconds=int(data.get("timeout_seconds", 300)),
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
    )

    web_raw = raw.get("web") or {}
    web = WebConfig(
        host=str(web_raw.get("host", "127.0.0.1")),
        port=int(web_raw.get("port", 5116)),
        open_browser=bool(web_raw.get("open_browser", True)),
        auth_password=str(web_raw.get("auth_password") or ""),
    )

    upd_raw = raw.get("update") or {}
    update = UpdateConfig(
        repo=str(upd_raw.get("repo") or "").strip(),
        token=str(upd_raw.get("token") or ""),
        auto_check=bool(upd_raw.get("auto_check", True)),
    )

    # 全局工具开关：加载期即校验（未知工具名/禁用 finish 立刻暴露，风格同 llm）
    try:
        tools = ToolsConfig(enabled=validate_tools_map(raw.get("tools"), context="tools"))
    except ValueError as e:
        raise ValueError(str(e)) from e

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
            )
        )

    cfg = AppConfig(llm=llm, scheduler=scheduler, logging=logging_cfg, web=web,
                    update=update, tools=tools, permissions=permissions,
                    projects=projects, source_path=path.resolve())
    return cfg
