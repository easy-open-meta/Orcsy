"""工具与命令白名单的静态规格、分类元数据与配置校验器。

本模块属于基础设施层，提供纯粹的元数据定义与数据校验函数，
不依赖 `orcsy.tools`、`orcsy.config` 或任何重型运行时，用于解耦配置校验与工具实现的循环依赖。
"""

from __future__ import annotations

# 命令白名单：测试、构建、lint 等 Agent 自验证所需的常见命令。
# 通配符按 argv[0] 的文件名匹配（小写、fnmatch）；不在表内的命令一律拒绝。
DEFAULT_ALLOWED_COMMANDS: list[str] = [
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
    # 包管理器（环境引导：缺工具链时静默安装；受访问权限审批约束）
    "winget*", "choco", "scoop", "brew", "apt*", "apt-get", "dnf", "nvm",
    # 系统 shell/下载/解压
    "powershell", "pwsh", "curl", "tar", "setx",
    # Windows 环境探测/文件查看（只读/信息性）
    "where", "cmd", "dir", "type", "echo", "set", "tasklist", "systeminfo",
    "findstr", "more", "ver", "hostname", "whoami",
    # POSIX 只读探测/统计
    "ls", "cat", "wc", "head", "tail", "grep", "sort", "uniq", "cut", "tr",
    "diff", "stat", "du", "df", "pwd", "printf", "date", "sleep", "uname",
    "which", "id", "basename", "dirname", "touch", "md5sum", "sha256sum",
    "printenv",
]

# 命令白名单生态分类（供设置页与 API 结构化呈现及批量管理）
COMMAND_CATEGORIES: list[dict] = [
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
TOOL_CATEGORIES: list[dict] = [
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

# 所有受管工具名称清单
ALL_TOOL_NAMES: tuple[str, ...] = tuple(
    tool
    for category in TOOL_CATEGORIES
    for tool in category["tools"]
)

# 可开关的工具（finish 是任务循环的协议工具，不可禁用，不在此列）
TOGGLEABLE_TOOLS: tuple[str, ...] = tuple(n for n in ALL_TOOL_NAMES if n != "finish")


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
    valid_names = set(ALL_TOOL_NAMES) | set(TOOL_GROUPS)
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


__all__ = [
    "ALL_TOOL_NAMES",
    "COMMAND_CATEGORIES",
    "DEFAULT_ALLOWED_COMMANDS",
    "DEFAULT_OFF_TOOLS",
    "TOGGLEABLE_TOOLS",
    "TOOL_CATEGORIES",
    "TOOL_GROUPS",
    "validate_commands_map",
    "validate_tools_map",
]
