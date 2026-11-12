"""工具集外观门面（Facade）：Agent 在受管项目内可用的原子操作统一入口。

本模块采用 Facade 设计模式，聚合文件操作（tools_fs）、命令执行与进程管理（tools_cmd）、
任务拆分与协作协议（tools_task）、外部网络与技能（tools_net）及本地调试工具（tools_debug），
并对外提供统一的 `ToolContext`、`TOOLS` 注册表与 `dispatch_tool` 调度器。
100% 保持原有导出与类型兼容。
"""

# ruff: noqa: F401

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .logging_setup import get_logger
from .tool_specs import (
    ALL_TOOL_NAMES,
    COMMAND_CATEGORIES,
    DEFAULT_ALLOWED_COMMANDS,
    DEFAULT_OFF_TOOLS,
    TOGGLEABLE_TOOLS,
    TOOL_CATEGORIES,
    TOOL_GROUPS,
    validate_commands_map,
    validate_tools_map,
)
from .tools_cmd import (
    _CHILD_PROCS,
    _CHILD_PROCS_LOCK,
    _NO_WINDOW,
    _TOOL_PATH_DIRS,
    CMD_TOOLS,
    MAX_OUTPUT_CHARS,
    REG_PATH_KEYS,
    TaskStopped,
    _augmented_env,
    _CommandTimeout,
    _decode_proc_output,
    _drain_after_kill,
    _kill_process_tree,
    _path_key,
    _registry_path_entries,
    _resolve_new_install,
    _tool_path_dirs,
    _wait_with_stop,
    _windows_startupinfo,
    check_command_allowed,
    kill_running_children,
    persist_path_dirs,
    register_child_proc,
    scan_tool_env,
    tool_run_command,
    unregister_child_proc,
)
from .tools_fs import (
    DEFAULT_READ_LINES,
    FS_TOOLS,
    MAX_FILE_READ_CHARS,
    _closest_snippet,
    _resolve_in_root,
    tool_delete_file,
    tool_edit_file,
    tool_find_files,
    tool_grep,
    tool_list_dir,
    tool_move_file,
    tool_multi_edit,
    tool_read_file,
    tool_write_file,
)
from .tools_net import (
    _MAX_REDIRECT_HOPS,
    _REDIRECT_STATUSES,
    MAX_SKILL_CHARS,
    MAX_WEB_FETCH_BYTES,
    MAX_WEB_FETCH_CHARS,
    NET_TOOLS,
    _assert_public_url,
    _fetch_with_guard,
    _html_to_text,
    _HTMLText,
    _WebFetchReject,
    tool_read_skill,
    tool_speedtest_mirrors,
    tool_web_fetch,
)
from .tools_task import (
    TASK_TOOLS,
    FinishSignal,
    tool_create_tasks,
    tool_delegate_explore,
    tool_finish,
    tool_reassign_task,
    tool_report_bug,
)

log = get_logger("tools")


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
    # 回给模型（操作不执行，零副作用）。由执行器按访问权限模式构建；
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


# ---- 聚合主注册表 ----

TOOLS: dict[str, tuple[str, dict, object]] = {}
TOOLS.update(FS_TOOLS)
TOOLS.update(CMD_TOOLS)
TOOLS.update(TASK_TOOLS)
TOOLS.update(NET_TOOLS)

from .tools_debug import DEBUG_TOOLS  # noqa: E402

TOOLS.update(DEBUG_TOOLS)


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
    # 人工审批门
    if ctx.approval_gate is not None:
        try:
            denial = ctx.approval_gate(name, args)
        except Exception as e:  # noqa: BLE001
            log.exception("审批门异常，按拒绝处理")
            denial = f"错误：审批系统异常（{e}），该操作未执行"
        if denial:
            return str(denial)
    try:
        return str(TOOLS[name][2](ctx, args))
    except FinishSignal:
        raise  # finish 由 Agent 循环捕获
    except Exception as e:  # noqa: BLE001
        return f"错误：{e}"


__all__ = [
    "ALL_TOOL_NAMES",
    "CMD_TOOLS",
    "COMMAND_CATEGORIES",
    "DEFAULT_ALLOWED_COMMANDS",
    "DEFAULT_OFF_TOOLS",
    "DEFAULT_READ_LINES",
    "FS_TOOLS",
    "FinishSignal",
    "MAX_FILE_READ_CHARS",
    "MAX_OUTPUT_CHARS",
    "MAX_SKILL_CHARS",
    "MAX_WEB_FETCH_BYTES",
    "MAX_WEB_FETCH_CHARS",
    "NET_TOOLS",
    "REG_PATH_KEYS",
    "TASK_TOOLS",
    "TOGGLEABLE_TOOLS",
    "TOOL_CATEGORIES",
    "TOOL_GROUPS",
    "TOOLS",
    "TaskStopped",
    "ToolContext",
    "_CHILD_PROCS",
    "_CommandTimeout",
    "_HTMLText",
    "_NO_WINDOW",
    "_TOOL_PATH_DIRS",
    "_WebFetchReject",
    "_assert_public_url",
    "_augmented_env",
    "_closest_snippet",
    "_decode_proc_output",
    "_drain_after_kill",
    "_fetch_with_guard",
    "_html_to_text",
    "_kill_process_tree",
    "_path_key",
    "_registry_path_entries",
    "_resolve_in_root",
    "_resolve_new_install",
    "_tool_path_dirs",
    "_wait_with_stop",
    "_windows_startupinfo",
    "check_command_allowed",
    "dispatch_tool",
    "kill_running_children",
    "persist_path_dirs",
    "register_child_proc",
    "scan_tool_env",
    "tool_definitions_anthropic",
    "tool_definitions_openai",
    "tool_delete_file",
    "tool_edit_file",
    "tool_find_files",
    "tool_grep",
    "tool_list_dir",
    "tool_move_file",
    "tool_multi_edit",
    "tool_read_file",
    "tool_run_command",
    "tool_write_file",
    "unregister_child_proc",
    "validate_commands_map",
    "validate_tools_map",
]
