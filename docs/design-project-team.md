# 多模型 Agent 项目小组（Project Team）需求评估与开发方案

> 状态：**v3 评审终审定案（全部核心决策已拍板，可按 M1→M4 启动实施）**
> 需求原文：**用户可以自由组合多个模型Agent组成项目小组进驻任意一个项目，负责该项目的所有需求任务拆分和代码开发工作，可以配置以下Agent：项目经理(负责对用户下发的任务，进行需求子任务拆分)、后端开发工程师(负责后端代码编写开发)、前端开发工程师(负责前端代码编写开发)、测试工程师(负责项目的前后端联调测试，并拥有新建bug任务到项目任务列表权限)。**
>
> **修订记录：**
> - 2026-11-09 v1：初稿（需求评估、架构设计、6 大技术缺口与 4 项待决策议题）。
> - 2026-11-09 v2（评审一）：① **Q1 定案**：全局 `agent_profiles` + 项目 `team` 双层架构；② **Q2 定案**：QA 默认只读，暴露默认关闭的「允许编写自动化测试脚本」开关；③ **Q3 定案**：项目小组模式强制开启 Git Worktree 沙盒隔离；④ **Q4 定案**：默认开启 `chain_within_cycle=true`。
> - 2026-11-09 v3（评审二终审定案）：**细化 Git 提交把关语义**——项目小组模式下「强制 Worktree 沙盒隔离」仅指任务执行阶段的安全隔离；当通过审查合入主分支时，**不默认执行 `git commit` 与 `git push`**（通过内部集成分支 `orcsy/stage/<project>` 衔接下游 worktree + 增量落地主分支工作区未提交态），最终提交（`git commit`/`git push`）必须由人工把关确认！

---

## 1. 需求评估与可行性分析

### 1.1 需求拆解与核心目标

本需求将 Orcsy 从当前的「**单项目单全栈执行 Agent + 可选 Review Agent**」演进为「**可自由编排的多模型多角色项目小组（Project Team）**」，核心诉求可拆解为 5 条：

| # | 需求点 | 语义说明 |
|---|---|---|
| R1 | **自由组合多个模型 Agent** | 用户可针对任意项目自由勾选/编排多个不同模型（Provider/Model/参数）的 Agent 成员组成项目小组，同时**必须向下兼容**现有的「单兵 Agent 模式」，未配置小组的项目零感知平滑运行 |
| R2 | **项目经理（PM）** | 领取用户下发的原始需求任务（或未分派任务），通过只读探索项目上下文后，拆分为结构化子任务（含分工角色与前置依赖关系）并派发到项目任务队列 |
| R3 | **前后端专职开发工程师（Backend / Frontend）** | 按角色标签分别领取对应的后端/前端开发任务，使用各自主擅长的模型与工具完成编码、单测与提交，并可复用现有 Review Agent 门禁把关代码质量 |
| R4 | **测试工程师（QA）** | 在前后端开发子任务完成后自动进入联调验收阶段，驱动本地开发服务、HTTP API 与 Chromium 浏览器执行端到端联调测试 |
| R5 | **QA 新建 Bug 与回归闭环** | QA 具备向项目任务队列新建 Bug 任务的权限（指定修复角色为后端或前端）；Bug 修复完成后能自动触发原 QA 任务回归验证，形成完整质量闭环 |

### 1.2 现有架构基础（可直接复用的基建）

经源码审计，Orcsy 已具备多项关键的底层基建，极大降低了改造成本：

1. **角色字段与过滤钩子已预留**：
   - `Task.assignee`（`orcsy/task_model.py:58`）已存在于任务数据模型与 YAML frontmatter 中。
   - `ProjectAgent.scan_and_work()`（`orcsy/agent.py:172-175`）已有按 `assignee` 过滤领取的逻辑与预留注释：`# assignee 非空时只有匹配的 Agent 能领取（支持一个项目里按角色分派）`。
2. **任务拆分工具已具备原型**：
   - `tool_create_tasks`（`orcsy/tools.py:1025-1097`）已支持校验并生成子任务 `.md` 文件、自动分配 `order` 及关闭父任务（`finish(action="split")`）。
3. **前后端联调引擎已完整上线**：
   - `orcsy/tools_debug.py` 与 `orcsy/debug_session.py` 已内置 `dev_server_*`（动态 `{PORT}` 分配、健康探针、任务结束必清理）、`http_request`（会话 Cookie 保持、主机白名单）与 `browser_*`（10 个 Chromium CDP 自动化工具）。测试工程师可以直接开箱复用，无需另造测试运行时。
4. **多模型隔离与独立门禁模式已验证**：
   - `Review Agent`（`orcsy/review.py`）已验证「多角色各自绑定独立 LLM + 按角色裁剪只读/受限工具集 + 分角色用量记账 + 三态配置覆盖」在当前框架下运转良好。
5. **强停与审批基础设施已支持多任务键**：
   - `TaskControls`（`orcsy/task_control.py:23`）以 `(project, task_id)` 为键，天然支持同一项目内多个任务各自受控停止；`DebugSession` 使用随机端口和临时 profile 目录，支持并发实例隔离。

### 1.3 核心技术缺口与高风险挑战（必须解决）

虽然基建扎实，但从「单兵」跨越到「项目小组协同」，现有代码存在 **6 个结构性缺口与陷阱**，若直接硬拼会引发严重故障：

| # | 挑战 / 现状缺口 | 真实代码依据 | 若不解决会导致的后果 |
|---|---|---|---|
| **G1** | **无任务依赖（DAG）机制** | `task_model.py` 仅按 `requeue_seq → priority → order` 排序，无 `depends_on` / `blocked_by` 字段 | PM 拆出「后端 → 前端 → QA 联调」3 个子任务后，调度器可能**先让 QA 跑联调测试**（代码尚未开发），或者前端在后端接口未就绪前盲写，导致大规模空转烧 Token |
| **G2** | **单工作区 / 单 worktree 并发冲突** | `executor.py:279-390` 硬编码单项目仅一个 `.worktree-arranger` 目录与单分支 `arranger/<project.name>`；且 `git.commit_and_merge` 默认 `False`（直接改主工作区） | 同一项目内 Backend 与 Frontend 若并发执行，会互相覆写主工作区文件、触发 Git `index.lock` 冲突，或一方 `reset --hard` 抹掉另一方的代码 |
| **G3** | **无代码改动即判 `blocked` 的守卫** | `executor.py:517-523`：`if not committed: return "blocked", None` | 在 `commit_and_merge=True` 模式下，QA 联调全部通过且未改任何代码时调用 `finish(action="completed")`，会被执行器误判为 `blocked`！PM 若判定小需求无需改文件也会撞坑 |
| **G4** | **QA 提 Bug 后的回归状态机缺失** | `tool_create_tasks` 只有 `close_current=True`（标记 `cancelled`）或 `False`（原状态不变）两种语义 | 若 QA 提 Bug 后直接 `completed` 归档，Bug 修完后**无人回归复测**；若保持 `open`，下轮调度器会立刻重跑 QA 任务（此时 Bug 还没修），造成无限空转；若直接 `blocked`，Bug 修完后无法自动唤醒回归 |
| **G5** | **PM 递归拆分死锁 & 自由组合缺省降级** | 当前所有任务 `assignee` 默认空串；无拆分深度 `split_depth` 概念 | ① PM 拆出的子任务若忘记写 `assignee` 或写错角色名，会再被 PM 自己领走无限套娃拆分；② 用户只配 `[PM, 后端]`（未配前端/QA）或只配 `[后端, 前端]`（无 PM）时，任务会无人认领而卡死 |
| **G6** | **配置繁琐与多轮周期耗时放大** | 每次全项目调度仅执行 `max_tasks_per_cycle=1`；每个项目都要手动配一遍多套 API Key/Base URL | ① 用户进驻多个项目时，若每个项目都要填 4 份大模型密钥配置，体验极差；② 1 个需求拆成 4 个阶段，若每阶段等 300s 调度间隔，端到端交付耗时将超过 20 分钟 |

