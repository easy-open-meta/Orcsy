# 风险审批 Phase 2：真多人会签设计（评估稿）

> 状态：**设计提案，尚未实施**｜2026-11-26
> 关联：[`design-risk-approval.md`](design-risk-approval.md)（Phase 0+1）、
> [`roadmap.md`](roadmap.md) §8、[`design-release-quality-gaps.md`](design-release-quality-gaps.md) 阶段②
> 目标：在**不伪造身份**的前提下支持高风险合入的 N 人会签（quorum），并保持
> Orcsy 单机/内网/离线优先的定位。

## 1. 结论

Phase 1 只能做「单机单审批人」：一个共享密码、一个看板会话，无法证明 N 个
不同的人。Phase 2 要解决的是**身份来源**，不是再做一个审批弹窗。

推荐路线：

1. **Provider 适配器架构**：Orcsy 只负责生成审批清单、校验决定、绑定 digest；
   真实身份与审批动作交给外部系统（GitLab / Gitea / 飞书 / 钉钉 / 企业微信 /
   通用 webhook 桥）。
2. **轮询优先，回调可选**：Orcsy 通常运行在桌面/内网/NAT 后，外部 SaaS 无法
   直接回调。Provider 接口同时支持 `poll()` 与 `verify_callback()`；没有公网
   回调地址时用轮询，不因为「无法回调」退化成假审批。
3. **不把内置账号/RBAC 作为首选**：本地账号要重建用户表、会话、角色、迁移
   与恢复，工期最长，且与 1.0「不做 SSO/RBAC」的边界冲突。只有「完全离线且
   没有任何外部身份源」是硬需求时，才单独立项（Phase 2C）。
4. **Phase 1 的 `single` 保持不变**：`single` 仍由看板登录会话批准；新增
   `quorum` 走外部 Provider。中风险可以继续 `single`，高风险 `quorum`。

## 2. 范围与非目标

**做**：

- 按风险等级配置 `quorum` 人数与必需角色；
- Provider 注册表（webhook / GitLab / Gitea / 飞书 / 钉钉 / 企业微信）；
- 审批请求持久化、重启恢复、幂等回调、过期与撤销；
- 审批清单签名、回调 HMAC 校验、重放防护；
- 看板显示 quorum 进度、审批人、外部链接；设置页 Provider 管理；
- 审计与 runlog 记录请求、轮询、每个决定、最终结果。

**不做**：

- 自建 SSO/IdP；不承诺任意 Provider 零配置；
- 远程多实例/集群审批（仍单实例，审批可来自外部系统）；
- 自动合并低风险改动（仍走现有审查/CI/人工提交门）；
- 把外部 Provider 的「已批准」当作代码正确性证据（仍需 CI/审查通过）。

## 3. 身份与信任模型

| 模式 | 身份来源 | 适用 | 结论 |
| --- | --- | --- | --- |
| `single`（Phase 1） | 看板登录会话 | 中风险、单机 | 保留 |
| `quorum` + 外部 Provider | GitLab/Gitea/飞书/钉钉/企业微信/自建审批 | 高风险、团队 | **Phase 2 主路线** |
| `quorum` + 内置账号 | Orcsy 本地用户表 + 角色 | 完全离线、无 IdP | Phase 2C，单独立项 |

外部 Provider 的可信边界：

- Orcsy **信任 Provider 返回的 approver 身份**，但会校验：人数、去重、必需
  角色、`agent:*`/`service:*` 不得出现在 approver 列表、digest 未变、未过期；
- Orcsy 无法识别「同一个人用两个账号」；这属于 Provider 的身份治理责任；
- Provider 不可达、签名错误、人数不足、角色缺失、digest 不一致 → **fail closed**，
  绝不放行。

## 4. 总体架构

