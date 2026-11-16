# 任务指纹、租约与断点续跑设计（V1-R2）

> 版本 V1.0 ｜ 2026-11-16 ｜ 关联：`docs/plan-v1-trusted-pipeline.md` §4.4
> 状态：已实施（分支 `feat/trusted-pipeline-r2`）

---

## 1 目标

在 R1「run 留痕」之上解决三件事：

1. **不重复派发**：同一任务在扫描/重启/并发窗口内只被一个执行者认领；
2. **提交前可校验**：任务内容或被占用状态在运行中变化时，绝不落提交/合并；
3. **崩溃可续跑**：WIP 提交或合并不丢，恢复后不从头重放、不重复提交。

非目标：多机分布式锁（单实例共享状态文件即可）、任务内容版本管理（Git 已有）。

---

## 2 指纹（Fingerprint）

```
sha256(JSON{
  meta:   frontmatter 中非系统字段（title/priority/labels/assignee/depends_on/...）,
  body:   正文（去掉「执行进度 / Agent 处理结果 / 审查结果」等系统追加节）,
  deps:   [[dep_id, dep_fingerprint], ...]（排序，递归 depth ≤ 8）,
  guide:  sha256(AGENT.md 规范化内容),
  model:  生效模型 id,
  agent:  执行角色标识（role；单兵为空）
})
```

**刻意排除**：`status / order / requeue_seq / review_pending / review_round / qa_round / created_by / resume_run / budget / error`。

这些字段由系统频繁改写（标 in_progress、追加进度、审查轮次），若计入指纹会导致每次运行都「内容已变化」，租约和二次校验失去意义。用户改正文、依赖、验收标准、AGENT.md 或换模型/角色才会改变指纹。

> Progress 节等系统内容只影响「执行证据」，不影响「任务意图」，因此不进指纹。

---

## 3 状态文件

路径：`<配置目录>/state/<project>.json`（与 `config.yaml` 同级，可由 `ORCSY_DATA_DIR` 整体迁移）。

```json
{
  "schema_version": 1,
  "project": "app1",
  "leases": {
    "T-101": {
      "claim_id": "9f2c…",
      "fingerprint": "sha256…",
      "owner": "pid:1234@host",
      "acquired_at": 1763200000.0,
      "heartbeat_at": 1763200030.0,
      "ttl": 900.0
    }
  },
  "resumes": {
    "T-102": {
      "stage": "review|merged",
      "wip_commit": "a1b2c3d",
      "fingerprint": "sha256…",
      "updated_at": 1763200100.0
    }
  }
}
```

- 读改写：进程内 `RLock` + 跨进程文件锁（Windows `msvcrt.locking` / POSIX `fcntl`）+ 临时文件原子替换；
- 所有方法抛出 I/O 异常时由调用方降级：调度器「宁可不跑」也不在无租约状态下执行。

---

## 4 租约语义

| 项 | 规则 |
| --- | --- |
| 获取 | `acquire(task_id, fingerprint)`：无租约或已过期 → 生成 `claim_id` 写入；否则返回 `None` |
| 排他 | 即使同一进程，已有活跃租约也不能二次获取（避免并发波次重复领取） |
| 心跳 | 执行线程每 30s 更新 `heartbeat_at`；心跳失败停止，TTL 自然过期 |
| TTL | 默认 900s；过期租约可被下一次扫描回收 |
| 校验 | `verify(task_id, fingerprint, claim_id)`：租约存在 + claim 匹配 + 未过期 + 指纹一致 |
| 释放 | 任务回写完成后释放；异常路径靠 TTL 兜底 |
| 清理 | 进程启动时清理过期租约，不触碰续跑标记 |

`claim_id` 挂在内存中的 `Task` 对象（`task.claim_id`），不落任务文件，不进 Git。

---

## 5 执行流程

```
scan_tasks → 依赖/角色筛选 → wave
  ├─ 若 resumes[task].stage == "merged" → 直接归档（零模型调用，不重复提交）
  ├─ fingerprint(task, model, agent) → acquire → 失败则跳过本轮
  ├─ 标 in_progress，启动心跳线程
  ├─ 执行 Agent 循环
  ├─ 提交前 verify（指纹/claim/TTL）→ 失败：blocked + stale_lease，不提交
  ├─ WIP 提交成功 → resumes[task] = {stage: "review", wip_commit, fingerprint}
  ├─ 审查通过并合入 → resumes[task] = {stage: "merged", ...}
  ├─ 回写任务状态 + 归档
  └─ 清 resumes + 释放租约（心跳停止）
```