### 1.4 总体评估结论

- **技术可行性**：**高**（基于现有 `ProjectAgent` / `TaskExecutor` / `Review Agent` / `tools_debug` 架构平滑演进，无需推翻重写）。
- **产品价值**：**极高**。直击大模型开发长任务的三大痛点——① 单模型上下文过长易退化（PM 拆解降低单任务上下文压力）；② 模型特长差异化性价比最优（推理模型做 PM、高性价比代码模型做前后端、工具能力强的模型做 QA）；③ 实现「拆解 → 开发 → 审查 → 联调 → 提 Bug → 回归」的准研发团队闭环。
- **建议策略**：分 **4 个里程碑（M1–M4）** 增量推进。**M1 先解决任务依赖（DAG）与并发安全底座**，M2 落地角色模型体系与 PM/开发/QA 闭环，M3 完善调度连环推进与防死锁熔断，M4 交付看板可视化团队编排 UI 与角色用量统计。

---

## 2. 角色定义与能力边界矩阵

系统内置 4 个标准角色模板（同时支持用户自定义扩展角色 `custom`，满足自由组合需求）：

| 角色 ID | 中文名称 | 核心职责 | 认领规则 (`assignee`) | 代码写入权限 | `create_tasks` / 提 Bug | 本地调试工具 (`browser`/`dev_server`/`http`) | 过 Review 门 | 推荐模型策略 |
|---|---|---|---|---|---|---|---|---|
| `pm` | **项目经理** | 需求理解、代码库调研、拆解子任务、定义 `depends_on` 依赖图与接口契约 | `assignee == "pm"` 或未指派的原始需求 (`assignee == ""` 且 `split_depth == 0`) | **禁止**（纯只读：`list_dir`/`find_files`/`read_file`/`grep`/`read_skill`/`web_fetch`） | **允许拆分**（必指定子任务 `assignee` 与 `depends_on`） | **关闭** | **跳过**（不改代码） | 强推理/长上下文模型（如 Claude Sonnet/Opus、GPT-5、Gemini Pro） |
| `backend` | **后端开发工程师** | 后端架构、接口、数据库与业务逻辑编码及单元测试 | `assignee == "backend"`（或在无 PM 模式下认领后端/通用任务） | **允许**（完整文件操作 + `run_command`） | 默认关闭（或仅允许应急拆解，禁止跨端派发） | 按需可选（`http_request` / `dev_server` 可选开启） | **开启**（若项目启用了 Review） | 高性价比代码模型（如 DeepSeek-V3/Coder、Qwen-Coder、Claude Sonnet） |
| `frontend` | **前端开发工程师** | 前端页面、组件交互、状态管理、API 对接与构建检查 | `assignee == "frontend"` | **允许**（完整文件操作 + `run_command`） | 默认关闭（或仅允许应急拆解） | 可选开启（便于自测页面渲染与 Console 报错） | **开启**（若项目启用了 Review） | 擅长 UI/TS/CSS 的代码模型（如 Claude Sonnet、GPT-4o/5、Gemini） |
| `qa` | **测试工程师** | **前后端联调测试**、E2E 验收、发现缺陷后向任务列表新建 Bug 并等待回归 | `assignee == "qa"` | **默认只读**（可选开关「允许编写自动化测试脚本」`allow_test_scripts`） | **拥有 `report_bug` 权限**（可新建指派给 `backend`/`frontend` 的 Bug 任务，不关闭当前 QA 任务，转入等待回归） | **默认全开**（`dev_server_*` + `http_request` + `browser_*` + `run_command`） | **跳过**（若未修改业务代码） | 工具调用稳健 + 多步调试能力强的模型 |

> **关键原则**：**角色与模型解耦**。用户可自由决定小组里启用哪些角色（例如仅启用 `[PM, 后端]`，或 `[后端, 前端, 测试]`，甚至同一角色绑定同款或不同款模型，或者添加自定义角色如 `devops`）。

---

## 3. 总体架构设计

### 3.1 协作全链路生命周期（Target Workflow）

```
用户下发需求（看板新建 / Issue 导入 / tasks/*.md，assignee="" 或 "pm"）
  │
  ▼
[阶段 1] 项目经理（PM）认领（免 worktree，主仓库只读调研）
  ├─ 分析需求与既有代码结构（read_file / grep / list_dir）
  ├─ 判定任务体量：
  │    ├─ 极简单一任务 → 调用 reassign_task 直接改写 assignee="backend"|"frontend"，不产生多余文件
  │    └─ 需多端协同/拆解 → 调用 create_tasks 创建子任务（写进 tasks/）：
  │         ├─ T-1 [assignee: backend]  实现后端接口与单测
  │         ├─ T-2 [assignee: frontend, depends_on: [T-1]] 实现前端页面并对接接口
  │         └─ T-3 [assignee: qa,       depends_on: [T-1, T-2]] 前后端联调测试
  └─ finish(action="split") → 原需求任务标记 cancelled/split 留档
  │
  ▼
[阶段 2] 开发工程师按依赖解锁顺序推进（T-1 → T-2，同级无依赖可安全并行）
  ├─ 扫描就绪条件：task.status == "open" 且 task.depends_on 全部已归档（status == "done"）
  ├─ [上下文透传] 自动将 depends_on 前置任务的「## Agent 处理结果」注入提示词，对齐接口契约
  ├─ [Worktree 沙盒] 从内部集成分支 orcsy/stage/<project> 拉取按任务隔离的 .worktree-arranger/<task_id> 编码与单测
  ├─ Review Agent 门禁（如项目已启用且模型互斥）：通过后持 Merge 锁合入集成分支 orcsy/stage/<project>
  ├─ [安全落地主分支·不默认提交] 将增量改动应用至主分支工作区/暂存区（不执行 git commit/push），归档进 tasks/done/
  └─ 解锁下游依赖任务（T-2、T-3 自动变为「就绪可执行」）
  │
  ▼
[阶段 3] 测试工程师（QA）前后端联调测试（T-3）
  ├─ 此时 T-1、T-2 均已安全落地主分支工作区（且已同步至集成分支），QA 基于完整集成代码启动服务
  ├─ 使用 dev_server_start 启动后端/前端服务 → http_request 测接口 → browser_* 跑页面交互、查 console/network
  ├─ 分支 A：联调全部通过 → finish(action="completed") → 跳过无改动拦截，归档 tasks/done/ ✅
  │          （最终全部代码改动保留在主分支未提交状态，等待人工检查 git diff 并亲自确认提交/推送）
  └─ 分支 B：发现 Bug（1~N 个）→ 调用 report_bug 新建 Bug 任务：
       ├─ 创建 B-1 [priority: P1, labels: [bug, qa-found], assignee: backend,  parent_id: T-3]
       ├─ 创建 B-2 [priority: P1, labels: [bug, qa-found], assignee: frontend, parent_id: T-3]
       └─ 调用 finish(action="wait_retest")：
            ├─ 系统自动将 [B-1, B-2] 追加写入 T-3 的 depends_on
            ├─ T-3 qa_round += 1，status 重置为 open（带「待前置 Bug 修复」锁定态）
            └─ 若 qa_round >= max_qa_rounds（默认 3）→ T-3 转 blocked，桌面通知人工裁决 ⚠
  │
  ▼
[阶段 4] Bug 修复与自动回归
  ├─ B-1、B-2 因优先级 P1 且无阻塞依赖，优先被 Backend / Frontend 认领修复并 Review 归档
  ├─ 当 B-1、B-2 全部进入 tasks/done/ 后，T-3 依赖自动全部满足
  └─ 调度器自动唤醒 QA 执行第 qa_round+1 轮回归测试，直至全部通过或触达熔断上限
```