```text
executor._approve_risk_merge
        │  risk=high, policy=quorum
        ▼
QuorumApprovalManager ──持久化──> state/risk_approvals.json
        │  create(manifest)
        ▼
ApprovalProvider (webhook / gitlab / gitea / feishu / ...)
        │  poll() 或 callback
        ▼
ProviderDecision {status, approvers[], comment, digest, external_id}
        │  校验 quorum/roles/digest/签名
        ▼
approved → premerge → merge
rejected/timeout/provider_error/stale → blocked（保留分支）
```

关键点：

- 审批仍在**合并锁之外**等待，不能占住全局合并锁；
- 请求与决定**持久化**，进程重启后可按 `project/task/digest` 复用已批准结果；
- Provider 只返回「谁批准了」，不返回「代码是否可合并」；CI/审查仍是独立门禁。

## 5. 协议与数据模型

### 5.1 审批清单（Orcsy → Provider）

```json
{
  "schema": "orcsy.approval.request.v1",
  "request_id": "01J...",
  "project": "orcsy",
  "task_id": "P1-abc",
  "title": "修改自更新下载校验",
  "risk": {
    "level": "high",
    "reasons": ["自更新逻辑：orcsy/updater.py"],
    "files": ["orcsy/updater.py"],
    "additions": 42,
    "deletions": 7
  },
  "commit": "<full sha>",
  "digest": "sha256:...",
  "required_approvals": 2,
  "required_roles": ["release_manager"],
  "requested_by": "agent:backend",
  "created_at": "2026-11-26T10:00:00+08:00",
  "expires_at": "2026-11-26T14:00:00+08:00",
  "callback_url": "https://orcsy.example/api/risk-approval/callback"
}
```

- **不包含** diff 正文、密钥、任务全文；只含路径/行数/原因；
- `digest` 与 Phase 1 相同：`sha256(branch_sha + diff_hash + level + reasons)`；
- `required_approvals` / `required_roles` 由 Orcsy 配置决定，Provider 不得改小。

### 5.2 决定（Provider → Orcsy）

```json
{
  "schema": "orcsy.approval.decision.v1",
  "request_id": "01J...",
  "external_id": "gitlab-mr-123",
  "digest": "sha256:...",
  "decision": "approved",
  "approvers": [
    {"id": "alice", "name": "Alice", "role": "release_manager",
     "decision": "approved", "at": "2026-11-26T10:30:00+08:00"},
    {"id": "bob", "name": "Bob", "role": "security",
     "decision": "approved", "at": "2026-11-26T10:35:00+08:00"}
  ],
  "comment": "looks good",
  "timestamp": 1764130000
}
```

回调头：`X-Orcsy-Signature: sha256=<hmac>`，`X-Orcsy-Timestamp`；HMAC 覆盖
`timestamp + "." + raw_body`，共享密钥来自 Provider 配置。

### 5.3 状态机

```text
pending ── approve(quorum 达标) ──> approved ──> merged
   │                                  │
   ├── reject（任一否决） ───────────> rejected
   ├── expires_at 到达 ──────────────> expired
   ├── digest 变化 ──────────────────> stale
   └── provider 连续失败 ────────────> provider_error
```

- 任一 approver 拒绝 → 立即 `rejected`（一票否决），不再等其他人；
- `approved` 后 digest 变化 → `stale`，旧批准作废，重新发起；
- 超时/Provider 不可达 → blocked，不自动降级为单人审批。

## 6. Quorum 语义

| 规则 | 定义 |
| --- | --- |
| 人数 | `required_approvals` 来自配置；不同 approver `id` 去重后计数 |
| 角色 | `required_roles` 中每个角色至少有一个 approving approver |
| 否决 | 任一 approver `decision=rejected` → 整体 rejected |
| 自批 | approver id 不得为 `agent:*` / `service:*`，也不得等于 `requested_by` |
| 重复 | 同一 id 多次回调只计一次；重复回调幂等返回 200 |
| 降级 | Provider 不得通过回调把 required 人数/角色改小；不一致即拒绝 |
| 过期 | `expires_at` 到达后拒绝任何新决定；已批准但在过期后回调不生效 |

## 7. 配置

