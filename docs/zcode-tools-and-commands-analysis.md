# ZCode 源码解析：LLM 工具系统 与 斜杠命令系统

> 基线：`main @ a60ba6a7`（与 origin/main 同步，2026-11-03 分析）。
> 所有路径均给出 `file:line` 引用，可直接点击跳转。分析基于源码实证（Explore 子代理 + 人工抽查核对）。

---

# 第一部分 项目全景

ZCode 是 pnpm monorepo，两大功能模块的主体实现都在 Agent CLI 侧：

| 位置 | 职责 |
|---|---|
| `apps/zcode-cli/packages/core/src/tool/` | **LLM 工具系统核心**：类型、注册表、调度器、执行器、权限流、handler |
| `apps/zcode-cli/packages/cli/src/command-center/` | **斜杠命令**：解析（`slash-commands.ts`）与分发（`create.ts`） |
| `apps/zcode-cli/packages/bootstrap/src/` | 自定义命令发现、prompt resolver（命令展开）、协议命令目录装配 |
| `apps/zcode-cli/packages/contracts/src/` | 工具契约（zod schema）与会话事件定义（事件真名） |
| `packages/shared/src/zcode-protocol/`、`zcode-protocol-v4/` | 跨进程协议（v3 旧链路 + v4 现行主链路） |
| `packages/services/src/zcode-agent/` | stdio 传输、进程管理、事件流分发（Desktop 宿主侧） |
| `packages/ui/` | 工具卡渲染、权限弹窗、`/` 命令面板 |

总体分层：

```
模型(LLM) ⇄ CLI 运行时(apps/zcode-cli, 工具系统+命令展开)
              │ NDJSON over stdio
          Desktop Host (utilityProcess, packages/services)
              │ MessagePort + ChannelServer
          Renderer (packages/ui + packages/web, 共用 @zcode/ui)
```

---

# 第二部分 LLM 工具系统

## 2.1 全景：一条 tool_use 的完整生命周期

```
LLM 流式输出 tool_use
  │
  ▼
StreamingToolCoordinator (runtime/methods/streaming-tool-coordinator.ts:49)
  · DeferredToolCall 就地准入解包 (tool/deferred-call.ts:15)
  · 只读工具允许 during-stream 提前执行 (streaming-tool-coordinator.ts:37, mode="readOnly")
  │
  ▼
executeToolCallsForModelStep (runtime/methods/turn-tools.ts:36)
  · 建 part、persistPendingToolPart、emitStreamingToolLedgerUpdate
  │
  ▼
ToolScheduler.schedule (tool/scheduler.ts:60)
  · 拓扑排序(105) → 依赖分层分组(154)
  · 并行判定 canRunInParallel(85)：destructive 不并行；concurrentSafe/只读/无副作用可并行
  · 默认并发 10 (scheduler.ts:48)
  │
  ▼
ToolExecutorImpl / batch-runner (tool/executor/impl.ts:25, batch-runner.ts:58)
  · 按组 yield batch_start/batch_complete；stopTurnOnSuccess 取消后续组(96)
  │
  ▼  单调用 executeToolCallImpl (tool/executor/call-runner.ts:112)
  │   1. registry 查找 + 别名归一 (99)          miss → <tool_use_error> (149)
  │   2. 输入归一化 prepareToolExecutionInput (tool/input-normalization.ts:42)
  │   3. schema 校验 (executor/validation.ts:64)
  │   4. entry.validateInput → entry.resolveInput (语义校验 + 归一化成执行事实)
  │   5. PreToolUse hooks (executor/hook-flow.ts, call-runner.ts:332)  ← 可 deny/改输入
  │   6. 权限检查 resolveToolPermission (executor/permission-flow.ts:134) ← hook 后、handler 前
  │   7. emitToolCallStarted (453)
  │   8. 超时预算 ToolDeadline(可暂停, executor/timeout.ts:10) → executeWithTimeout (575)
  │   9. validateOutput (584)；ToolHandlerFailure 转异常
  │   10. serializeOutput 结果序列化 (executor/result-serialization.ts:38)
  │   11. PostToolUse hooks (604)
  │   12. createToolResultDisplay UI 投影 (executor/result-display.ts:175)
  │   13. emitToolCallResult (executor/events.ts:51)
  ▼
settleToolResults (runtime/methods/tool-result-settlement.ts:39)
  · 顺序不变量：真实结果先同步进两份模型历史(56) → part 持久化 → StreamRecoveryAnchor
    → hook 附件/批次提示(236)——中断时也要保证「结果→附件→中断标记」顺序
  │
  ▼
下一轮模型请求：tool_result entry + buildToolExposure 重新决策工具面
  (runtime/helpers/runtime-provider-request-messages.ts:42)
```

## 2.2 核心数据模型（`tool/types.ts`）

