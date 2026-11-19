# Phase 1 团队记忆：架构黑板报、批次共享剪贴板与依赖成果跳转

> 落地：2026-11-17　|　分支：`feat/phase1-team-memory`
> 前置：V1 可信流水线 R1~R6 已合入 main
> 关联：`docs/roadmap.md` §2.1 / §2.2、`docs/design-task-claims.md`

## 1. 范围

Phase 1 做三件事，全部是「结构化产出物 + 精准透传」，不共享原始聊天历史：

1. **项目架构黑板报**：`ARCHITECTURE.md` 作为全局架构备忘录，任务启动时只读注入；
2. **批次共享剪贴板**：同一需求批次内跨角色传递临时端口、测试账号、动态凭证；
3. **依赖成果一键查看**：任务详情展示前置任务状态/归档/成果摘要，可点击跳转。

不在本期：任务向量 RAG（Phase 3）、DAG 可视化（Phase 2）、角色扩展（Phase 2）。

## 2. 项目架构黑板报（ARCHITECTURE.md）

### 2.1 文件约定

- 位置：项目根目录 `ARCHITECTURE.md`（不是任务目录；随仓库走，可被审查/提交）；
- 内容：核心分层、模块边界、关键技术选型、公共数据模型、关键路由与文件导引；
- **语言：正文统一用英文**（代码路径/接口名/标识符保留原文），在信息不丢失的前提下压缩 token；仅当项目明确要求中文时才用中文；
- 上限：注入前 3000 字符；`update_architecture` 单文件上限 16000 字符。

### 2.2 只读消费

任务执行器在启动 Agent 循环前读取**当前工作根**（worktree 或项目根）的
`ARCHITECTURE.md`，交给 `build_system_prompt`：

```
# 项目架构黑板报（ARCHITECTURE.md）
<architecture>…前 3000 字符…</architecture>
维护约定：PM 首次梳理；开发重大接口变更追加 ≤200 字符（英文）；只写长期架构事实。
```

- 文件不存在：整节省略，不占上下文；
- 文件过长：截断并提示完整文件位置；
- 所有角色（PM/开发/QA/单兵）只要启用 `update_architecture` 都可见。

### 2.3 维护工具 `update_architecture`

PM 默认只读、不能用 `write_file` 改业务代码；因此提供**只写一个固定文件**的专用工具：

| 参数 | 说明 |
| --- | --- |
| `content` | 黑板报内容（英文书写；replace ≤6000 字，append ≤2000 字，提示词约定开发增量 ≤200 字符） |
| `mode` | `append`（默认）/ `replace` |

- 只写 `ctx.root / "ARCHITECTURE.md"`，无法路径穿越或改其他文件；
- PM 在工作区根（非 worktree）用 `replace` 首次建立；开发在 worktree 内用 `append`，
  随代码分支合并；
- 文件总长超过 16000 字时拒绝追加，提示用 `replace` 精简。

### 2.4 角色协议

- **PM**：摸底后若文件不存在或与现状明显不符，先用 `update_architecture(mode=replace)` 建立/更新（content 用英文）；
- **开发**：重大接口/数据表/模块边界变更后 `append` ≤200 字符（英文）；普通小改不动；
- **QA**：只读消费，不要求维护。

## 3. 批次共享剪贴板（Session Bus）

### 3.1 批次定义

沿任务 `parent_id` 向上追到根任务：

- PM 拆出的子任务、QA 回归任务、QA `report_bug` 创建的 Bug 任务共享同一个根批次；
- 无 `parent_id` 的单兵任务以自身 ID 为批次；
- 父任务已归档时从 `tasks/done/` 继续追溯；父任务缺失时以父 ID 作为稳定批次键。

### 3.2 存储与安全

- 路径：`<配置目录>/state/session_bus/<project>__<batch>.json`；不写任务文件、不进 Git；
- 单批次最多 50 个键；key 1~64 位（字母/数字/`_`/`.`/`-`）；value ≤8000 字符；
- 单进程内文件锁 + 临时文件原子替换；POSIX 下目录 0700、文件 0600；
- 只放临时运行态，不放长期密钥；值会随 `session_read` 进入模型上下文，与普通工具结果同等对待。

### 3.3 工具

| 工具 | 行为 |
| --- | --- |
| `session_share(key, value)` | 写入/覆盖；返回键名与当前条目数，不回显 value |
| `session_read(key)` | 读取值；key 留空列出可用键（不返回值/凭证） |

工具未注入 `session_bus`（单测直构）时返回明确错误，不抛异常打断循环。

### 3.4 生命周期

- 任务 `done/cancelled` 回写或看板人工改状态后，扫描同批次全部任务（含 done/）；
- 若批次内已无 `open / in_progress / blocked` 任务 → 立即删除文件；
- `blocked` 保留（可能重新入队）；
- 进程被杀/漏清理：启动时 `prune_expired` 删除超过 7 天的文件；
- 单兵任务完成即删除自己的批次。

## 4. 依赖成果一键查看

- `GET /api/task` 新增 `dep_tasks`：
  - `id / title / status / assignee / archived / missing / summary / qa_round / parent_id`；
  - `summary` 取 done 任务的「## Agent 处理结果」节（≤600 字），未完成/删除任务为空；
- 任务详情弹窗在「前置依赖任务」下渲染：
  - 状态标签（已完成/执行中/受阻/已删除…）；
  - 点击任务 ID/标题 → `openTaskView` 打开该任务详情（归档任务只读）；
  - done 任务展示成果摘要两行，悬浮可看全文；
  - 父需求/来源任务同样可点击跳转。
- 归档任务不在 `/api/state` 最近缓存时，标题/优先级/标签/指派从 `/api/task` 全文补全。

## 5. 兼容性

- 旧任务无 `parent_id` / 无 `ARCHITECTURE.md`：行为与旧版一致（黑板报节为空、剪贴板按自身任务批次）；
- 新工具默认启用，可在设置页「批次共享状态 / 项目架构记忆」分类里关闭；
- 不改任务状态机、`depends_on` 语义与既有 API；只新增字段/工具/提示词节。

## 6. 验收

`tests/test_phase1_team_memory.py`：

1. 黑板报：有内容注入、无内容省略、超长截断、PM/开发/QA 角色约定、`update_architecture` replace/append；
2. 剪贴板：parent 链批次解析、跨角色读写、批次隔离、工具调用、无上下文错误、未完成不清理、全终态清理、TTL；
3. 依赖概览：done 摘要、open 状态、missing 条目。

另需回归：`test_tools_toggle`、`test_tool_map`、`test_team_web_api`、`test_task_dependencies`、`test_task_split`、`e2e_fake_llm`。