Provider 注册表（仿 `storage_targets`，凭据三态脱敏）：

```yaml
approval_providers:
  corp_webhook:
    type: webhook
    url: https://approvals.example.com/api/requests
    poll_url: https://approvals.example.com/api/requests/{id}
    secret: "..."
    verify_tls: true
    timeout_seconds: 10
  gitlab:
    type: gitlab
    base_url: https://gitlab.example.com
    project_id: 123
    token: "..."
  feishu:
    type: feishu
    app_id: "..."
    app_secret: "..."
    approval_code: "..."
```

`risk_approval` 扩展：

```yaml
risk_approval:
  enabled: true
  provider: corp_webhook      # single 策略不使用；quorum 必填
  policy:
    low: off
    medium: single            # 仍由看板会话批准
    high: quorum              # 外部 Provider
  quorum:
    high: 2
  required_roles:
    high: [release_manager]
  timeout_minutes: 240
  poll_interval_seconds: 20
  on_provider_error: block    # 固定 fail closed；保留字段便于显式审计
```

校验：

- `quorum` 只允许在配置了非空 `provider` 时使用；`quorum[level] >= 2`；
- `single` 仍使用 Phase 1 的看板会话，不依赖 Provider；
- `approval_providers` 的 secret 只回掩码；`/api/state` 不下发明文；
- `CONFIG_VERSION` 建议升到 3（新增 `quorum` 枚举值与 provider 注册表），
  旧版本遇到 v3 会明确拒绝加载，而不是半解析。

## 8. 持久化与恢复

- `state/risk_approvals.json`（原子写 + 文件锁，同 claims）：
  `requests[]`、`decisions[]`、`last_poll_at`、`callback_dedupe[]`；
- 进程启动：加载 pending；任务恢复后，`_approve_risk_merge` 先按
  `(project, task_id, digest)` 查找：
  - `approved` 且未过期 → 直接复用，不再要求人工；
  - `pending` → 附着并继续等待/轮询；
  - `rejected/expired/stale` → 按 blocked 处理，需重新发起；
- 清理：过期后保留 7 天供审计，之后删除；回调去重缓存保留 24 小时；
- 单实例假设不变；多实例需要共享数据库，Phase 2 不做。

## 9. 执行器与 Web

### 执行器

`_approve_risk_merge` 按 `policy` 分派：

- `off` → 放行；
- `single` → Phase 1 本地 manager（看板会话）；
- `quorum` → `QuorumApprovalManager.request(...)`：创建/复用请求，轮询 + 回调，
  阻塞等待，响应停止信号；Provider 连续失败 N 次 → `risk_approval_provider_error`。

`block_reason` 新增：

- `risk_approval_rejected` / `risk_approval_timeout`（复用 Phase 1）；
- `risk_approval_stale`（digest 变化）；
- `risk_approval_provider_error`（Provider 不可达/协议错误）；
- `risk_approval_invalid`（签名/人数/角色/重复等校验失败）。

### Web

- `POST /api/risk-approval/callback`：**免看板登录，但必须 HMAC 校验**；
  校验 timestamp（±5 分钟）、request_id、digest、quorum、roles、去重；
  成功 200，失败 400/401 并审计；重复回调幂等。
- `GET /api/state`：`risk_approvals` 增加 `provider`、`required`、`approvers`、
  `external_url`、`status`；quorum 请求不显示本地「批准/拒绝」按钮，只显示
  「打开审批」与「刷新」。
- `POST /api/approval-provider/test`（看板会话）：测试 Provider 连通性；
  `POST /api/risk-approval/refresh`（可选）：手动触发一次轮询。
- 设置页：Provider 增删改（secret 三态）、选择 Provider、quorum/roles/timeout、
  回调地址展示与复制、测试按钮；无公网回调时提示使用轮询。

## 10. 安全与威胁模型

