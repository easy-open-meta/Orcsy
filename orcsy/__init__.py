"""Orcsy — Agent 任务编排系统：定时扫描各项目任务列表，由大模型 Agent 按优先级执行。"""

from __future__ import annotations

__version__ = "0.23.2"

# 对外 HTTP 请求的统一身份（httpx 默认 UA 是 python-httpx/x.y，无辨识度）
USER_AGENT = f"Orcsy/{__version__}"

# 先初始化基础元数据以避免 issues/llm 等子模块在导入时发生循环部分初始化
from .agent import ProjectAgent  # noqa: E402
from .config import AppConfig, LLMConfig, ProjectConfig, TaskStatus  # noqa: E402
from .executor import TaskExecutor, TaskResult  # noqa: E402
from .masking import MaskingSession  # noqa: E402
from .task_model import Task  # noqa: E402

__all__ = [
    "AppConfig",
    "LLMConfig",
    "MaskingSession",
    "ProjectAgent",
    "ProjectConfig",
    "Task",
    "TaskExecutor",
    "TaskResult",
    "TaskStatus",
    "USER_AGENT",
    "__version__",
]