### 3.2 任务数据模型与 DAG 依赖引擎（`task_model.py`）

#### 3.2.1 Frontmatter 新增字段（完全向下兼容）

在保留现有 `title / priority / status / labels / assignee / order / requeue_seq / review_pending / review_round` 的基础上，新增 5 个可选字段（旧任务文件缺失时取安全默认值）：

```yaml
---
title: 实现登录页面并对接 /api/login
priority: P1
status: open
assignee: frontend              # 角色 ID（pm / backend / frontend / qa / custom）或空
labels: [feature, ui]
depends_on: [P1-auth-api]       # 前置任务 ID 列表；全部进入 done/ 后才可被领取
parent_id: P1-user-login        # 所属父需求任务 ID（用于看板分组展示与链路追踪）
split_depth: 1                  # 拆分深度（用户下发=0，PM 拆出=1），防 PM 递归无限拆分
qa_round: 0                     # QA 回归测试已执行轮数（防 Bug 修补死循环）
created_by: pm                  # 创建者标识（user / issue / pm / qa）
---
```

#### 3.2.2 依赖状态判定与死锁防护

在 `task_model.py` 中新增依赖解析器 `evaluate_dependencies(tasks_dir: Path, all_tasks: list[Task], done_ids: set[str])`：

1. **已完成集合 `done_ids`**：扫描 `tasks/done/*.md` 的文件名 stem（以及当前目录 `status == "done"` 的任务 ID），构成已满足的依赖全集。
2. **任务就绪判定（`Task.dep_state` 计算属性，不落盘，实时计算）**：
   - `ready`：`depends_on` 为空，或全部 ID 均在 `done_ids` 中 → **可领取**。
   - `waiting`：存在未完成的前置任务（前置任务处于 `open` 或 `in_progress`）→ **跳过领取**，看板展示 `⏳ 等待前置 (1/2)`。
   - `upstream_blocked`：存在前置任务处于 `blocked` 或 `cancelled`（且非正常 split 迁移）→ **不可领取**，看板告警 `⚠ 前置受阻`，避免静默死等；若前置任务被拆分为子任务（`split_children` 记录），支持自动或提示重定向依赖。
3. **无环校验（Cycle Detection）**：`tool_create_tasks` 与 Web 手动编辑保存任务时，执行有向图 DFS 环检测；若存在环依赖或引用不存在的 ID，直接拦截报错。
4. **上游上下文透传（Handoff Context）**：
   - 当 `TaskExecutor._build_task_prompt` 构造 Prompt 时，若 `task.depends_on` 非空，自动读取前置任务文件（从 `tasks/done/<dep_id>.md`）中的 `## Agent 处理结果` 节（包含 `summary`、修改文件清单、关键备注，单前置截断 1500 字），拼装为 `## 上游前置任务交付摘要` 注入 User Prompt，彻底解决前后端及 QA 联调时的信息断层。

### 3.3 配置体系设计：全局 Agent 模板库 + 项目小组自由编排

为解决「多个项目重复配置多套模型 Key/URL 过于繁琐」的痛点（挑战 G6），采用 **两层配置复用架构**（延续 `ReviewConfig` 已验证的继承+部分覆盖模式）：

#### 3.3.1 全局配置：`agent_profiles`（全局 Agent 角色预设池）

在 `config.yaml` 新增顶级 `agent_profiles` 字典，用户只需配置一次各角色常用的模型接入与工具偏好，即可进驻任意项目：

```yaml
# config.yaml 新增全局 Agent 角色预设池（可选，未配置时单兵模式照常工作）
agent_profiles:
  pm_default:
    name: "项目经理 PM"
    role: pm                         # pm | backend | frontend | qa | custom
    llm:                             # 独立模型绑定（缺省字段继承全局 llm）
      provider: anthropic
      model: "claude-sonnet-4-5"
      base_url: "https://api.anthropic.com"
      api_key: "sk-..."
      temperature: 0.2
    max_turns: 20
    produces_code: false             # 不产生代码提交，免走无改动 blocked 校验与 Review 门
    tools:                           # 按角色预设工具开关（未列出的按角色默认边界裁剪）
      write_file: false
      edit_file: false
      run_command: false
      create_tasks: true
    extra_prompt: "拆分子任务时务必注明接口契约与验收命令，子任务不超过6个。"

  backend_default:
    name: "资深后端开发"
    role: backend
    llm:
      provider: openai
      model: "deepseek-chat"
      base_url: "https://api.deepseek.com/v1"
      api_key: "sk-..."
    produces_code: true

  frontend_default:
    name: "资深前端开发"
    role: frontend
    llm:
      provider: anthropic
      model: "claude-sonnet-4-5"
    produces_code: true

  qa_default:
    name: "自动化测试工程师"
    role: qa
    llm:
      provider: openai
      model: "gpt-4o"
    produces_code: false
    allow_test_scripts: false        # 是否允许 QA 修改/编写自动化测试脚本文件
    tools:
      dev_server_start: true
      dev_server_logs: true
      dev_server_stop: true
      http_request: true
      browser: true                  # 启用全部 10 个浏览器调试工具
```

#### 3.3.2 项目级配置：`team`（项目小组自由编排）

在 `ProjectConfig` 中新增 `team` 结构（`None` = 沿用传统单兵模式，100% 向后兼容）：

```yaml
projects:
  - name: ecommerce-web
    path: "D:/Repos/ecommerce-web"
    enabled: true
    team:
      enabled: true                  # true=启用项目小组模式（强制开启 Git Worktree 沙盒隔离）；false/缺省=传统单兵模式
      execution_mode: parallel_worktree # parallel_worktree（默认，按任务独立 worktree 安全并发） | sequential（强制串行）
      max_qa_rounds: 3               # QA 发现 Bug 后打回返工→自动回归测试的最大循环轮数
      max_split_depth: 1             # PM 最大拆分深度（防无限递归拆任务）
      chain_within_cycle: true       # 默认开启：同轮周期内依赖解锁后立即连续推进下一阶段任务（免等300s周期）
      max_chain_steps_per_cycle: 10  # 单周期链式推进步数上限（防极端异常死循环）
      fallback_role: backend         # 若未配置 PM 且收到未指派任务时的默认接单角色（或 "error" 等人工指派）
      members:
        - id: pm                     # 成员在项目内的 assignee 路由标识（即任务 frontmatter 的 assignee 值）
          profile: pm_default        # 引用全局 agent_profiles；也可省略 profile 完全内联自定义
          enabled: true
          # 支持项目级部分覆盖：例如针对本项目覆盖技术栈提示词或换模型
          extra_prompt: "本项目采用 Python FastAPI + Vue 3，拆分时请对应指派 backend / frontend。"
        - id: backend
          profile: backend_default
          enabled: true
          llm:                       # 项目内可单独为后端换模型（字段级覆盖 profile.llm）
            model: "deepseek-reasoner"
        - id: frontend
          profile: frontend_default
          enabled: true
        - id: qa
          profile: qa_default
          enabled: true
```

