# 合入前风险分级审批（Phase 0+1）设计与落地

> 状态：**Phase 0+1 已实现（全局 opt-in，单机单审批人）**｜2026-11-26
> 关联：[`roadmap.md`](roadmap.md) §8、[`design-release-quality-gaps.md`](design-release-quality-gaps.md) 阶段②、
> [`design-review-agent.md`](design-review-agent.md)

## 1. 定位与边界

Claude 四阶段的「低风险 0 人 / 中风险 1 人 / 高风险 2 人」是**策略层**，
其前提是真实身份、角色与审批配额。Orcsy 当前是单共享密码 + 全局 API Key 的
单机桌面应用，没有账号/RBAC/SSO，因此 Phase 1 只做**诚实可交付**的部分：

- **Phase 0**：本地风险评估（路径 + 少量内容信号），产出等级、原因、文件、
  增删行数与内容 digest；
- **Phase 1**：按配置对高风险（或显式配置的等级）在**合入前**要求看板人工批准；
  审批绑定 commit + diff digest；拒绝/超时/内容变化一律阻断，任务分支保留；
- 记录审计与运行台账，供事后追溯。

**明确不做**：

- 多人会签（quorum）、账号/RBAC、SSO/外部 IdP；
- 低风险自动合入的新策略（Phase 1 的 `off` 只是沿用现有审查/CI/合入流程）；
- 手工 `/api/project/commit` 的风险门（它提交的是已落地的主工作区改动）。

Phase 1 的 UI 与文档都标注「单机单审批人」；多人审批属于 Phase 2。

## 2. 风险评估

`orcsy/risk.py` 不调用模型，规则确定、可解释：

| 等级 | 规则示例 |
| --- | --- |
| high | `orcsy/updater.py`、`auth.py`、`masking.py`、`egress.py`、`tools_net.py`、`budget.py`、`usage.py`、`storage.py`、`approvals.py`、`risk*.py`；`build/**`、`installer.iss`、`.github/workflows/**`、`deploy/**`、`Dockerfile`、`docker-compose.yml`、`orcsy.spec`；依赖锁文件 |
| high（内容信号） | `orcsy/config.py` diff 含 `CONFIG_VERSION` / `_CONFIG_MIGRATIONS` / `_migrate_`；`orcsy/web.py` diff 含 `auth_password` / `api_keys` / `AUTH_COOKIE` / `token_valid` / `_handle_login` |
| medium | 其余 `orcsy/**/*.py`、`dashboard/src/**`、`config.example.yaml`、未知路径 |
| low | `docs/**`、`README*`、`CHANGELOG.md`、`tests/**`、`tasks/**`、`website/**`、`promo/**` |

- 项目可在 `risk_approval.extra_high_risk_paths` 追加高风险 glob；
- 多文件取最高等级；原因去重保序；
- 评估异常（git 命令失败）→ **high + 风险评估失败**，fail closed。

**digest**：`sha256(branch_sha + diff_hash + level + reasons)`。审批的是任务
commit 的内容，不把 base SHA 纳入；基线移动由 premerge 重测处理，避免主分支
活跃时审批频繁失效。

## 3. 配置

全局 `risk_approval` 分区（纯增量，不升 `CONFIG_VERSION`）：

```yaml
risk_approval:
  enabled: false
  policy:            # low / medium / high → off | single
    low: off
    medium: off
    high: single
  timeout_minutes: 60
  extra_high_risk_paths: []
```

- `policy` 只接受 `off` / `single`；`quorum` / 2 人会给明确错误并提示需要
  Phase 2 外部身份源；
- YAML 1.1 会把未加引号的 `off`/`on` 解析为 bool，解析器兼容为 `off`/`single`；
- `timeout_minutes` 1~1440，超时即阻断；
- 默认关闭；关闭时执行器零开销。

## 4. 执行流程

