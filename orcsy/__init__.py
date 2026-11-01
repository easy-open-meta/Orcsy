"""Orcsy — Agent 任务编排系统：定时扫描各项目任务列表，由大模型 Agent 按优先级执行。"""

__version__ = "0.17.0"

# 对外 HTTP 请求的统一身份（httpx 默认 UA 是 python-httpx/x.y，无辨识度）
USER_AGENT = f"Orcsy/{__version__}"