#### 3.3.3 自由组合与降级路由规则

针对用户「自由组合」的需求，可能出现任意子集组合，路由逻辑（`resolve_team_routing`）必须严密覆盖所有组合：

| 项目小组配置组合 | 收到无 `assignee` 原始任务 (`split_depth==0`) 时 | PM 拆分子任务时允许指派的角色集合 | 异常/缺角色兜底 |
|---|---|---|---|
| **完整 4 角色**：`[pm, backend, frontend, qa]` | 由 `pm` 领取拆分 | `backend`, `frontend`, `qa` | — |
| **精简组合**：`[pm, backend, qa]`（无前端） | 由 `pm` 领取拆分 | 仅 `backend`, `qa`（Prompt 动态屏蔽 `frontend`） | 若拆分强行写了 `frontend`，`create_tasks` 直接校验拦截要求重拆 |
| **无 PM 组合**：`[backend, frontend, qa]` | 由 `team.fallback_role`（或面板创建任务时选择 `assignee`）领取 | N/A | 若未设 `fallback_role` 且无 `assignee`，任务停留在队列并提示「待指派角色」 |
| **自定义扩展角色**：如增加 `dba` 或 `backend_2` | 若有 `pm`，`pm` 的 Prompt 动态读取 `members` 的 `id` 与 `description` 进行精准派单 | 项目内所有已启用非 `pm` 成员 `id` | 同上 |
| **`team.enabled = false` 或未配 `team`** | 走原有单兵模式 (`ProjectAgent` 单模型全权负责) | 原有拆分子任务逻辑 | 100% 保持历史行为 |

### 3.4 并发控制与 Git 工作区安全模型（解决挑战 G2 & G3）

项目小组内部存在多个角色，必须严防多 Agent 并发写同一目录导致文件覆盖，同时严格守住 **「安全合入主分支后不默认提交，最后提交必须由人工把关」** 的底线：

#### 3.4.1 核心策略：强制 Worktree 沙盒隔离 + 合入主分支工作区（默认不自动 Commit/Push）

将「执行隔离」与「主分支最终提交」彻底解耦：当启用 `team.enabled = true` 时，**强制开启按任务隔离的 Git Worktree 沙盒**，但合入主分支时 **默认只落地工作区改动、不产生主分支 `git commit` 和 `git push`**：

```
主分支 main (C0, 人工未提交前 HEAD 始终停在 C0)
  ▲
  │ [5] 增量安全落地到 main 工作区（仅改写工作区/暂存区，绝不自动 git commit / push）
  │     待全部任务完成后，由人工检查 git diff 并亲自执行最终 commit & push
  │
内部集成分支 orcsy/stage/<project> (C0 → C1 → C2，用于衔接下游任务上下文)
  ├──< 分叉 .worktree-arranger/T1 (arranger/proj/T1) ── 后端开发 → Review → 合入 stage (C1)
  └──< 分叉 .worktree-arranger/T2 (arranger/proj/T2) ── 基于 C1 前端开发 → Review → 合入 stage (C2)
```

具体分角色运行策略如下：

1. **只读角色（`pm`，以及 `allow_test_scripts=false` 时的 `qa`）**：
   - 不创建/修改 Git worktree，直接在 `project.path`（主工作区，已安全合入前序任务成果的当前最新集成代码）上以只读工具运行；仅 `qa` 启用 `DebugSession` 拉起开发服务与浏览器测试（只写临时工件目录，不污染代码库）。
   - **修复无改动拦截坑（G3）**：当成员配置 `produces_code=false`（或 `allow_test_scripts=true` 但 QA 当轮仅验证通过未改动文件）时，`executor.py` 的 `action="completed"` **跳过「无提交即 blocked」拦截及 Review 门禁**，直接归档或流转。
   - 若 `qa` 运行测试时产生了临时缓存文件（如 `.pytest_cache` / `coverage`），应确保清理或不纳入业务变更校验。

2. **写入型角色（`backend` / `frontend` / 开启 `allow_test_scripts=true` 且产出了测试脚本的 `qa`）**：
   - **沙盒隔离执行**：强行从内部集成分支 `orcsy/stage/<project>`（初始化或同步自当前 `main` HEAD）创建 **按任务隔离的 worktree**：
     - 目录：`.worktree-arranger/<task_id>`
     - 分支：`arranger/<project>/<task_id>`
     - 任务执行期间的 WIP 提交仅落在这条临时沙盒分支中，主分支历史保持零污染；若任务中途失败或被强停，直接丢弃该 worktree，主分支工作区安然无恙。
   - **安全合并与冲突处理**：默认 `execution_mode = parallel_worktree`，无前置依赖的任务可在各自 worktree 中安全并行；完成后经 Review 门禁，在 **项目级 Merge 互斥锁（`project_merge_lock`）** 保护下先合入 `orcsy/stage/<project>`：
     - 若发生合并冲突（如两端同时改了同个文件），merge 在 `stage` 阶段即安全中止，不影响主分支工作区；系统保留分支提交并将冲突清单写入返工说明打回原开发 Agent 解决。
   - **安全落地主分支·人工把关最终提交（关键机制）**：
     - **默认模式（`auto_commit = false`，严守最后提交由人把关）**：`stage` 合并成功后，系统计算本次新增增量 diff（`prev_stage..new_stage`），通过三方补丁（`git apply --3way` / `git checkout` 增量同步）**安全落地到主分支（`project.path`）的工作区（未提交态）**，随后清理临时 `.worktree-arranger/<task_id>`；**全程不在主分支执行 `git commit` 和 `git push`**。
     - **下游任务衔接与人工提交同步**：因为 `orcsy/stage/<project>` 已记录了前序成果，后续 `T2`（即使人工尚未在主分支 commit）依然从 `stage` 拉出包含 `T1` 代码的新 worktree；当用户审查 `git diff` 满意并在主分支手动执行 `git commit` 后，Orcsy 下次启动执行前检测到 `main` HEAD 推进或工作区干净，自动将 `orcsy/stage/<project>` 同步至最新 `main` HEAD。
     - **显式可选模式**：仅当用户主动开启 `git.commit_and_merge = true`（或在看板主动点击「确认提交当前更改」按钮并输入 commit message）时，才会在主分支生成最终 commit。

3. **非 Git 目录边界兜底**：
   - Orcsy 在新建/导入项目时已默认自动初始化 Git 仓库；若历史遗留的非 Git 文件夹启用小组模式，项目保存接口与弹窗会提示一键初始化 Git（若仍处于纯非 Git 目录，运行期自动降级为同项目写任务串行互斥锁兜底）。

- **传统单兵模式（`team.enabled = false`）**：保持原有 `git.commit_and_merge` 开关行为完全不变（默认 `false` 不自动提交）。
- **阶段内链式推进（`chain_within_cycle = true`，Q4 定案默认开启）**：
  - 前序任务完成安全合入并解锁下游依赖任务后，调度器无需退出等下一个 300s 周期，原地立即触发重扫并唤醒下游就绪任务（受 `max_chain_steps_per_cycle = 10` 上限保护），保证需求全流程顺畅推进。