| 类型 | 位置 | 要点 |
|---|---|---|
| `ToolMetadata` | `tool/types.ts:71` | 模型可见性与安全性声明：`name/description/modelInstructions`、`readOnly/destructive/concurrentSafe`、`sideEffectScope`、`riskLevel`、`needsApproval`、`providerVisible`（false 则不发 provider，`registry.ts:106`）、`shouldDefer`（进 ToolSearch 目录）、`stopTurnOnSuccess`（终态工具） |
| `ToolEntry` | `tool/types.ts:315` | 完整工具定义 = metadata + `handler` + `inputSchema`/`runtimeInputSchema`(zod) + `resolveInput` + `validateInput` + `applyInputAdjustments`（用户确认窗调参）+ `resolvePermissionCapability/RulePolicy` + `resolveModelContract`（按模型投影描述/schema）+ `resolveTimeoutBudgetMs` + `aliases` + `resultBudget/timeout/cancellation` |
| `ToolExecutionContext` | `tool/types.ts:141` | handler 运行时上下文：宿主端口（fileSystem/http/subagent/skill/workflow…）、`readFileState`、`workingDirectory/workspaceRoot`、`runtimeScope: "main"\|"subagent"` |
| `ToolExecutionResult` | `tool/types.ts:475` | 分四路输出：`modelContent`（给模型）、`display`（给 UI）、`serialization`（给持久化）、`hookAdditionalContexts` |
| `ToolHandlerFailure` | `tool/types.ts:255` | `{result:false, errorCode, message}`——可预期业务失败用返回值表达，call-runner 统一转异常流（`call-runner.ts:584`） |

**双层校验**：`inputSchema` 是手写 JSON Schema 常量（`tool/json-schema.ts` 是校验器不是生成器，`json-schema.ts:33`），`runtimeInputSchema` 是 zod（负责默认值/preprocess/约束）。schema 按工具定义在 `packages/contracts/src/tools/<name>.ts`。

**模型可见内容**：`registry.ts:104-129 toContracts()` 过滤 `providerVisible:false`，描述拼接规则 `description + "\n\nUsage:\n- <modelInstructions>"`（`registry.ts:132`）。最终排序边界在 `runtime/methods/config.ts:280`，规则见 `tool/provider-visible-order.ts:41`：名单内 33 个工具按 en-US localeCompare 固定排序（防缓存漂移）→ 名单外本地工具按注册序 → MCP 工具按名排最后。

## 2.3 内置工具清单

总注册表：`apps/zcode-cli/packages/core/src/tool/handlers/index.ts:81` 的 `builtInTools` 数组（已人工核对）。

| 类别 | 工具 | 实现 | 说明 |
|---|---|---|---|
| 发现 | ToolSearch / DeferredToolCall | `handlers/tool-search.ts:22` / `deferred-tool-call.ts:14` | 惰性工具目录检索 / 解包调用 |
| 文件 | Read | `handlers/read.ts:569` | 文本分页/图片/PDF/视频，写 readFileState 快照 |
| 文件 | Write | `handlers/write.ts:216` | 写文件（要求先 Read） |
| 文件 | Edit | `handlers/edit.ts:258` | 多策略字符串替换 + 陈旧检测 |
| 文件 | Glob / Grep | `handlers/glob.ts:87` / `grep.ts:143` | 文件名匹配 / ripgrep 内容搜索（embedded-search 分支下不注册，由 Bash find/grep 接管，`index.ts:227`） |
| 执行 | Bash | `handlers/bash.ts:488` | shell 执行；超时策略、后台任务、只读命令白名单（`bash-readonly-policy*.ts`） |
| 网络 | WebFetch / WebSearch | `handlers/webfetch.ts:202` / `websearch.ts:118` | 抓取转 markdown（预批准域名免确认）/ 搜索（provider 别名 `web_search`） |
| 会话 | TodoRead / TodoWrite | `handlers/todo.ts:78,136` | 任务清单 |
| 会话 | AskUserQuestion | `handlers/ask-user-question.ts:76` | 向用户提问（选择题） |
| 会话 | Skill | `handlers/skill.ts:101` | 加载 SKILL.md 指令 |
| 会话 | ReadSessionContext | `handlers/read-session-context.ts:164` | 历史/交接会话检索 |
| 协作 | Agent / Task | `handlers/agent.ts:236,302` | 子代理派发（Task 为兼容别名，`tool/compat.ts:1`） |
| 协作 | SendMessage / RespondToCoordinator | `handlers/send-message.ts:81` / `respond-to-coordinator.ts:56` | 主↔子代理通信 |
| 工作流 | submit_result / escalate | `handlers/submit-result.ts:82` / `escalate.ts:99` | workflow actor 终态提交（`stopTurnOnSuccess`）/ 阻塞升级 |
| 工作流 | CreateWorkflow / AmendWorkflow / SaveWorkflow / ListSavedWorkflows / EvalWorkflowSnippet | `handlers/create-workflow.ts:372` 等 | 动态工作流 CRUD 与试跑 |
| 工作流 | ListWorkflowRuns / GetWorkflowRun / ResumeWorkflowRun | `handlers/list-workflow-runs.ts:138` 等 | run 内省与恢复 |
| 工作流 | ResolveWorkflowQuestion / FillWorkflowHole | `handlers/resolve-workflow-question.ts:136` / `fill-workflow-hole.ts:304` | 升级问答 / 留白补全（主代理侧） |
| 工作流 | ListModels | `handlers/list-models.ts:134` | 模型目录（供 `subagent_model`） |
| 任务 | TaskOutput / TaskStop | `handlers/task-output.ts:92` / `task-stop.ts:105` | 后台任务输出/停止 |
| 自动化 | CronCreate/List/Update/Delete/PauseSelf | `handlers/cron.ts:227,295,341,394,435` | 定时任务（automation 轮内防自指，`cron.ts:44`） |
| 自动化 | OffPeakCreate / OffPeakList | `handlers/off-peak.ts:187,239` | 闲时任务 |
| 计划 | EnterPlanMode / ExitPlanMode | `handlers/plan-mode.ts:111,160` | 计划模式 |
| REPL | js (node_repl) | `handlers/node-repl.ts:369` | Node REPL，默认关、由 browser-use 插件启用（`index.ts:285`） |
| 未注册 | ApplyPatch / ReplyToChannel / Workflow | `handlers/apply-patch.ts:328` 等 | 实现保留但注册被注释（`index.ts:87,117,152`） |