- 回调端点免登录但**必须**验签；无签名/签名错误/时间戳过期 → 401，审计；
- HMAC 常量时间比较；`raw_body` 参与签名，避免 JSON 重排绕过；
- 重放：timestamp 窗口 + request_id 幂等 + 去重缓存；
- TLS 默认必须；只允许 `http://127.0.0.1` 用于本机桥接（显式配置）；
- Provider secret/token 只存配置、只回掩码；日志脱敏；
- 清单不含 diff 正文/密钥；只含路径、行数、风险原因；
- 外部 Provider 是身份真值源；Orcsy 不做「同人多账号」识别，文档明确；
- 本机恶意程序仍可能读取配置/会话；Phase 2 的外部 Provider 把审批移到另一台
  设备/系统，显著提高攻击成本，但不宣称 OS 级沙箱。

## 11. Provider 适配矩阵

| Provider | 身份 | Quorum | 接入方式 | 备注 |
| --- | --- | --- | --- | --- |
| 通用 webhook 桥 | 由桥决定 | 支持 | create + poll/callback | **首个实现**，契约测试用假 Provider |
| GitLab MR approvals | GitLab 账号 | 原生 required approvals | API 轮询 | 团队已有 GitLab 时最省事 |
| Gitea protected branch reviews | Gitea 账号 | 取决于版本/保护规则 | API 轮询 | 需核验目标实例能力 |
| 飞书/钉钉/企业微信审批 | 企业账号 | 原生会签 | 审批实例轮询 | 国内团队常用；回调需公网 |
| 内置账号 + RBAC | Orcsy 本地用户 | 自建 | 本地 | Phase 2C，工期最长 |

首个真实适配建议按用户现有系统选：已有 GitLab 选 GitLab；国内办公 IM 选飞书/
钉钉；否则先落通用 webhook 桥，再按需加适配。

## 12. 迁移与兼容

- Phase 1 `policy=off|single` 配置原样可用；
- 新增 `quorum` 值 + `approval_providers` 分区；建议 `CONFIG_VERSION=3`；
- 旧版本（v2）遇到 v3 配置明确拒绝加载，避免把 quorum 静默降级；
- Phase 1 的内存 pending 不迁移；Phase 2 起新请求写持久化；
- 项目级覆盖（可选）：Phase 2 先全局，项目级 quorum 后置，避免配置面膨胀。

## 13. 测试与验收

- 假 Provider HTTP 服务：create/poll/callback、HMAC、timestamp、重放、重复回调；
- quorum：2 人达标、1 人不足、重复 id 只计一次、任一拒绝一票否决；
- roles：缺 release_manager → 不通过；
- 自批：approver=`agent:backend` 或等于 requested_by → 拒绝；
- digest：批准后改动 branch → stale；
- 重启：pending/approved 请求持久化，任务恢复后复用/继续等待；
- Provider 故障：连续超时 → `risk_approval_provider_error`，不放行；
- UI：quorum 进度、审批人列表、外部链接、无本地批准按钮；
- 审计/runlog：request/poll/decision/invalid/stale 全留痕。

## 14. 排期与决策点

粗估：

| 阶段 | 内容 | 工期 |
| --- | --- | --- |
| 2A | Provider 接口 + 持久化 + 通用 webhook + 回调/轮询 + UI + 假 Provider 测试 | 2–3 周 |
| 2B | 首个真实适配（GitLab 或飞书/钉钉） | 1–1.5 周 |
| 2C | 内置账号/RBAC（仅离线硬需求） | 3–4 周 |

需先定：

1. 首个真实 Provider（GitLab / Gitea / 飞书 / 钉钉 / 通用桥）；
2. 高风险 quorum 人数与必需角色（默认 2 人 + release_manager？）；
3. 回调可达性：是否接受轮询为主；是否有公网反代；
4. 是否升 `CONFIG_VERSION=3`（建议升）；
5. 是否把内置账号排入 1.x，还是只做外部 Provider。

在以上决策确定前，不把 Phase 2 写成已承诺功能；Phase 1 的 `single` 仍是默认
安全落点。
