# 预算硬闸设计（V1-R3）

> 版本 V1.0 ｜ 2026-11-16 ｜ 关联：`docs/plan-v1-trusted-pipeline.md` §4.5
> 状态：已实施（分支 `feat/trusted-pipeline-r3`）

---

## 1 目标

让无人值守运行有**成本上限**：

- 单任务 token / 成本超限 → 立即停止执行，不继续烧 token；
- 单项目当日 token 超限 → 暂停领取新任务（软停），避免一觉醒来账单翻倍；
- 超限是「可恢复的受阻」，不是失败：成果保留，调高预算或次日重新入队即可续跑。

非目标：按模型/按时间窗的复杂计费策略、跨项目总预算、账单对账。

---

## 2 配置

```yaml
budget:
  enabled: false                     # 默认关闭，升级行为不变
  max_tokens_per_task: 0             # prompt + completion；0 = 不限制
  max_tokens_per_project_per_day: 0  # 本地自然日；0 = 不限制
  max_cost_per_task_cny: 0           # 元；需 prices 配单价
  prices:                            # 每百万 tokens 的人民币单价
    deepseek-chat: {prompt: 1.0, completion: 2.0}
```

- 看板「设置 → 预算与成本」可改 enabled / 三个限额并即时生效；
- 单价表当前只在 `config.yaml` 维护（模型名精确匹配，找不到单价则成本按 0 计）；
- `save_config` 整体写回，看板任意保存不会丢单价表。

---

## 3 计量与存储

- `BudgetStore`：`<配置目录>/state/budget.json`，按项目保存**当日** prompt/completion/cost；
  - 跨进程文件锁 + 原子替换（与租约同一套机制）；
  - 日期变化自动归零；快照供看板展示。
- `BudgetTracker`：一次任务一个，子代理共享同一实例；
  - 每次模型调用后由 `LLMClient._acc_usage` 上报；
  - 记录先写 store 再判超限，保证超限那一次的消耗也计入；
  - `precheck()` 在每次 HTTP 请求前执行：已超限（含子代理触发）不再发请求；
  - 任务级计数在内存，进程重启后从 0 开始（当日项目累计仍在文件里）。

> 只统计 **开启预算之后** 产生的模型调用；开启当天的历史调用不会回填（usage.jsonl 有完整台账，但不做回填以避免重复计算）。

---

## 4 执行语义

| 触发 | 动作 |
| --- | --- |
| 单任务 token 超限 | `BudgetExceeded(scope=task)` → 停止工具循环，`outcome=blocked`，`block_reason=budget_exceeded` |
| 单任务成本超限 | 同上（仅当该模型配置了单价） |
| 项目当日超限（执行中） | `BudgetExceeded(scope=project)` → 当前任务 blocked；下一轮扫描软停 |
| 项目当日已超限（扫描前） | `scan_and_work` 直接返回 0，不领取新任务 |
| 看板展示 | 项目卡片「预算暂停」标签；设置页显示各项目今日 tokens/成本 |

超限后的任务：

- 不提交、不合并；worktree 保留（与审查失败同策略）；
- 任务文件 frontmatter 写 `block_reason: budget_exceeded`，正文进度写明确原因；
- run 台账 `exit_reason=budget_exceeded`；
- 重新入队时 `_mark_in_progress` 清除旧 `block_reason`。

---

## 5 代码接入点

| 位置 | 改动 |
| --- | --- |
| `orcsy/budget.py` | `BudgetStore` / `BudgetTracker` / `BudgetExceeded` / 模块级配置与单例 |
| `orcsy/llm.py` | `LLMClient.budget`；`_post` 前 precheck；`_acc_usage` 后 record |
| `orcsy/executor.py` | 每任务建 tracker；异常收敛为 blocked；review 共享 tracker |
| `orcsy/subagent.py` | 子代理继承父 tracker，超限由父循环停止 |
| `orcsy/agent.py` | 扫描前项目软停；回写 block_reason；清旧原因 |
| `orcsy/task_model.py` | `block_reason` 解析与 frontmatter 读写 |
| `orcsy/settings_service.py` / `web.py` | 分块保存、热生效、状态曝光、项目预算标签 |
| 前端 | 设置页「预算与成本」；项目卡片「预算暂停」 |

---

## 6 兼容与边界

- 默认 `enabled: false`：旧配置无需修改，升级后行为不变；
- 不新增任务状态值：仍用 `blocked` + `block_reason`，看板/旧任务/归档逻辑不受影响；
- 任务级计数重启即清零（同一次运行内的硬闸；跨重启的累计由项目日窗负责）；
- 审查 Agent 的 tokens 计入同一个任务 tracker（任务总成本包含审查）；
- 子代理异常被吞也无妨：tracker 的 `exceeded` 标记会让父循环在下一次请求前停止；
- 项目只有 **token** 日上限，没有成本日上限（单价因模型而异，避免误判）；
- 单价缺失时成本为 0：配置了 `max_cost_per_task_cny` 但没配单价不会生效。

---

## 7 测试

`tests/test_budget.py`：

1. 当日用量累计、跨日归零、快照；
2. 单任务 token 超限、成本超限；
3. 项目当日超限 + 软停判断；
4. `LLMClient` 用量上报与请求前拦截；
5. 项目预算用尽 → `scan_and_work` 零模型调用；
6. 执行器硬停 → `blocked + block_reason=budget_exceeded`；
7. `block_reason` frontmatter 写入/清除/解析；
8. 看板设置分块保存、热生效、非法值拒绝。

手工验收：设置极小上限跑一个任务，确认任务受阻、worktree 保留、项目卡片显示「预算暂停」、设置页能看到今日用量。