**门控注册**：`registerBuiltInTools`（`index.ts:217`）按端口存在性/灰度开关过滤：`includeSkill=Boolean(skillPort)`、`includeAgent=Boolean(subagentPort)`、`includeSubmitResult/includeEscalate`（workflow 端口）、`includeDynamicWorkflow`（灰度名单 `index.ts:163-175`）等；开关值由装配点 `runtime/helpers/runtime-tools.ts:47` 传入。分支工厂 `resolveBuiltInToolEntryForBranch`（`index.ts:294`）替换工具的分支版本（如 Bash 按 timeoutPolicy/embeddedSearch）。子代理另有结构性禁用名单 `runtime/helpers/tool-allowlist.ts:30`（如 CreateWorkflow 的 alwaysAsk 在 child 无窗可弹会隐形挂起）。

## 2.4 注册表机制（`tool/registry.ts`）

`ToolRegistryImpl`（`registry.ts:30`）维护两张 Map：`tools`（canonical 名→ToolEntry）与 `aliases`（别名→canonical）。三条不变式：

1. **canonical 永远优先于 alias**——后注册同名工具会清掉占位 alias 并告警（`registry.ts:34`）；
2. **alias 冲突拒绝新 alias**（`registry.ts:56`）——防止一次调用被路由到错误 handler/权限；
3. `get()` 先查 alias 再查 canonical（`registry.ts:88`）；`unregister` 支持 MCP 热刷新。

## 2.5 动态工具并入

| 来源 | 机制 | 关键代码 |
|---|---|---|
| **MCP 服务器** | `initializeMcp` 为每个 `McpToolDescriptor` 注册 `mcp__server__tool`；风险/预算按 readOnlyHint/destructiveHint 分档；回合边界热刷新（签名对比 → unregister 旧 + register 新） | `runtime/methods/mcp.ts:140,222`、`mcp/index.ts:76,122` |
| **MCP Apps 页面工具** | `app__<server>__<tool>`，handler 经实例信箱投递给沙箱页面；非只读强制 needsApproval、risk ≥ medium | `mcp/app-tools.ts:47` |
| **插件工具** | 与 MCP 同路，server 名经 `pluginIdByServerName`（session 冻结的 PluginReferenceCatalog）投影 pluginId；官方 CUA 经不可伪造 authority 门投影 `mcp__computer-use__*` | `mcp.ts:200`、`mcp/index.ts:57` |
| **惰性（deferred）工具** | 每次 provider 请求前 `buildToolExposure` 决定哪些工具发 schema、哪些只进 ToolSearch 目录（MCP 工具与 `shouldDefer` 工具默认 deferred）；模型发 `DeferredToolCall{name,input}` 就地解包成目标工具调用 | `tool/search/exposure.ts:101,37`、`tool/deferred-call.ts:15`、检索打分 `tool/search/search.ts:25` |

Deferred 的关键不变式：只接受 catalog 中标记 deferred 的目标；失败保持包装身份、不触发目标工具生命周期；调度前（`call-runner.ts:80`）与流式准入（`streaming-tool-coordinator.ts:57`）共用同一闸门。

## 2.6 权限与 Hooks（时序）

```
PreToolUse hooks ──deny/改输入──▶ (改写后重新归一化+复验)
        │
        ▼  权限检查 (permission-flow.ts:134, 循环直至定论)
  ① 静态判定 checkStaticToolPermission(103)：mode/planEnabled/riskLevel/项目规则/
     resolvePermissionRulePolicy/RuntimePermissionCapability
  ② hook permission 行为叠加 + memory 文件写权限(memory-file-permission.ts)
  ③ allow → 放行 │ deny → emitPermissionDenied (223)
  ④ ask → resolveToolApproval(approval-gate.ts, prepareApproval 只能窄化)
        → PermissionRequested 事件 + broker 竞速(permission-request.ts:20)
        → hook 可应答 modify → 改写后重判/重写循环(286,368)
  ⑤ 批准的 permissionUpdates 落盘 + applyPermissionInputAdjustments(385)
        │
        ▼
     handler 执行
```

- 缺省 broker 是 `DenyPermissionBroker`（fail-closed，`permission/broker.ts`）；guarded 模式下单次确认不可被预览覆盖（`permission-flow.ts:247`）。
- **顺序契约**：权限检查在 PreToolUse hook 之后、handler 之前（`call-runner.ts:405`）。PostToolUse hook 的文本不进 tool_result，而作为 `tool_hook_context` 附件（`call-runner.ts:604`）。

## 2.7 安全与正确性机制

