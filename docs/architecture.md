# Orcsy 系统架构与工程化设计规范

> 遵循 `python-project-structure` 技能规范编撰。  
> 明确 Orcsy 的模块分层、公共 API 导出边界、单向依赖流向以及测试回归工程规范。

---

## 1. 系统分层架构（Layered Architecture）

Orcsy 采用自顶向下的单向分层架构，各层职责明确，**严格禁止下层反向导入上层**：

```
┌─────────────────────────────────────────────────────────────┐
│ 1. 交互与接入层 (Presentation & Entry Layer)                 │
│    __main__.py (CLI), web.py (REST/看板), tray.py (系统托盘)  │
└──────────────────────────────┬──────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. 编排与协同层 (Orchestration & Collaboration Layer)         │
│    scheduler.py (定时调度), agent.py (项目管理),             │
│    executor.py (任务循环), review.py (审查门),               │
│    subagent.py (只读子代理并发)                             │
└──────────────────────────────┬──────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. 执行工具与沙箱层 (Tooling & Sandbox Layer)                 │
│    tools.py (工具实现), tools_debug.py (本地调试工具),         │
│    browser/ (CDP 浏览器自动化), git_ops.py (Worktree/Git 操作) │
└──────────────────────────────┬──────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 4. 领域模型与核心状态层 (Domain Model & Core Logic Layer)     │
│    task_model.py (任务解析/DAG), config.py (全量配置模型),   │
│    llm.py (大模型适配/调用), masking.py (数据脱敏闭环),       │
│    prompts.py (角色/系统提示词), skills.py (专业技能注入)     │
└──────────────────────────────┬──────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 5. 基础设施与纯规格层 (Foundation & Specifications Layer)     │
│    tool_specs.py (命令与工具元数据/校验),                      │
│    concurrency.py (AIMD 并发流控), auth.py (PBKDF2 密码加盐), │
│    hosts.py (网络安全与 SSRF 防护),                           │
│    logging_setup.py (滚动日志), task_control.py (停止信号)   │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. 核心模块职责映射（One Concept Per File）

| 模块文件 | 归属分层 | 核心单一职责 |
|---|---|---|
| `orcsy/__init__.py` | 包入口 | 定义包版本 `__version__`、`USER_AGENT` 并导出顶级公共 API 类 |
| `orcsy/tool_specs.py` | 基础设施 | 无状态命令白名单、生态分类、工具分组常量与纯校验函数 |
| `orcsy/tools.py` | 执行工具门面 | 工具外观门面（Facade）：聚合各领域工具、管理 `ToolContext` 与派发 `dispatch_tool` |
| `orcsy/tools_fs.py` | 执行工具层 | 文件沙箱读写、多点编辑、重命名移动、删除与 glob/正则代码搜索 |
| `orcsy/tools_cmd.py` | 执行工具层 | 终端命令白名单执行、Windows 隐藏控制台、进程树管理与输出编码自愈 |
| `orcsy/tools_task.py` | 执行工具层 | 任务拆单编排协议（create_tasks/DAG检测）、Bug 提单、PM指派与完成信号 |
| `orcsy/tools_net.py` | 执行工具层 | SSRF 严格防护网页抓取、HTML 文本抽取、技能正文读取与镜像测速 |
| `orcsy/worktree.py` | 隔离沙箱层 | Git Worktree 物理目录映射、独立分支检出、陈旧锁恢复与重置清理 |
| `orcsy/task_service.py` | Web 服务层 | 看板侧任务文件 CRUD（创建/状态/排序/批量删除/清空/读取），纯函数不感知 HTTP |
| `orcsy/git_service.py` | Web 服务层 | 人工把关提交（stage 分支复位）、diff 预览、改动丢弃、Git 初始化与新项目引导 |
| `orcsy/config.save_config` | 领域模型 | config.yaml 序列化保存器（与 load_config 对称），字段顺序固定、输出与历史 byte-identical |
| `orcsy/executor.py` | 编排层 | 单任务生命周期编排：调用 WorktreeManager、Agent 轮次循环、审查门对接与落地 |
| `orcsy/config.py` | 领域模型 | 数据类（AppConfig 等 21 个 dataclass）、YAML 加载反序列化与配置解析 |
| `orcsy/task_model.py` | 领域模型 | 任务 Markdown 文件解析、frontmatter 状态机与任务依赖 DAG 环检测 |
| `orcsy/review.py` | 编排层 | 独立模型合并前只读审查 Agent |
| `orcsy/subagent.py` | 编排层 | 并发只读子代理扇出（fan-out）探索引擎 |
| `orcsy/masking.py` | 核心逻辑 | 发往模型厂商前的内存级敏感数据正则脱敏与写盘还原闭环 |
| `orcsy/concurrency.py`| 基础设施 | 进程级大模型请求 AIMD（加性增/乘性减）自适应并发配额控制器 |
| `orcsy/hosts.py` | 基础设施 | 本地调试网络安全、主机白名单校验与 DNS 重绑定防范 |
| `orcsy/auth.py` | 基础设施 | PBKDF2-HMAC-SHA256 密码加盐哈希存储与安全比对 |
| `orcsy/task_control.py` | 基础设施 | 跨线程/进程级运行任务的强制停止信号注册表 |
| `orcsy/browser/` | 工具子包 | Chromium / CDP 直连协议驱动与无头浏览器会话管理 |

---

## 3. 依赖流向与防循环依赖治理

### 3.1 历史循环成环问题
在早期版本中，`config.py` 在模块顶部导入 `tools.py` 中的 `validate_commands_map` 与 `DEFAULT_ALLOWED_COMMANDS`；而 `task_model.py` 依赖 `config.py` 的 `TaskStatus`；`tools.py` 的任务拆分与指派操作又必须依赖 `task_model.py`，形成三元环形依赖：
```
tools.py ──(延迟 import)──> task_model.py ──> config.py ──> tools.py (环)
```
导致 `tools.py` 内部不得不在三个工具函数中使用局部延迟 `from .task_model import ...` 规避崩溃。

### 3.2 治理方案：规格下沉（Specification Extraction）
将无状态的静态白名单、工具分组常量及纯输入校验函数抽离到 `orcsy/tool_specs.py`。
- `config.py` 仅依赖基础层的 `tool_specs.py`；
- `tools.py` re-export `tool_specs` 的符号（保持 100% 向后兼容）；
- `tools.py` 可在模块顶部安全导入 `task_model.py`；
- 依赖链彻底变为干净的单向拓扑流向：
```
tools.py ──> task_model.py ──> config.py ──> tool_specs.py
```

---

## 4. 显式公共接口规范（`__all__`）

依据 Pattern 2 与 Pattern 5，所有模块必须显式声明 `__all__` 列表，严格区分对外公开契约与内部私有细节：

### 4.1 根包导出 (`orcsy/__init__.py`)
作为类库或被外部代码调用时，可直接从 `orcsy` 引入核心对象：
```python
from orcsy import (
    __version__,
    USER_AGENT,
    AppConfig,
    ProjectConfig,
    LLMConfig,
    TaskStatus,
    ProjectAgent,
    TaskExecutor,
    TaskResult,
    Task,
    MaskingSession,
)
```

### 4.2 子包导出 (`orcsy/browser/__init__.py`)
浏览器调试引擎直接暴露 CDP 核心对象：
```python
from orcsy.browser import (
    CdpClient,
    BrowserError,
    find_browser,
    launch_browser,
    BrowserInstance,
)
```

---

## 5. 测试体系与批量回归运行器

### 5.1 架构设计
- `tests/test_*.py`：单模块或子系统级别的单元与行为测试（49 个）。
- `tests/e2e_*.py`：多 Agent 协作、工作流与端到端集成测试（14 个）。
- 每个测试文件均自包含 `if __name__ == "__main__": main()`，支持直接独立运行。

### 5.2 统一回归工具 (`tests/run_all.py`)
为避免单个测试逐一执行的低效，工程化提供 `tests/run_all.py` 批量回归工具：
```bash
# 执行全部 63 个测试并统计结果与耗时
uv run python tests/run_all.py

# 仅执行快速单元测试（跳过较慢的 e2e 测试）
uv run python tests/run_all.py --unit-only

# 过滤运行特定关键字相关的测试用例
uv run python tests/run_all.py masking
```