### 5.1 提交前二次校验

- 位置：`TaskExecutor._complete_with_review` 的 `git commit` 之前，以及工作区模式（非 worktree/无自动提交）落地前；
- 失败动作：不 commit、不 merge，`result.outcome=blocked`，进度写 `stale_lease`，worktree 保留；
- 失败原因：`lease_missing / lease_owner_changed / lease_expired / fingerprint_changed`；
- run 台账 `exit_reason=stale_lease`，便于统计「任务被改写导致的阻断」。

### 5.2 续跑阶段

| stage | 含义 | 恢复动作 |
| --- | --- | --- |
| `review` | WIP 提交已落，审查/合并未完成 | 任务置 open + `resume_run: true`；worktree 保留；下次执行跳过 reset，提示模型检查现状后继续；分支领先基线时直接补审 |
| `merged` | 合并已完成，状态未回写 | 恢复扫描时直接归档为 done，不再调用模型、不再提交 |

`resume_run` 是任务 frontmatter 的临时标记（系统字段，不参与指纹），回写完成后清除。

---

## 6 启动恢复决策

| 任务残留状态 | 租约状态 | 动作 |
| --- | --- | --- |
| `in_progress`，无 `review_pending` | 有 `resume`（review） | open + `resume_run=true`，保留 worktree |
| `in_progress`，无 `review_pending` | 有 `resume`（merged） | open + `resume_run=true`，下次扫描直接归档 |
| `in_progress`，无 `review_pending` | 无 resume | open，worktree 未提交改动下次 reset 丢弃（原有行为） |
| `in_progress` + `review_pending` | 任意 | 跳过，交给补审扫描只重跑审查 |
| 非 in_progress | 任意 | 释放残留租约，不动任务 |

补审前额外校验：若 `resumes[task].fingerprint` 存在且与当前任务指纹不一致 → 阻断合并，置 blocked（stale_lease）。

---

## 7 并发与降级

- 项目内多线程波次：每个任务独立租约；心跳线程按任务隔离；
- 看板/CLI 多进程共享同一状态文件，文件锁保证「只有一个 claim 成功」；
- 状态文件写失败：`acquire` 抛异常，调度器记录日志并跳过该任务（安全优先），下一轮重试；
- 心跳失败：停止续租，租约在 TTL 后回收，不会永久锁死；
- 任务指纹读取失败（文件被删/编码异常）：按未认领处理，跳过本轮。

---

## 8 兼容性

- 旧任务文件：无 `resume_run`、无依赖指纹 → 默认值，行为不变；
- 状态文件缺失：按空状态启动，首次扫描正常认领；
- 旧版本降级：不认识 `state/` 与 `resume_run`，任务文件仍可执行（resume_run 被忽略）；租约文件可保留，升级后继用；
- 指纹包含模型/角色：切换模型或改派角色会视为「任务已变化」，运行中的提交会被阻断，需要重新入队（防止用错模型产出被误合并）。

---

## 9 测试

`tests/test_claims.py`：

1. 指纹稳定性：状态/进度/审查标记不敏感；正文/依赖/AGENT.md/模型/角色敏感；
2. 租约独占、指纹绑定、释放重领；
3. 10 线程并发认领仅 1 个成功；
4. TTL 过期回收 + 心跳续租；
5. 续跑标记 + 启动恢复（保留 / 普通两条路径）；
6. 提交前二次校验（正文变化、租约丢失）；
7. 真实 ProjectAgent 扫描：1 次执行 + 100 次空扫描（模型调用不重复、租约释放）；
8. 执行中任务被改写 → `stale_lease` 阻断；
9. 待审任务被改写 → 补审阻断；
10. 合并后崩溃恢复 → 直接归档、零模型调用。

---

## 10 已知边界

1. **WIP 提交前崩溃**：没有提交可续，worktree 未提交改动按原行为丢弃，任务从头重跑（不产生重复提交）；
2. **合并成功到写入 merged 断点之间的极短窗口**：若恰在此窗口被杀，可能重跑；概率极低，后续可用 WAL 细化；
3. **多机共享状态文件**需要共享文件系统支持字节锁；网络盘锁语义不保证，官方仅支持单机/单实例；
4. 指纹不含 `budget` 等执行参数：修改预算不会阻断已开始的运行（R3 预算硬闸负责运行中限额）。