### 3.5 工具权限扩展设计

#### 3.5.1 增强 `tool_create_tasks`（`orcsy/tools.py`）

为子任务对象扩展参数，并保持原有默认缺省向下兼容：

```json
{
  "tasks": [
    {
      "title": "实现用户登录后端接口 POST /api/login",
      "description": "## 目标...\n## 接口契约...\n## 验收标准\n- pytest tests/test_auth.py 通过",
      "priority": "P1",
      "assignee": "backend",
      "depends_on": [],
      "labels": ["backend", "api"]
    },
    {
      "title": "实现登录页面并对接登录接口",
      "description": "...",
      "priority": "P1",
      "assignee": "frontend",
      "depends_on_refs": [0],
      "labels": ["frontend", "ui"]
    },
    {
      "title": "登录功能前后端联调与 E2E 验证",
      "description": "...",
      "priority": "P1",
      "assignee": "qa",
      "depends_on_refs": [0, 1],
      "labels": ["qa", "integration"]
    }
  ],
  "close_current": true
}
```

> **精妙设计：`depends_on_refs`（批内下标引用）**
> 当 PM 一次性创建 3 个子任务时，子任务的文件名 ID（slug）尚未落盘，大模型无法准确预知最终生成的真实任务 ID（尤其是带重名后缀 `-2` 时）。允许传 `depends_on_refs: [0, 1]` 引用本次 `tasks` 数组内的第 0、1 项，由 `tool_create_tasks` 在落盘时自动解析翻译为真实生成的 `stem` 写入 `depends_on`，同时支持传已存在的外部任务 ID `depends_on: ["existing-task-id"]`。这能把 PM 依赖编排的准确率提升到近 100%！

#### 3.5.2 新增 QA 专用工具：`report_bug`（或 `create_bug`）

为测试工程师提供语义明确、强约束的提 Bug 工具（而非复用模糊的 `create_tasks`），确保 Bug 质量与回归闭环：

```json
{
  "bugs": [
    {
      "title": "登录失败时前端按钮未恢复可点击状态",
      "assignee": "frontend",
      "priority": "P1",
      "severity": "major",
      "repro_steps": "1. 打开 /login 输入错误密码点击登录\n2. 接口返回 401，但按钮仍处于 loading 禁用态",
      "expected": "收到 4xx 响应后重置 loading 并弹出错误提示",
      "actual": "按钮永久置灰无法重试，Console 无报错",
      "evidence": "POST /api/login -> HTTP 401; 截图: artifacts/login-err.png"
    }
  ]
}
```

**`report_bug` 服务端执行逻辑**：
1. 校验 `assignee` 必须是当前项目小组中已启用的开发角色（`backend` / `frontend` / 等），禁止指派给自己（`qa`）或 `pm`。
2. 在 `tasks_dir` 生成 Bug 任务 `.md` 文件：自动注入 `labels: ["bug", "qa-found", severity]`、`parent_id: <qa_task.id>`、`created_by: qa`、`status: open`、高优先级 `P0`/`P1`。
3. 在内存中记录本次 QA 任务产生的 `created_bug_ids`（存放在 `ToolContext.created_bugs`），以便 QA 结束任务时自动绑定回归依赖！
4. 当 QA 调用 `finish(action="wait_retest")`（或调用过 `report_bug` 后结束任务，系统强制校验拦截防止误标 `completed`）：
   - 自动将 `created_bug_ids` 追加进当前 QA 任务的 `depends_on`；
   - `qa_round += 1`；
   - 若 `qa_round < max_qa_rounds`：将 QA 任务 `status` 重置为 `open`（而非归档），等待 Bug 修复完毕自动触发回归测试；
   - 若 `qa_round >= max_qa_rounds`：转 `status: blocked`，记录「联调发现 Bug 超过最大回归轮数，转人工裁决」并发送桌面提醒。

#### 3.5.3 新增 PM 轻量工具：`reassign_task`

当用户下发的是一个单一小任务（例如「修复接口空指针异常」），PM 无需拆成子任务产生冗余文件，可调用 `reassign_task(assignee="backend", note="任务粒度合适，直接指派给后端修复")`，系统直接更新当前任务 frontmatter 的 `assignee` 并保持 `status: open` 供后端立即认领，随后 PM 退出本任务。

#### 3.5.4 工具开关与审批联动

- `report_bug` 与 `reassign_task` 注册入 `TOOLS` 并在 `orcsy/tools.py` 纳入 `TOOL_CATEGORIES`（`task_collab` 组）。
- `report_bug` 归入 `MUTATING_TOOLS`（`orcsy/approvals.py`），受 `ask_on_change` 审批模式管辖，但在 `autonomous` / `full_access` 模式下可直接创建 Bug 无需阻塞人工确认（确保无人值守测试流畅运行）。

### 3.6 角色提示词工程（`orcsy/prompts.py`）

为不同角色构建差异化系统提示词注入函数 `build_role_system_prompt(role_cfg, team_members, ...)`：

1. **PM（项目经理）系统提示词要点**：
   - 动态注入 **「当前项目小组可用角色名录」**（列出每个已启用成员的 `id`、名称与职责说明），严格禁止派单给未启用的角色。
   - 指导拆分原则：按技术边界与独立可验收原则拆分（通常后端、前端、联调测试各成任务），用 `depends_on_refs` 显式编排前后序依赖；明确**接口契约**（URL、请求/响应字段）写入描述；单个简单任务优先使用 `reassign_task`。
   - 严禁 PM 自行修改任何源码文件（对应写工具也已被物理剥离）。
2. **Backend / Frontend（开发工程师）系统提示词要点**：
   - 明确自身定位（专注本端职责与质量边界，不越界乱改另一端核心架构，除非需要微小联调适配并在 `notes` 说明）。
   - 若有上游 `depends_on`，引导优先阅读注入的「上游前置任务交付摘要」，确认已有的接口/数据模型。
   - 要求在 `finish(summary, notes)` 中写清楚新增/修改的 API 路由、端口、启动命令或前端入口，供下游开发与 QA 无缝衔接。
3. **QA（测试工程师）系统提示词要点**：
   - 指导联调完整闭环：
     1. 通读需求描述、上游前后端任务的交付摘要及 `AGENT.md` 中的服务启动命令；
     2. 先使用 `dev_server_start` 拉起后端服务与前端服务（使用 `{PORT}` 避免端口冲突），等待 `ready_url` 就绪；
     3. 若项目涉及 API，用 `http_request` 校验状态码、鉴权、边界异常入参；
     4. 若项目涉及 Web 前端，用 `browser_navigate`、`browser_snapshot`、`browser_click`、`browser_type` 模拟真实用户走通核心链路，并用 `browser_console` 与 `browser_network` 断言无报错、无 500/404 请求；
     5. 一旦发现缺陷，收集请求参数、响应体、Console 错误与截图路径，调用 `report_bug` 精确分派给 `backend` 或 `frontend`，随后调用 `finish(action="wait_retest")`；
     6. 若全部验收条目通过，调用 `dev_server_stop` 清理后 `finish(action="completed")`。

### 3.7 可观测性与看板 UI 设计