| 机制 | 实现 | 关键规则 |
|---|---|---|
| **路径边界** | `tool/path-policy.ts:15` | 相对路径按 workingDirectory 解析；当前版本**刻意不硬挡** workspaceRoot 之外（注释 :36：subagent 可能要读同级 repo，等 FS 适配器长出 ask/deny 规则再收紧）——真正的越界防护在权限层（Read 静态判定 `areReadsDeniedWithoutPrompt`，`executor/permission-flow.ts:55`）与各 handler |
| **路径归一** | `tool/path-normalization.ts:15` | Windows/Git Bash `/c/...`→`C:\...`、盘符大写、剥 `\\?\`、NFC——readFileState key 与权限规则匹配的统一比较形 |
| **输入归一** | `tool/input-normalization.ts:42` | 顶层 JSON 字符串尝试 `JSON.parse`（adapter/hook/broker 可能传入字符串，失败降级为可恢复校验错误）；再 zod safeParse。三个来源 `initial/hook/permission` |
| **Read/Edit 陈旧检测** | `tool/read-file-state.ts` + `handlers/edit.ts:422` | 每文件一条快照（Map 顺序=写入顺序，修复注释 :19）；没读过/只读过部分 → `FILE_NOT_READ`；mtime/revisionId/content 三重判定不一致 → `STALE_FILE`。快照持久化进 tool part metadata 供冷恢复（`read-file-state-metadata.ts:16`，缺 mtime/revision 的历史不能冒充已读 :70） |
| **Bash 超时** | `tool/bash-timeout-policy.ts:6` | 默认 120s / 上限 600s；`BASH_DEFAULT_TIMEOUT_MS`/`BASH_MAX_TIMEOUT_MS` 可调，max ≥ default；`min(input||default, max)` |
| **工具通用超时** | `executor/timeout.ts:10` | `ToolDeadline` 可暂停——工具内部模型请求排队时暂停计时 |
| **Edit 匹配** | `tool/edit-matchers.ts:41` | 8 级策略依次尝试：exact → quote_normalized（弯引号）→ 行号前缀剥离 → escape 归一 → `\uXXXX` 归一 → line_trimmed → 缩进弹性 → block_anchor（首尾锚点+中间相似度≥0.8）；多候选且不唯一 → `ambiguous` 拒绝；`replace_all` 禁用三种宽匹配；替换文本还原弯引号风格（:82） |
| **ApplyPatch** | `patch-parser.ts:13` / `patch-apply.ts:70` | `*** Begin Patch` 格式解析（含 heredoc 剥离）；`seekSequence` 4 级放宽：exact → trimEnd → trim → unicode 归一（弯引号/破折号/NBSP）；`*** End of File` 锚定尾 |
| **WebFetch 预批准** | `tool/webfetch-preapproved.ts:1` | 约 90 个文档站免确认 + 路径前缀表；`%2f/%5c/%2e` 百分号编码路径一律不放行（:109） |
| **结果预算** | `executor/result-serialization.ts:25` | 默认 `maxInlineBytes/maxModelBytes=100KB`（UTF-8 字节）；超限落盘 artifact，模型看到 `<persisted-output>` 信封（`result-persistence-format.ts:46`，预览 2000 字符）；错误文本中段截断保头尾（:69） |

## 2.8 跨进程事件链路（工具结果如何到达 UI）

仓库里有**三代并存的事件契约**，工具链路横跨其中：

```
CLI 运行时 (事件真名: contracts/src/events/session.events.ts)
  ToolCallScheduled/Started/Progress/Result/Error (L136-141)
  PermissionRequested/Resolved/Denied (L149-151)
  StreamingToolLedgerUpdated (L123, 流恢复台账 stream-recovery.events.ts:37)
     │ NDJSON over stdio（帧边界只认 LF，防 U+2028/2029 切坏半帧
     │   packages/services/src/zcode-agent/zcodeStdioTransport.ts:35,64）
     ▼
Desktop Host 进程 (Electron utilityProcess, desktopHostProcess.ts:163 fork)
  main spawn 窗口级 Host；Host 内 zcodeAgentProcessManager.ts:1172 spawn CLI
  (命令解析 :477：monorepo dist/zcode.cjs app-server --stdio / 打包态 resources/glm / 原生 binary)
     │ MessagePort + ChannelServer, channel "zcode-agent" (shared/channels.ts:120)
     │ host/index.ts:2636 exposeServicesOnMessagePort
     ▼
zcodeAgentService (packages/services/src/zcode-agent/zcodeAgentService.ts:1326)
  · session/event 校验→per-(workspaceKey,sessionId) emitter fan-out (2567,2285)
  · v4/conversation/frame 按 topic 分流 (2687)
  · 后台会话 streaming delta 1.5s/96 条合并 (zcodeSessionEventCoalescer.ts:7)
     ▼
Renderer (@zcode/ui)
  · v4 主链路: agentConversationTransport.ts:240 subscribeConversationV4
    → conversationProjectionStore.ts（手写原子外部 store，非 Zustand；
      三条铁律 :9：snapshot 整体替换、delta 仅区间衔接、断档即重订）
  · delta 按 rowId 原位 upsert（shared/zcode-protocol-v4/apply.ts:156），
    toolCallId 只用于二级分组（explore/execute/changes/cua 虚拟组）
     ▼
渲染: ConversationTimeline → ConversationAgentToolCallRow → toolCallRowAdapter
  (v4/toolCallRowAdapter.ts:84, STATUS_MAP :17)
  → resolveRenderer (ToolCallBlocks/resolveRenderer.ts:58) → ToolCallBlock
  diff 卡: renderers/edit.tsx (display.kind="file_diff")；MCP 卡: renderers/mcp.tsx
display 解析: v4/toolResultDisplay.ts:300 从五个候选位 strict 解析，失败退化为纯文本
  （协议层 toolOutputSchema 对 display 用 optional().catch()，解析失败只丢卡片不拒整帧
   —— toolDisplay.ts:156 注释记录了两次因 display strip 导致整帧被拒的事故根因）
