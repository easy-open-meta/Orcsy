# 熔断停机设计（V1-R4）

> 版本 V1.0 ｜ 2026-11-16 ｜ 关联：`docs/plan-v1-trusted-pipeline.md` §4.6
> 状态：已实施（分支 `feat/trusted-pipeline-r4`）

---

## 1 目标

防止系统性故障「安静地跑一整夜」：模型 401 / 端点全挂 / 审查连续不通过时，
项目自动暂停领取新任务，看板给出可解释原因，由人决定何时恢复。

非目标：跨项目全局熔断、自动降级换模型、自动回滚（审查失败仍按 R1/R2 语义保留成果）。

---

## 2 配置

```yaml
resilience:
  enabled: true                     # 默认开启
  max_consecutive_failures: 3       # 0 = 不按连续失败触发
  window_minutes: 120               # 失败率统计窗口
  max_failure_rate: 0.6             # 0 = 不按失败率触发
  min_samples: 5                    # 窗口内最少样本数
  auto_resume: false                # true = 暂停满一个窗口后自动恢复一次
```

看板「设置 → 熔断与恢复」可调并即时生效；默认人工恢复，避免自动重启掩盖故障。

---

## 3 失败口径

| 结果 | 计入失败 | 说明 |
| --- | --- | --- |
| `done` | ❌（记成功） | 重置连续失败计数 |
| `open`（拆分/转派/等待回归） | ❌（中性） | 不改变连续计数 |
| `cancelled`（用户强制停止） | ❌（中性） | 不惩罚人工操作 |
| `blocked` + `budget_exceeded` | ❌（中性） | 预算受控停止 |
| `blocked` + `stale_lease` | ❌（中性） | 任务被改写，不是系统故障 |
| `blocked`（普通/审查 fail/llm_error） | ✅ | 系统性失败 |
| 未知结果 | ✅ | 宁可熔断也不静默跑一夜 |

- **连续失败**：从最新事件向前数 `fail`，遇到 `ok` 停止；`neutral` 跳过。
- **窗口失败率**：窗口内 `fail / (fail+ok)`；样本数 < `min_samples` 不判。

---

## 4 状态与流程

状态文件：`<配置目录>/state/resilience.json`

```json
{
  "schema_version": 1,
  "projects": {
    "demo": {
      "events": [{"ts": 1763200000, "kind": "fail", "task": "T-1",
                  "outcome": "blocked", "reason": "", "review": "fail"}],
      "paused": true,
      "pause_reason": "连续 3 次失败（阈值 3）",
      "paused_at": 1763200100,
      "paused_by": "auto",
      "consecutive_failures": 3
    }
  }
}
```

- 事件保留最近 200 条 / 7 天；
- 触发后写 `paused`，项目 `scan_and_work` 直接返回，不领取新任务；已领取的波次允许收尾；
- 人工恢复：看板项目卡片「恢复」或设置页暂停列表 → `POST /api/project/resume`；
  清空失败史并重新计数；
- 自动恢复：`auto_resume=true` 且暂停满 `window_minutes` 后，下一次状态检查自动解除
  并清空旧事件（若故障仍在，会再次熔断）。

---

## 5 代码接入点

| 位置 | 改动 |
| --- | --- |
| `orcsy/resilience.py` | `ResilienceStore`（事件/暂停/恢复/自动恢复）、模块级配置与单例 |
| `orcsy/agent.py` | 扫描前与本轮循环内检查暂停；任务回写后记录健康事件 |
| `orcsy/scheduler.py` / `web.py` | 初始化、热生效、`full_state` 曝光项目 `health` |
| `orcsy/settings_service.py` | `resilience` 分块保存与校验 |
| `POST /api/project/resume` | 人工恢复（记录日志，R5 起入审计） |
| 前端 | 设置页「熔断与恢复」；项目卡片「已熔断」+「恢复」按钮 |

---

## 6 测试

`tests/test_resilience.py`：

1. 连续失败触发；成功重置；预算/stale/取消不计入；
2. 窗口失败率达到阈值触发；窗口外旧事件忽略；
3. 人工恢复清空失败史；手动暂停；
4. auto_resume 窗口结束后自动恢复；
5. 熔断项目扫描软停（零模型调用）；
6. 设置保存/热生效、人工恢复 API、非法值拒绝。

手工验收：连续制造 3 次失败 → 项目卡片出现「已熔断」；点「恢复」后下一轮继续领取任务。

---

## 7 已知边界

- 熔断是项目级，不影响同一实例里的其它项目；
- 当前一轮已领取的并行任务不会被强制中止，只停止后续派发；
- `auto_resume` 只恢复一次/窗口，不跟踪「恢复后是否仍失败」的自适应退避；
- 事件只在任务回写完成后记录，进程在任务中途崩溃不会计入失败（由 R2 续跑处理）。