1. **全局设置页新增「🤖 Agent 角色预设池」分区（`SettingsView.vue`）**：
   - 管理全局可复用的 Agent 角色模板（默认预置 4 个开箱模板骨架，用户只需填入对应的 API Key/模型即可启用；支持新增自定义模板、复制、测试模型连接、编辑角色默认工具集与补充提示词）。
2. **项目编辑弹窗升级「项目小组编排」（`Dialogs.vue`）**：
   - 提供顶部模式切换：**「单兵模式（兼容）」** vs **「项目小组模式（Multi-Agent Team）」**。
   - 在项目小组模式下：
     - 展示可视化成员编排列表（可点击「+ 添加角色成员」自由组合 PM、后端、前端、测试或自定义角色）；
     - 每个成员卡片支持：启用开关、下拉选择绑定的全局 Agent 模板、按本项目快速覆盖切换特定模型、查看工具权限摘要、填写本项目技术栈补充提示词；
     - 底部实时展示小组协作拓扑预览（例如：`👑 PM (GPT-5) → ⚙️ 后端 (DeepSeek) → 🎨 前端 (Claude) → 🧪 测试 (GPT-4o)`）及并发/熔断参数（工作区并发策略、最大 QA 回归轮数、周期内链式推进开关）。
3. **任务看板与列表增强（`DashboardView.vue` / `ProjectsView.vue` / `Dialogs.vue`）**：
   - **任务创建/编辑弹窗**：增加「指派角色 (`assignee`)」下拉框（可选：自动路由/未指派、PM、后端、前端、测试等小组内成员）与「前置依赖 (`depends_on`)」多选框。
   - **任务卡片角标**：
     - 角色徽标：`👑 PM`、`⚙️ 后端`、`🎨 前端`、`🧪 测试`、`🐞 QA Bug`；
     - 依赖锁定徽标：当 `dep_state == "waiting"` 时灰显提示 `⏳ 等待前置 (0/2)`；当 `dep_state == "upstream_blocked"` 时红色警示 `⚠ 前置受阻`；
     - 回归轮次徽标：`🔁 回归 #2`（当 `qa_round > 0`）。
   - **项目卡片头部**：展示本项目小组成员徽标组（如 `[PM] [后端] [前端] [测试]`）以及当前活跃正在执行的成员高亮呼吸指示。
4. **用量统计页（`UsageView.vue` & `usage.py`）**：
   - `usage.record()` 增加可选字段 `role: str = ""` 与 `agent_name: str = ""`；看板「用量」页签增加按角色维度的 Token 消耗明细统计，方便用户直观评估不同模型角色组合的 ROI。

---

## 4. 关键决策与备选方案（全部定案）

| # | 决策议题 | 定案方案 | 备选方案（未采纳） | 定案理由与权衡 |
|---|---|---|---|---|
| **D1** | **与 Review Agent 的关系定位** | **正交并存，各司其职**：Review Agent 负责单个开发任务的 Diff 级代码审查（微观）；QA 测试工程师负责全链路服务拉起与前后端联调（宏观）；PM 与未改代码的 QA 跳过 Review 门 | 用 QA 彻底取代 Review Agent；或让 Review 也对 QA 结果复核 | Review 看代码规范/边界漏洞（静态+单测），QA 看运行态端到端交互；让 QA 过只看 Diff 的 Review 没有意义（QA 没改代码） |
| **D2** | **QA 发现 Bug 后原 QA 任务状态** | **自动挂起等待回归（`finish(action="wait_retest")` + 自动将新建 Bug ID 写入 `depends_on` + 保持 `status=open`）** | QA 提完 Bug 直接 `done`；或标 `blocked` 等人工重启 | 唯一能实现「提 Bug → 开发修 → 自动触发回归验证」全自动无人值守闭环的方案，同时以 `max_qa_rounds` 兜底防止死循环 |
| **D3** | **QA 是否允许修改源码（Q2 已定案）** | **默认只读禁改业务源码**（`produces_code=false`），同时提供默认关闭的「允许编写自动化测试脚本（`allow_test_scripts`）」开关（开启后可新建/编辑测试脚本并纳入独立 worktree 提交） | 永久允许 QA 改业务代码；或永久禁止 QA 写任何文件 | 保证测试与开发职责边界清晰、避免绕过 Review；开关满足沉淀 E2E 测试脚本的进阶需求 |
| **D4** | **Worktree 隔离与最终提交把关（Q3 终审定案）** | **强制 Worktree 沙盒执行 + 集成分支衔接 + 安全落地主分支不默认提交**：项目小组模式下，所有写代码任务按 `.worktree-arranger/<task_id>` 独立隔离开发，过 Review 后先合入内部集成分支 `orcsy/stage/<project>`，再将增量安全合入主分支工作区（未提交态），**绝不默认执行主分支 `git commit` 与 `git push`**，最终提交必须由人工审阅把关（仅当显式开启 `commit_and_merge=true` 时才自动提交） | 自动在主分支产生 `git commit` 并推送；或无隔离直接共享同一物理目录并发写盘 | 既从根源彻底消灭同项目多 Agent 写盘互踩、支撑无依赖任务安全并行，又保证主分支 Commit 历史干净可控——最后提交权100%留给人类 |
| **D5** | **调度周期内流转机制（Q4 已定案）** | **默认开启同周期链式推进（`chain_within_cycle=true`）**：项目小组内前序任务完成并解锁下游任务后，同周期立即连续认领下一就绪任务（默认单周期链式步数上限 `max_chain_steps_per_cycle=10`） | 严格每 300s 调度一轮只做 1 步 | 避免「PM→后端→前端→测试」多阶段需求干等 15~20 分钟空窗，实现单周期内秒级接棒交付 |
| **D6** | **配置层级设计（Q1 已定案）** | **全局 `agent_profiles` 模板池 + 项目 `team.members` 自由引用/覆盖** | 仅在每个项目里独立手填多套模型参数 | 一次配置各角色模型即可进驻任意项目，同时保留项目级覆写与自由增减成员的灵活性 |

---

## 5. 分文件详细改动清单

### 5.1 后端（`orcsy/`）

