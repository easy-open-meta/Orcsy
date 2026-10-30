"""系统运行的全部配置：模型、调度、日志与各受管项目。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

# 任务状态机：open → in_progress → done / blocked / cancelled
TaskStatus = Literal["open", "in_progress", "done", "blocked", "cancelled"]


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


@dataclass
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    web: WebConfig = field(default_factory=WebConfig)
    projects: list[ProjectConfig] = field(default_factory=list)
    source_path: Path | None = None   # 配置文件来源（web 界面提示用户编辑位置）


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

    llm_raw = raw.get("llm") or {}
    llm = LLMConfig(
        provider=llm_raw.get("provider", "openai"),
        api_key=llm_raw.get("api_key", ""),
        base_url=llm_raw.get("base_url", LLMConfig.base_url),
        model=llm_raw.get("model", LLMConfig.model),
        temperature=float(llm_raw.get("temperature", 0.2)),
        max_tokens=int(llm_raw.get("max_tokens", 8192)),
        max_context_tokens=int(llm_raw.get("max_context_tokens", 131072)),
        enable_thinking=bool(llm_raw.get("enable_thinking", False)),
        timeout_seconds=int(llm_raw.get("timeout_seconds", 300)),
    )
    if require_api_key and not llm.api_key:
        raise ValueError("config.yaml 中 llm.api_key 未配置。")

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
    )

    projects: list[ProjectConfig] = []
    for i, item in enumerate(raw.get("projects") or []):
        p_path = Path(str(item.get("path", ""))).expanduser()
        if not p_path.is_absolute():
            # 相对路径以配置文件所在目录为基准，避免受启动工作目录影响
            p_path = (path.parent / p_path).resolve()
        projects.append(
            ProjectConfig(
                name=str(item.get("name") or p_path.name or f"project-{i + 1}"),
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
            )
        )

    cfg = AppConfig(llm=llm, scheduler=scheduler, logging=logging_cfg, web=web,
                    projects=projects, source_path=path.resolve())
    return cfg