```text
WIP commit → CI 门禁（可选）→ 独立审查 → 风险评估 → 人工审批（高风险）
                                              │ 批准 → premerge → 合入
                                              ├ 拒绝 → blocked（保留分支）
                                              ├ 超时 → blocked（保留分支）
                                              └ 内容变化 → blocked（批准作废）
```

- 唯一入口：`TaskExecutor._merge_with_premerge` 的**合并锁之外**；
  人工等待最长 60 分钟，不能占住全局合并锁；
- 审批先于 premerge：批准的是任务内容；premerge 失败则任务 blocked，审批作废；
- 覆盖 review pass / review skipped / 补审恢复直调；
- 看板强制停止会取消等待（`TaskStopped`），不会让任务线程挂满超时。

## 5. 身份与「作者≠审批者」

- 审批请求的 `requested_by` 是 `agent:<角色>`；只有**看板登录会话**能响应，
  `api_key:` 开头的 actor 一律拒绝（避免脚本代批）；
- Executing Agent 没有任何接口可以调用审批端点，因此「Agent 不能自批」是硬约束；
- Phase 1 是单机单审批人：**同一个人可以批准自己发起的任务**。真正的
  「作者/审批人身份分离」需要账号或外部 IdP，属于 Phase 2；UI 不宣称双人会签。

**威胁模型**：Phase 1 防的是「脚本/API Key 代批、Agent 误触」——执行器没有
审批工具，API Key actor 被拒绝。它**不是**恶意代码沙箱：本机 Agent 若拥有
任意命令执行且看板无密码，理论上可直接请求未鉴权的看板或读取会话文件冒充
人类。需要更强隔离时，应设置看板登录密码并把审批放到另一台设备（移动端），
或等 Phase 2 的外部身份源/OS 级沙箱。不要把 Phase 1 描述成能抵抗本机恶意程序。

## 6. 审计与运行台账

- `logs/audit.jsonl`：`risk_approval.request|approve|reject|timeout|cancel|stale`，
  detail 含等级、原因、文件、增删数、digest、审批人、意见；自动脱敏；
- runlog：`risk_assess` / `risk_approval_request` / `risk_approval_decision`
  事件 + `risk.json` 工件（评估 + 审批决定）+ meta `risk_level` / `risk_approval`；
- 任务正文：实际触发审批的任务写「## 风险审批」（等级、原因、范围、digest、
  决定、审批人、意见、时间），随结果/归档留存；
- `block_reason`：`risk_approval_rejected` / `risk_approval_timeout` /
  `risk_approval_stale`。

## 7. Phase 2：真多人审批怎么接

Phase 1 不伪造身份。需要 N 人会签时，二选一：

1. **外部审批/身份系统（推荐）**：Orcsy 生成签名清单（project/task/risk/digest/
   diff stat/required approvals），POST 到 GitLab/Gitea/飞书/钉钉等审批，
   外部系统完成 N 个真实账号的会签，回调带 `approvers[]`；Orcsy 校验 HMAC、
   身份唯一性、人数与 digest，不可达/超时 fail closed；
2. **内置账号 + RBAC**：本地用户表、角色、每用户 token/会话、quorum 计数、
   审计与恢复；工期最长，且与 1.0「不做 SSO/RBAC」的边界冲突，建议作为独立
   团队版能力。

无论哪条路，都必须保留 Phase 1 的三条红线：**审批绑定 digest、作者不能自批、
不可用/超时 fail closed**。

详细协议、Provider 适配器、持久化与重启恢复、配置与排期见
[`design-risk-approval-phase2.md`](design-risk-approval-phase2.md)。

## 8. 验收

- `tests/test_risk.py`：分类、内容信号、自定义 glob、digest 稳定性、fail closed；
- `tests/test_risk_approval.py`：批准/拒绝/超时/重复响应/停止取消、执行器门禁、
  stale、合并前拦截、任务正文风险节；
- `tests/test_risk_approval_config.py`：配置往返/非法值/quorum 拒绝、`full_state`
  暴露、API Key 拒绝、审计记录；
- 回归：工具审批、配置往返、settings contract、runlog、示例模板。