| 文件路径 | 核心改动内容 |
|---|---|
| `orcsy/task_model.py` | ① `Task` 扩充 `depends_on`、`parent_id`、`split_depth`、`qa_round`、`created_by` 字段；② `parse_task_file` 兼容解析；③ 新增 `evaluate_dependencies()` 与环检测 `detect_dependency_cycle()`、`append_task_dependencies()`；④ 新增读取已完成任务摘要工具函数 `read_done_summaries()` |
| `orcsy/config.py` | ① 新增 `AgentProfileConfig`、`TeamMemberConfig`、`ProjectTeamConfig` dataclass（`chain_within_cycle` 默认 `True`、`max_chain_steps_per_cycle` 默认 `10`、`execution_mode` 默认 `"parallel_worktree"`）；② `AppConfig` 增加 `agent_profiles`；③ `ProjectConfig` 增加 `team`；④ 新增 `resolve_worktree_isolation()`（`team.enabled=True` 时强制返回 `True`）与 `resolve_commit_and_merge()`（默认仍保持 `False`，由人把关最终提交）；⑤ 新增 `resolve_team_member_llm()`、`resolve_team_member_tools()`（含 QA `allow_test_scripts` 裁剪控制）及小组配置合法性校验 |
| `orcsy/tools.py` | ① `tool_create_tasks` 支持 `assignee`、`depends_on`、`depends_on_refs`、`parent_id`、`labels`，强制校验小组模式下的目标角色合法性与 `split_depth` 限制；② 新增 `tool_report_bug`（QA 专用，写 Bug 文件并在 `ToolContext.created_bugs` 登记）；③ 新增 `tool_reassign_task`（PM 单任务免拆分直派）；④ `ToolContext` 增加 `created_bugs: list[str]` 与 `team_roles: dict` 元数据 |
| `orcsy/approvals.py` | 将 `report_bug`、`reassign_task` 纳入 `MUTATING_TOOLS` 权限矩阵分类 |
| `orcsy/llm.py` | `finish` 协议 `action` 枚举扩展支持 `"wait_retest"`（QA 等待回归）；`AgentResponse` 兼容透传 |
| `orcsy/prompts.py` | ① 新增 `build_pm_prompt()`、`build_dev_prompt(role)`、`build_qa_prompt()`（动态注入项目小组有效角色清单及各角色职责边界）；② `TaskExecutor._build_task_prompt` 增加 `## 上游前置任务交付摘要` 段落 |
| `orcsy/git_ops.py` | 新增集成分支管理与增量无提交落地工具函数（`sync_stage_branch()`、`apply_delta_to_worktree()`、`has_uncommitted_changes()`），支持 `orcsy/stage/<project>` 与主分支安全增量合入 |
| `orcsy/executor.py` | ① 支持 `work_root` 按任务目录 `.worktree-arranger/<task_id>` 从 `orcsy/stage/<project>` 隔离创建；② 合并路径拆分：**隔离合入 `stage`（加 Merge 锁）** + **当 `commit_and_merge=False` 时仅将增量安全合入主分支工作区，绝不执行主分支 `git commit` 与 `git push`**；③ 只读角色（`produces_code=false`）在 `action="completed"` 时跳过空提交 `blocked` 拦截与 Review 门；④ 增加 `team_role`、`produces_code` 上下文传递 |
| `orcsy/agent.py` | ① 将单兵 `ProjectAgent` 升级支持 `team` 调度协调器（或内部委托多个角色执行单元）：筛选依赖就绪任务、按 `assignee` 匹配成员模型与工具集、处理 `wait_retest` 绑定 Bug 依赖与 `qa_round` 熔断；② 支持 `chain_within_cycle` 同周期链式流转与同项目写锁 |
| `orcsy/scheduler.py` | 适配 `ProjectAgent` 传入 `agent_profiles` 与项目级并发/Merge 互斥锁协调 |
| `orcsy/usage.py` | `record()` 增加可选字段 `role`、`agent_name`；`summary()` 增加按角色维度的用量聚合块（完全向下兼容历史 JSONL 记录） |
| `orcsy/web.py` | ① `GET /api/state` 导出 `agent_profiles`、角色内置元数据 `built_in_roles`、各项目 `team` 配置及成员状态、主分支待人工提交变更统计 (`pending_git_changes`)、任务的依赖状态 `dep_state` / `blocking_deps`；② `POST /api/config` 支持保存 `agent_profiles`（含 H2 密钥掩码三态处理）；③ `_project_payload` 与 `_save_config` 完整解析、脱敏并序列化 `project.team`（防静默丢失）；④ 任务保存接口支持 `assignee`、`depends_on` 校验；⑤ 新增可选 `POST /api/project/commit` 人工确认提交端点（由用户主动触发填 commit message 提交推送） |
| `config.example.yaml` & `__main__.py` | 同步补充 `agent_profiles` 与项目 `team` 配置注释示例（保持两处字节级一致） |

### 5.2 前端看板（`dashboard/src/`）

| 文件路径 | 核心改动内容 |
|---|---|
| `dashboard/src/api.ts` | 新增 `AgentProfileT`、`TeamMemberT`、`ProjectTeamT` 类型；扩充 `TaskT`（`depends_on`, `parent_id`, `dep_state`, `qa_round`）与 `ProjectT` / `AppStateT` 类型 |
| `dashboard/src/views/SettingsView.vue` | 左侧分区新增「👥 Agent 角色预设池」管理页：展示内置 4 角色模板与自定义模板，支持模型配置、连接测试、工具范围勾选（含 QA「允许编写自动化测试脚本」开关）、Prompt 模板编辑 |
| `dashboard/src/components/Dialogs.vue` | ① 项目弹窗新增「开发模式：单兵 / 项目小组自由编排」配置面板（勾选组合角色、选模板/覆写模型、设置流转与 QA 熔断参数）；开启小组模式时显示「已强制启用 Worktree 沙盒隔离（主分支默认不自动提交，留待人工把关）」说明并做非 Git 目录引导；② 任务新建/编辑弹窗支持指定 `assignee` 角色与 `depends_on` 前置任务，展示子任务/关联 Bug 链路跳转 |
| `dashboard/src/views/DashboardView.vue` & `ProjectsView.vue` | ① 项目卡片展示小组成员花名册、当前活跃执行角色状态以及「待人工审阅提交 (N 个文件)」提示徽标（支持一键打开人工提交确认弹窗或自行用 IDE 提交）；② 任务列表展示角色徽标（PM/后端/前端/测试/Bug）、前置依赖等待锁状态与 QA 回归轮数标签；③ `toggleProject` 等局部更新接口必须原样回传 `team` 字段防止误清空 |
| `dashboard/src/views/UsageView.vue` | 用量统计增加「按角色消耗」维度明细展示 |

---

## 6. 风险评估与缓解措施

| 风险点 | 影响等级 | 缓解与兜底方案 |
|---|---|---|
| **Token 消耗与执行耗时成倍增长** | 高 | ① 支持不同角色差异化选模型（小模型/性价比模型承担高轮次编码或审查）；② 默认开启上游交付摘要透传，大幅压缩下游探索代码的轮次；③ 限制 `max_split_depth=1`、`max_qa_rounds=3` 与单成员 `max_turns` 封顶 |
| **PM 拆分颗粒度失控（过粗或拆出 10+ 碎任务）** | 中 | ① PM Prompt 明确推荐单需求拆分 2~6 个子任务、严禁过度拆分；② `tool_create_tasks` 限制单次上限 10 个且硬校验 `assignee` 必在当前项目已启用角色内 |
| **PM 拆出的前置依赖任务中途 `blocked` 导致全链卡死** | 中 | ① `evaluate_dependencies` 实时识别 `upstream_blocked` 状态并在看板醒目标红告警 + 触发系统通知；② 用户在看板解除阻塞或调整依赖后下一轮自动恢复下游推进 |
| **多端并行修改同一公共文件（如 `package.json` / 路由注册表）引发合并冲突** | 中 | ① 小组模式强制启用按任务独立 Git Worktree 隔离编码；② 合入 `orcsy/stage/<project>` 失败时保留沙盒分支且**绝不污染主分支工作区**，自动附加冲突上下文打回 `open` 供开发 Agent 基于最新 `stage` 解决冲突 |
| **QA 幻觉/环境未起起来误报大量无效 Bug** | 中 | ① QA Prompt 强制要求先验证服务就绪（`dev_server_start` 返回健康态），`report_bug` 强制必填 `repro_steps`、`expected`、`actual`、`evidence`；② 超过 `max_qa_rounds` 熔断转人工避免无限打乒乓 |
| **人工提前修改或手工提交主分支代码与 `stage` 分支漂移** | 中 | 每次分配新任务前，检测主分支 `HEAD` 是否推进或存在人工手工改动；若主分支干净且 `HEAD` 已更新，自动将 `orcsy/stage/<project>` 重置同步到新 `HEAD`，保证人工提交后立即可继续下一批任务 |
| **存量配置与正在运行任务兼容升级** | 低 | `team` 默认缺省为 `None`（走单兵旧路径），旧任务文件缺省 `depends_on=[]`（视为 `ready`），升级过程对现有项目零破坏 |