```

**v4 协议关键结构**（`packages/shared/src/zcode-protocol-v4/`）：
- `ToolCallRow`（`rows.ts:206`）：`status = inputStreaming | pendingApproval | running | success | error | cancelled`，含 `toolCallId/approvalInteractionId/outputPreview`。
- `toolResultDisplaySchema`（`toolDisplay.ts:23`）：`file_diff`（filePath/additions/deletions/structuredPatch[]）、`task_output`、`mcp_tool`、`cua`（含截图）、`bash_output`、`local_agent_message` 等约 15 种 kind。
- 权限快照（`snapshot.ts:244`）：`permissionRequestPayloadSchema`（summary/detail/display/options: allowOnce|allowAlways|deny|custom）挂 snapshot `pendingInteractions`（:520）；`pendingApproval` 行通过 `approvalInteractionId` 与交互互指。
- 权限应答回程：UI `V4InteractionDialogs.tsx:327`（复用 `PermissionDialog.tsx`）→ `resolveInteraction` command → `v4/command` RPC。

旧 v3 链路（`packages/shared/src/zcode-protocol/index.ts:1739` `tool.updated` discriminated union：scheduled|started|progress|result|error|batch|raw）仍在服务旧 TaskChat 视图（`packages/ui/src/lib/zcodeSessionProjection.ts:820`）。

## 2.9 新增一个工具（扩展指南）

1. **契约**：`packages/contracts/src/tools/<name>.ts` 加工具名常量 + zod schema / JsonSchema（先例 `contracts/src/tools/task-output.ts:4`）。
2. **Handler**：`core/src/tool/handlers/<name>.ts` 导出 `ToolEntry`（或工厂），填 metadata（readOnly/destructive/concurrentSafe/sideEffectScope/riskLevel/needsApproval/shouldDefer…）、handler、双层 schema、resultBudget/timeout。
3. **注册**：加入 `handlers/index.ts:81` `builtInTools`；有门控则同步 `RegisterBuiltInToolsOptions`（:182）+ 过滤链（:226）+ 分支工厂（:294）+ 装配点 `runtime/helpers/runtime-tools.ts:50`。
4. **provider 排序**（可选）：名字加进 `tool/provider-visible-order.ts:3` 名单。
5. **并行**（可选）：只读工具加入 `tool/scheduler.ts:233` `READ_ONLY_TOOLS` 或在 metadata 声明 `sideEffectScope:"none"`。
6. **权限**（可选）：`permission/service.ts` 登记能力组/规则；确认窗建议项走 `resolvePermissionRulePolicy`（`tool/types.ts:427`）。
7. **UI 卡片**（可选）：`tool/executor/result-display.ts:175` 加 display 分支（协议侧 `zcode-protocol-v4/toolDisplay.ts` 同步加 kind）。
8. **惰性加载**（可选）：metadata 设 `shouldDefer: true` 自动进 ToolSearch 目录。
9. **治理**（如构成新模块）：仿 `tool/search/module.ts` 建 `module.ts` + `CONTRACT.md`，同步 `architecture-policy.yaml`。

---

# 第三部分 斜杠命令系统

## 3.1 三层架构

```
┌─ 层1: CLI/TUI command-center ──────────────────────────────┐
│ 输入 → parseSlashCommand (cli/src/command-center/slash-commands.ts:40)
│   ├─ known   → 本地即时处理 (create.ts:77 逐分支)：/help /login /model …
│   │            prompt 型命令原文透传 app.submitPrompt
│   └─ unknown → 先试自定义命令展开 (handlers/custom.ts:6)
│               失败且 "not found" → Unknown command + 命令列表
├─ 层2: bootstrap app facade prompt resolver ────────────────┐
│ 所有进入回合的输入 (TUI/headless/桌面 App) 统一经
│ create-app.ts:965 customCommandPromptResolver：
│   内置 prompt 命令 (/init /workflow) → 代码生成正文
│   自定义命令 → 展开成 prompt（facade 路径支持 !`cmd` shell 展开）
│ 展开后文本 = 真实输入；原文 /cmd args = displayInput（展示与历史用原文）
│   (bootstrap/src/app/input-facade.ts:116, core turn.ts:168)
└─ 层3: 协议命令目录（桌面/Web UI 的 / 面板数据源）────────────┐
│ bootstrap/src/zcode-protocol/slash-commands.ts:21
│ = 内置段(App 面板仅 goal/workflow/compact/init) + 自定义段(过滤保留名)
│ 随 session snapshot 与 workspace/readPresentation RPC 下发
└────────────────────────────────────────────────────────────┘
唯一「命令名权威表」: packages/shared/src/zcode-slash-command-help.ts:9
（help 文案、TUI 候选、协议目录、保留名表全部由它派生）
```

## 3.2 内置命令清单（权威表已核对）

`packages/shared/src/zcode-slash-command-help.ts:9-199`：

| 命令 | 别名 | 说明 |
|---|---|---|
| `/help` | | 本地打印命令帮助，不建会话不发模型 |
| `/login` / `/logout` | | Coding Plan 供应商登录 / 删除 Z.ai OAuth 凭据 |
| `/compact [instructions]` | | 手动压缩对话 |
| `/init [notes]` | | 检查工作区并创建/更新 AGENTS.md（prompt 型：生成完整提示词交模型执行） |
| `/expert [status\|resume\|stop\|<task>]` | | 持久化 expert 工作流 |
| `/effort` | `variant` | 查看/切换推理强度 |
| `/dwf` | | 列出/取消/恢复动态工作流 run |
| `/fork` | | 从 checkpoint 分叉新会话 |
| `/locale` | `language` | 查看/切换 UI 语言 |
| `/mcp` | | 查看/连接/断开 MCP 服务器 |
| `/plugins` | `plugin` | 插件管理 |
| `/mode` | | 权限模式 plan/build/edit/yolo/guarded |
| `/model` | | 查看/切换会话模型 |
| `/new` | `clear` | 开新会话 |
| `/resume` | `continue` | 恢复会话 |
| `/rewind` | | 查看/恢复 checkpoint |
| `/skill [name] [task]` | | 列技能 / 强制下条 prompt 先加载某技能 |
| `/goal` | `target` | 会话目标管理 |
| `/workflow` | | 加载 dynamic-workflows 技能并经 CreateWorkflow 启动（prompt 型，受灰度门） |

CLI 顶层子命令（非斜杠，`cli/src/run.ts:601` switch 分派）：`help/version/agent-server/app-server/doctor/login/logout/commands/plugin(s)/skills/tui`；另有隐藏子命令 `__zcode-plugin-host`（`plugin-host-command.ts:19`，agent 子进程内动态加载官方插件 MCP server）、`__zcode-dwf-child`（`dwf-child-command.ts:24`，工作流沙箱子进程，SEA 单文件二进制 re-exec 自身）。

## 3.3 解析与分发细节

**`parseSlashCommand`**（`cli/src/command-center/slash-commands.ts:40`）：非 `/` 开头返回 null；首个空白切出命令名（lower-case）与参数；别名折叠表（`variant→effort`:68、`language→locale`:142、`continue→resume`:151、`clear→new`:160、`plugin→plugins`:196、`target→goal`:214）；未识别返回 `{type:"unknown"}`；`/skill` 额外拆 `skillName/task`（:223）。

**`createCommandCenter`**（`cli/src/command-center/create.ts:39`）分派规则：

1. 非命令 → 直接 `app.submitPrompt`（:45）；
2. 带图片附件的命令输入拒绝（:57）；
3. **unknown → 先试自定义命令**（`handlers/custom.ts:6` → `buildCustomCommandPrompt`），加载失败且错误含 "not found" 才回落 Unknown command 文案 + 可用命令列表（create.ts:64）；
4. known → 逐 if 分支（:77-382）：
   - **纯本地**：help/login/logout/new/resume + mode/locale/mcp/plugins/model/goal/effort/dwf/expert/skill(list) 委托 `command-center/handlers/<name>.ts`；
   - **prompt 型（原文透传 facade）**：compact/init/workflow/rewind、`skill <name>`（重写为 manual skill prompt，`slash-commands.ts:252`）——都重新 `app.submitPrompt`，由层2展开；
5. 执行后写输入历史 `recordSlashCommandInHistory`（`history.ts:9`，含 API key 的 login 不记录）。

内置命令参数只按空白粗切（`command-center/utils.ts:1`）；自定义命令的 `$1/$2` 用**引号感知**切分（`custom-command-expand.ts:67`，支持 `\` 转义与单双引号）。注意 `cli/src/argument-parsing/` 与斜杠命令无关——那是 CLI 旗标解析的架构边界声明。

## 3.4 自定义命令：发现、优先级、frontmatter

**发现入口**：`bootstrap/src/custom-commands.ts:33 listZCodeCustomCommands` → `adapters/src/commands/index.ts:48 discoverCommands`。roots 按 priority 升序注册（`adapters/src/commands/roots.ts:21`）：

| 优先级 | 来源 |
|---|---|
| 10 | config 声明的 extraRoots |
| 20 / 30 | 用户级 `~/.zcode/commands` / `~/.agents/commands` |
| 40+ | 项目级：从 cwd 向上到 git worktree root 的每层 `.zcode/commands` + `.agents/commands` |
| — | additionalDirectories（带 namespace 前缀） |
| 1000+ | 插件命令 roots（`adapters/src/plugins/index.ts:109`；manifest 还可声明无文件的元数据命令） |

**遮蔽规则**：priority 升序扫描，同名**先到先赢**，后者记 `custom_command_duplicate_name` 警告（`commands/index.ts:61`）。即实际优先级 extraRoots > 用户级 > 项目级 > 附加目录 > 插件（测试 `adapters/tests/commands.test.ts:113` 证实用户级赢项目级）；同一层 `.zcode` 先于 `.agents`。config 里 `command.<path>.enable=false` 可禁用单个命令文件。

**命名与限额**：相对路径 `dir/file.md` → `/dir:file`；名字须匹配 `^[a-z0-9][a-z0-9_:-]{0,63}$`（`commands/index.ts:22`）；最深 12 层、文件上限 100KB 超限截断。

**frontmatter**（白名单 `SAFE_FRONTMATTER_KEYS`，`commands/index.ts:26`）：`allowed-tools`、`argument-hint`、`description`、`disable-noninteractive`、`model`、`skills`；未知 key 记 warning；`description` 缺省取正文第一行。注意 **`allowed-tools`/`model` 目前只是元数据透出**（`commands-command.ts:160` 展示），展开器不据此做权限裁剪。

**保留名 gate**（防自定义命令遮蔽内置）：`bootstrap/src/slash-command-surface.ts:18`——内置名+别名+`compress`/`plan` 构成保留名集合；facade 解析时保留名直接拒绝展开（`custom-command-prompt.ts:30`），协议目录同样过滤（`zcode-protocol/slash-commands.ts:44`）。

## 3.5 占位符展开（两条路径、一套 `$` 语义）

共享模板实现在 `packages/contracts/src/commands/index.ts:140`：

| 能力 | CLI/TUI 路径 | facade 路径 |
|---|---|---|
| 实现 | `cli/src/custom-command-expand.ts:22 expandCliCustomCommandPrompt` | `bootstrap/src/custom-command-prompt.ts:26 resolveZCodeCustomCommandPrompt` |
| `$ARGUMENTS` | 全量替换（:35） | 同 |
| `$1`/`$2`… | 位置参数（:36） | 同 |
| 无占位符 + 带参数 | 追加 `User arguments:\n<args>` 尾块（:41） | 同 |
| `skills` frontmatter | 生成"先调 Skill 工具"前言（:58） | 同 |
| `!`cmd`` shell 展开 | **直接抛错**（:26,107） | 有 `executionPort` 时支持：内联 + ` ```! ` 围栏逐段执行替换 stdout；超时 30s、输出 128KB；注入 `CLAUDE_*/ZCODE_*` 上下文变量；非零退出抛错（`custom-command-shell-expansion.ts:13`） |
| `@文件` 引用 | ❌ 不支持——那是 UI 输入框的 mention/附件机制（`packages/ui/src/SlashCommandPlugin.tsx`），与命令系统解耦 | 同 |