---

## 7. 实施里程碑与工作量估算

建议按以下 4 个阶段渐进实施，每个阶段均具备独立的测试闭环：

| 里程碑 | 核心交付物 | 预计工作量 | 验收标志 |
|---|---|---|---|
| **M1：DAG 依赖引擎与 Worktree 隔离不自动提交底座** | `task_model.py`（`depends_on` / `parent_id` / `split_depth` / 环检测 / 上游摘要读取）；`git_ops.py` & `executor.py`（按任务 worktree 隔离 + `orcsy/stage/<project>` 集成分支 + 增量安全落地主分支工作区且默认不自动 `commit`/`push` + `produces_code=false` 免空提交拦截）；`tools.py`（`create_tasks` 支持 `assignee`/`depends_on_refs`） | 3.5 人日 | 单元测试覆盖依赖排序、环检测拦截、worktree 隔离、增量落地主分支零自动提交，原有回归测试全绿 |
| **M2：角色配置体系与多角色运行时** | `config.py`（`agent_profiles` + `ProjectTeamConfig` + 自由组合路由解析）；`tools.py`（`report_bug` + `reassign_task`）；`prompts.py`（PM/Dev/QA 角色提示词与契约透传）；`agent.py`（多成员领取、`wait_retest` 闭环、`qa_round` 熔断与周期内链式推进） | 4.5 人日 | 使用 FakeLLM 构建端到端剧本测试：覆盖「PM 拆分 → 后端 → 前端 → QA 联调提 Bug → 修复 → QA 回归通过归档」全流程及精简组合降级 |
| **M3：Web API 与看板多角色可视化 UI** | `web.py`（`agent_profiles` 脱敏保存、`team` 配置序列化、`pending_git_changes` 导出、可选人工提交端点）；`SettingsView.vue`（角色预设池）；`Dialogs.vue`（项目小组自由编排器、任务指派与依赖配置）；`DashboardView.vue`/`ProjectsView.vue`/`UsageView.vue`（角色与依赖角标、待人工提交提示、分角色用量） | 3.5 人日 | Web API roundtrip 测试全过；前端打包与本地浏览器交互验证无误 |
| **M4：联调优化、Prompt 调优与文档** | 真实多模型联调压测（验证 Python+Vue 项目拆解→编码→CDP 浏览器联调完整流程）、`config.example.yaml` 与 `用户指南.md`/`user-guide.md` 同步更新 | 2.0 人日 | 真实本地项目从需求下发到 QA 验收全自动闭环通过，主分支保持未提交态待人工把关 |
| **合计** | | **13.5 人日** | |

---

## 8. 自动化测试验证方案

沿用 `tests/` 独立脚本风格，需新增以下测试套件并确保原有全部测试无回归：

1. **`tests/test_team_config.py`**：
   - `agent_profiles` 加载、保存、H2 密钥脱敏掩码与三态保持；
   - `ProjectConfig.team` 自由组合（4 角色全配 / 缺省 PM / 缺省前端 / 自定义角色）解析与序列化 roundtrip；
   - 传统单兵项目与小组模式项目的向后兼容验证。
2. **`tests/test_task_dependencies.py`**：
   - `depends_on` 解析、`done/` 目录完成状态判定、`waiting` / `ready` / `upstream_blocked` 准确识别；
   - `tool_create_tasks` 的 `depends_on_refs` 批内索引自动翻译；
   - 循环依赖与不存在 ID 的强拦截校验。
3. **`tests/e2e_project_team.py`**（基于 `FakeLLMClient` 脚本化多角色协同）：
   - **剧本 1（完整流水线 + 强制 Worktree 隔离 + 主分支不默认提交）**：默认 `commit_and_merge=False` 下启用 `team.enabled=True`，验证写任务强制走独立 worktree 隔离、后端合入 `stage` 后前端能看到后端文件、增量安全落地主分支工作区且 **`main` 分支 `HEAD` 保持原位零自动 commit/push**，以及单周期内链式完成「PM 拆 → 后端 → 前端 → QA 联调通过归档」。
   - **剧本 2（Bug 发现与自动回归闭环）**：QA 联调调用 `report_bug` 新建 1 个前端 Bug + 1 个后端 Bug 并 `wait_retest` → 原 QA 任务自动挂靠 2 个 Bug 依赖且暂不重试 → 前后端分别修复后，调度器自动唤醒 QA 第 2 轮复测通过并归档；同时验证 `allow_test_scripts=True` 时 QA 可写入测试脚本提交，而 `allow_test_scripts=False` 时写工具被拦截。
   - **剧本 3（防死锁熔断 & 人工提交同步）**：QA 连续发现 Bug 达到 `max_qa_rounds=2` 上限后自动转 `blocked`；PM 拆分深度达到 `max_split_depth=1` 后禁止再拆回自己；模拟人工在主分支执行 `git commit` 后，`orcsy/stage/<project>` 自动同步至最新 `main` HEAD。
   - **剧本 4（自由组合降级）**：仅配置 `[pm, backend]` 时，强行派发 `frontend` 任务被拒绝重派；无 PM 仅 `[backend, frontend]` 时走 `fallback_role` 正常推进。
4. **`tests/test_team_web_api.py`**：
   - `/api/state` 与 `/api/config`、`/api/project/save` 针对 `agent_profiles` 与 `project.team` 的完整脱敏、三态密钥及往返不丢字段断言；`toggleProject` 不冲掉 `team` 配置断言；`pending_git_changes` 统计与 `/api/project/commit` 人工提交端点校验。

---

## 9. 已定案决策摘要（Review Resolved）

**无遗留待确认项**——全部核心产品与技术决策已终审定案，方案可直接按 **M1 → M4** 顺序启动开发：
1. **Q1（配置分层）**：采纳全局 `agent_profiles` 模板池 + 项目 `team` 自由引用/覆盖双层架构。
2. **Q2（QA 代码权限）**：默认只读禁改业务源码（`produces_code=false`），同时提供默认关闭的「允许编写自动化测试脚本（`allow_test_scripts`）」开关。
3. **Q3（Worktree 隔离 + 人工把关最终提交）**：处于项目小组模式下，**强制开启按任务隔离的 Git Worktree 沙盒模式**；所有写代码任务经 Review 后先合入内部集成分支 `orcsy/stage/<project>` 再增量安全落地到主分支工作区（呈未提交改动状态），**默认绝不在主分支自动执行 `git commit` 与 `git push`**，最终提交这一步必须由人工检查把关确认；只读角色免建 worktree 且豁免空提交拦截。
4. **Q4（同周期连续推进）**：**默认开启 `chain_within_cycle = true`**（配 `max_chain_steps_per_cycle = 10` 防失控上限），实现单周期内多阶段自动衔接交付。

---

## 10. 后续演进规划

关于跨任务长期记忆、架构黑板报、会话瞬时剪贴板与历史经验 RAG 召回等长远演进方向，详见未来路线图文档：[roadmap.md](roadmap.md)。