## 3.6 命令如何进入对话

**核心原则：命令不新建通道，展开成文本后注入用户消息。**

```
UI/TUI/headless 提交 "/cmd args"
        │
        ▼
inputFacade.runPromptTurn (bootstrap/src/app/input-facade.ts:116)
  customCommandPromptResolver (create-app.ts:965):
    ① 内置 prompt 命令:
       /init    → builtin-prompt-command.ts:79 生成完整 AGENTS.md 检查提示词
       /workflow→ builtin-workflow-command.ts:20 生成"先加载 dynamic-workflows
                  技能再写脚本调 CreateWorkflow"正文；命中还会激活 onDemand 工具面
                  (create-app.ts:977)
    ② 自定义命令 → 展开（含保留名拒绝）
        │
        ▼
  runtime.executeTurn({ input: 展开后文本, displayInput: "/cmd args" })
  (core/src/runtime/methods/turn.ts:168 —— 展示与历史用原文，模型看展开后 prompt)
        │
        ▼
  每回合开始以展开后 prompt 触发 UserPromptSubmit hooks
  (turn.ts:565；hook 可注入 additionalContext 或 preventContinuation 拦截)
  工作流通知驱动的自动回合跳过该 hook (runtime-command-queue.ts:312,407)
```

headless（`zcode -p`）路由（`cli/src/prompt-command.ts:302,484`）：只有 `expert/goal` 与解析不出的 unknown 进 command-center；能解析成自定义命令的一律原文走 `app.submitPrompt`（:290 注释给出三个原因：dwf 结算、事件单写者、多回合 response）。

## 3.7 UI 集成：目录下发与提交通道

**目录下发（/ 面板与自动补全数据源）**：

```
bootstrap/src/zcode-protocol/slash-commands.ts:21 listProtocolSlashCommands
  = 内置段(仅 goal/workflow/compact/init 上 App 面板, 顺序 slash-command-surface.ts:8)
  + 自定义段(过滤 disableNonInteractive 与保留名)
载荷 zcodeSlashCommandSchema {name, description, inputHint?, source: builtin|custom}
  (shared/zcode-protocol/index.ts:1387)
  ├─ 随 session snapshot (slashCommands 字段 :1441)
  └─ 随 workspace/readPresentation RPC (:2450, 方法 :4290)
        ▼
packages/services zcodeAgentService.ts:4773 readWorkspacePresentation
        ▼
packages/ui workspacePrepareRpc.ts:62 → store setSlashCommands
  (store/zcodeSessionStoreWorkspaceSlice.ts:788) → useSlashCommands.ts:8
        ▼
Lexical / 触发面板 SlashCommandPlugin.tsx；模糊过滤打分 lib/slashCommands.ts:39
插件命令变化增量合并 settings/pluginSlashCommandRefresh.ts:28
```

TUI 候选走另一条本地路径：`listSlashCommandSuggestions`（`cli/src/slash-commands.ts:272` = 内置 help 表 + 自定义命令建议，接线 `tui-command.ts:66`）。

**提交执行**——UI **不做本地展开**，把 `/cmd args` 原文当普通文本提交：

- v4 主路径：`agentConversationTransport.ts:389 sendCommand` → `sendConversationCommandV4`（`zcodeAgentService.ts:6626`）→ RPC `v4/command`（`zcode-protocol-v4/transport.ts:404`）→ `record.app.sendInput`；
- legacy 路径：RPC `session/send`（`zcode-protocol/index.ts:4268`）→ `sendPrompt`（忙时 `-32010` 拒绝，立即 ACK 后台执行，`server-operations.ts:2019`）。

两路最终都进 inputFacade 由 resolver 展开。

## 3.8 新增一个内置命令（扩展指南）

1. `packages/shared/src/zcode-slash-command-help.ts` 加 help entry——**一步同时进入** /help 输出、TUI 候选、协议目录、保留名表（`slash-command-surface.ts:18` 自动收集）；
2. `cli/src/command-center/slash-commands.ts` 加识别分支（含别名）+ `slash-command-types.ts` 加 known 类型变体；
3. `command-center/create.ts` 加分发分支；复杂逻辑新建 `command-center/handlers/<name>.ts`；headless 自动复用同一条 command-center 路径（`prompt-command.ts:524`）；
4. prompt 型命令（如 /init）：改 `bootstrap/src/builtin-prompt-command.ts:34`，TUI/headless 只需把原文透传；
5. 要上桌面 App 面板：名字加进 `slash-command-surface.ts:8` `APP_PROTOCOL_VISIBLE_BUILTIN_SLASH_COMMAND_NAMES`（UI 不维护排序白名单）；
6. 补测试：解析、分发、自定义命令遮蔽回归（`adapters/tests/commands.test.ts`）。

---

# 附录 A 关键文件索引

**工具系统**
- `apps/zcode-cli/packages/core/src/tool/types.ts` — 类型中枢（ToolMetadata/ToolEntry/Context/Result）
- `apps/zcode-cli/packages/core/src/tool/registry.ts` — 注册表 + provider 契约投影
- `apps/zcode-cli/packages/core/src/tool/handlers/index.ts` — builtInTools 总注册表 + 门控
- `apps/zcode-cli/packages/core/src/tool/scheduler.ts` — 拓扑排序并行调度
- `apps/zcode-cli/packages/core/src/tool/executor/{impl,batch-runner,call-runner,permission-flow,hook-flow,result-serialization,result-display,events}.ts` — 执行管线各阶段
- `apps/zcode-cli/packages/core/src/tool/{path-policy,path-normalization,input-normalization,read-file-state,edit-matchers,bash-timeout-policy,deferred-call}.ts` — 安全与正确性
- `apps/zcode-cli/packages/core/src/tool/search/` — ToolSearch 惰性曝光
- `apps/zcode-cli/packages/core/src/mcp/` — MCP/插件/App 工具注册
- `apps/zcode-cli/packages/core/src/permission/` — 权限服务与 broker
- `apps/zcode-cli/packages/contracts/src/events/session.events.ts` — 事件真名

**命令系统**
- `packages/shared/src/zcode-slash-command-help.ts` — 内置命令权威表
- `apps/zcode-cli/packages/cli/src/command-center/{slash-commands.ts,create.ts,slash-command-types.ts,handlers/}` — 解析与分发
- `apps/zcode-cli/packages/cli/src/custom-command-expand.ts` — TUI 侧展开（禁 shell）
- `apps/zcode-cli/packages/bootstrap/src/{custom-commands.ts,custom-command-prompt.ts,custom-command-shell-expansion.ts,slash-command-surface.ts,builtin-prompt-command.ts,builtin-workflow-command.ts}` — 发现/facade 展开/保留名/内置 prompt 命令
- `apps/zcode-cli/packages/adapters/src/commands/` — 自定义命令发现与 roots 优先级
- `apps/zcode-cli/packages/bootstrap/src/zcode-protocol/slash-commands.ts` — App 命令目录
- `packages/ui/src/SlashCommandPlugin.tsx` — Lexical / 面板

**跨进程链路**
- `packages/services/src/zcode-agent/{zcodeAgentProcessManager,zcodeStdioTransport,zcodeProtocolClient,zcodeAgentService}.ts`
- `packages/desktop/src/main/desktopHostProcess.ts`、`packages/desktop/src/host/index.ts`
- `packages/shared/src/zcode-protocol-v4/{rows,toolDisplay,snapshot,transport,apply}.ts`
- `packages/ui/src/v4/{agentConversationTransport,conversationProjectionStore,toolCallRowAdapter,toolResultDisplay,V4InteractionDialogs}.ts`

# 附录 B 容易踩的设计要点

1. **结果结算顺序不变量**：真实结果必须先同步进模型历史，再持久化 part、发 anchor、写附件——中断时也要保证「结果→附件→中断标记」顺序，否则模型只看到被截断正文（`tool-result-settlement.ts:56,234`）。
2. **v4 display 的 catch 语义**：`toolOutputSchema` 对 display 用 `optional().catch()`——display 解析失败只丢卡片不拒整帧；这是两次线上事故的沉淀（`toolDisplay.ts:156` 注释）。UI 侧解析同样五候选位容错退化（`toolResultDisplay.ts:300`）。
3. **v4 会话状态不是 Zustand**：`conversationProjectionStore` 是手写原子外部 store（snapshot 整体替换/delta 区间衔接/断档重订），Zustand store 承载旁路域（subagents/mcp 等）。
4. **UI 不展开命令**：桌面/Web 把 `/cmd` 原文提交，展开只发生在 CLI facade 层——保证 TUI/headless/App 三端展开语义一致（含 shell 展开、灰度门、保留名）。
5. **displayInput 分离**：历史与展示用原文 `/cmd args`，模型看展开后 prompt——排查"模型为什么这么做"时要看展开后文本。
6. **`allowed-tools` frontmatter 目前不裁剪权限**：只是元数据透出，别误以为它生效。
7. **path-policy 当前不硬挡 workspace 外路径**：防护在权限层与 handler 内，别误以为路径越界会被统一拦截。
8. **并行失败的批次语义**：并行组内某工具失败不再跳过后续组（旧策略已移除，`batch-runner.ts:134`）。
