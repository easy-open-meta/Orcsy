# 风险审批 Phase 2：真多人会签设计（定稿）

> 状态：**Phase 2A/2B/2C 已实现并随 0.36.0 交付（Provider 接口、持久化 quorum、GitLab/GitHub 工单、飞书/钉钉审批实例轮询、设置面板）；飞书/钉钉 Webhook 与 Provider 凭据可视化待补**｜2026-11-27
> 关联：[`design-risk-approval.md`](design-risk-approval.md)（Phase 0+1）、
> [`roadmap.md`](roadmap.md) §8、[`design-release-quality-gaps.md`](design-release-quality-gaps.md) 阶段②
> 目标：在**不伪造身份**的前提下支持高风险合入的 N 人会签（quorum），并保持
> Orcsy 单机/内网/离线优先的定位。

## 1. 结论

Phase 1 只能做「单机单审批人」：一个共享密码、一个看板会话，无法证明 N 个
不同的人。Phase 2 要解决的是**身份来源**，不是再做一个审批弹窗。

推荐路线：

1. **首批 Provider**：GitLab、GitHub、飞书、钉钉（建议按此顺序推进；Gitea/
   企业微信后续按需增加）。Orcsy 只负责生成审批清单、校验决定、绑定 digest；
   真实身份与审批动作交给这些系统。
2. **回调优先，轮询兜底**：部署后使用 Orcsy 自己的公网域名
   `https://<orcsy-domain>/api/risk-approval/callback/{provider}`；开发期
   localhost 无法被 GitHub/飞书/钉钉 SaaS 回调，`callback_url` 留空即走
   `poll()`，或用本地假 Provider/临时隧道只验证回调契约。回调丢失时始终用
   `poll()` 兜底，不因为「回调失败」退化成假审批。
3. **email 白名单是身份真值**：按人数会签，但只有配置中指定的 email 对应的
   GitLab/GitHub/飞书/钉钉用户才能计入批准；未登记用户的批准/拒绝一律忽略
   （审计留痕）。**不要求角色**，只按人数。
4. **内置账号/RBAC 排进 1.x（Phase 2C）**：作为完全离线、无外部身份源时的
   兜底；它与共享密码并存需要迁移方案，单独立项，不与 2A/2B 混做。
5. **Phase 1 的 `single` 保持不变**：`single` 仍由看板登录会话批准；新增
   `quorum` 走外部 Provider。中风险可以继续 `single`，高风险 `quorum`。

## 2. 范围与非目标

**做**：

- 按风险等级配置 `quorum` 人数，以及**指定 email 白名单**（email ↔ Provider
  用户映射）；
- Provider 注册表：GitLab / GitHub / 飞书 / 钉钉（首批四个）；
- 审批请求持久化、重启恢复、幂等回调、过期与撤销；
- 审批清单签名、回调 HMAC 校验、重放防护；
- 公网回调端点 `https://<domain>/api/risk-approval/callback` + 轮询兜底；
- 看板显示 quorum 进度、审批人、外部链接；设置页 Provider 管理；
- 审计与 runlog 记录请求、轮询、每个决定、最终结果；
- Phase 2C：1.x 内的内置本地账号 + RBAC（完全离线兜底），单独立项。

**不做**：

- 自建 SSO/IdP；不承诺任意 Provider 零配置；
- 远程多实例/集群审批（仍单实例，审批可来自外部系统）；
- 自动合并低风险改动（仍走现有审查/CI/人工提交门）；
- 把外部 Provider 的「已批准」当作代码正确性证据（仍需 CI/审查通过）；
- 首批不做 Gitea / 企业微信（后续按需追加适配）。

## 3. 身份与信任模型

| 模式 | 身份来源 | 适用 | 结论 |
| --- | --- | --- | --- |
| `single`（Phase 1） | 看板登录会话 | 中风险、单机 | 保留 |
| `quorum` + 外部 Provider | GitLab/GitHub/飞书/钉钉 | 高风险、团队 | **Phase 2 主路线** |
| `quorum` + 内置账号 | Orcsy 本地用户表 + 角色 | 完全离线、无 IdP | Phase 2C，排进 1.x |

外部 Provider 的可信边界：

- **email 白名单是唯一计数口径**：配置里登记 `email ↔ GitLab/GitHub/飞书/钉钉
  用户` 的映射；只有映射到白名单 email 的批准才计数。未登记用户的批准/拒绝
  一律忽略，但写审计；
- 同一 email 的多个 Provider 账号只计一次；同一 Provider 账号重复回调只计一次；
- 不要求角色，只按人数；`agent:*` / `service:*` 不得出现在 approver 列表；
- Orcsy 无法识别「同一个人用两个不同 email」；这属于 Provider 的身份治理责任；
- Provider 不可达、签名错误、人数不足、email 未登记、digest 不一致 → **fail
  closed**，绝不放行。

### 3.1 email ↔ Provider 用户映射

GitLab/GitHub 的公开 API 不保证返回任意用户的 email（GitLab 可能只有
`public_email`，GitHub 的 `/user/emails` 只对本人 token 有效），因此**不要**
在审批时反查 email。改为在 Orcsy 配置中显式登记：

```yaml
approvers:
  alice@corp.com:
    gitlab: alice
    github: alice-gh
    feishu: ou_xxx
    dingtalk: userid_xxx
```

- Provider 返回 `username` / `login` / `open_id` / `user_id`；
- Orcsy 用该身份反查唯一 email；找不到 → 该决定不计入 quorum；
- 一个 email 可映射多个 Provider 身份，但只算一个人；
- 配置变更后，进行中的请求按创建时的映射快照计数，避免中途换人。

## 4. 总体架构

```text
executor._approve_risk_merge
        │  risk=high, policy=quorum
        ▼
QuorumApprovalManager ──持久化──> state/risk_approvals.json
        │  create(manifest)
        ▼
ApprovalProvider (gitlab / github / feishu / dingtalk)
        │  poll() 或 callback
        ▼
ProviderDecision {status, approvers[], comment, digest, external_id}
        │  校验 quorum/email 白名单/digest/签名
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
  "allowed_approvers": ["alice@corp.com", "bob@corp.com"],
  "requested_by": "agent:backend",
  "created_at": "2026-11-26T10:00:00+08:00",
  "expires_at": "2026-11-26T14:00:00+08:00",
  "callback_url": "https://<orcsy-domain>/api/risk-approval/callback/{provider}"
}
```

- **不包含** diff 正文、密钥、任务全文；只含路径/行数/原因；
- `digest` 与 Phase 1 相同：`sha256(branch_sha + diff_hash + level + reasons)`；
- `required_approvals` 与 `allowed_approvers` 由 Orcsy 配置决定，Provider 不得
  改小或扩权；`allowed_approvers` 只是提示，最终以 Orcsy 的 email 映射校验为准。

### 5.2 决定（Provider → Orcsy）

```json
{
  "schema": "orcsy.approval.decision.v1",
  "request_id": "01J...",
  "external_id": "gitlab-mr-123",
  "digest": "sha256:...",
  "decision": "approved",
  "approvers": [
    {"provider": "gitlab", "id": "alice", "name": "Alice",
     "decision": "approved", "at": "2026-11-26T10:30:00+08:00"},
    {"provider": "gitlab", "id": "bob", "name": "Bob",
     "decision": "approved", "at": "2026-11-26T10:35:00+08:00"}
  ],
  "comment": "looks good",
  "timestamp": 1764130000
}
```

回调头：`X-Orcsy-Signature: sha256=<hmac>`，`X-Orcsy-Timestamp`；HMAC 覆盖
`timestamp + "." + raw_body`，共享密钥来自 Provider 配置。

`approvers[].id` 是 Provider 原生身份（GitLab username / GitHub login /
飞书 open_id / 钉钉 user_id）；Orcsy 用 `approvers` 映射反查 email，未登记或
不在 `allowed_approvers` 的决定**不计入 quorum**（仍写审计）。

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
| 人数 | `required_approvals` 来自配置；按**不同 email** 去重计数 |
| 白名单 | 只有 `allowed_approvers` 中的 email 计入；未登记用户的批准/拒绝忽略并审计 |
| 角色 | 首批**不要求角色**，只按人数；后续可加 `required_roles` |
| 否决 | 任一**白名单内** approver `decision=rejected` → 整体 rejected |
| 自批 | approver 不得为 `agent:*` / `service:*`，也不得等于 `requested_by` |
| 重复 | 同一 email 的多个 Provider 账号只计一次；重复回调幂等返回 200 |
| 降级 | Provider 不得通过回调把 required 人数或白名单改小；不一致即拒绝 |
| 过期 | `expires_at` 到达后拒绝任何新决定；已批准但在过期后回调不生效 |

## 7. 配置

Provider 注册表（仿 `storage_targets`，凭据三态脱敏）：

```yaml
approval_providers:
  gitlab:
    type: gitlab
    base_url: https://gitlab.example.com
    project_id: 123
    token: "..."
    callback_secret: "..."
  github:
    type: github
    base_url: https://api.github.com
    repo: owner/name
    token: "..."
    callback_secret: "..."
  feishu:
    type: feishu
    app_id: "..."
    app_secret: "..."
    approval_code: "..."
    callback_secret: "..."
  dingtalk:
    type: dingtalk
    app_key: "..."
    app_secret: "..."
    process_code: "..."
    callback_secret: "..."
```

`approvers` 注册表（email 是唯一计数口径）：

```yaml
approvers:
  alice@corp.com:
    gitlab: alice
    github: alice-gh
    feishu: ou_xxx
    dingtalk: userid_xxx
  bob@corp.com:
    gitlab: bob
    github: bob-gh
```

`risk_approval` 扩展：

```yaml
risk_approval:
  enabled: true
  provider: gitlab           # single 策略不使用；quorum 必填
  policy:
    low: off
    medium: single           # 仍由看板会话批准
    high: quorum             # 外部 Provider
  quorum:
    high: 2
  allowed_approvers:
    high: [alice@corp.com, bob@corp.com]
  timeout_minutes: 240
  poll_interval_seconds: 20
  # 生产：Orcsy 自己的公网域名；{provider} 由适配器替换
  # 开发/无公网：留空 = 仅轮询（localhost 不能被 GitHub/飞书/钉钉 SaaS 回调）
  # 该字段可在看板「设置 → 风险审批」可视化编辑；空值 = 仅轮询
  callback_url: https://<orcsy-domain>/api/risk-approval/callback/{provider}
```

校验：

- `quorum` 只允许在配置了非空 `provider` 时使用；`quorum[level] >= 2`；
- `allowed_approvers[level]` 必须都存在于 `approvers` 注册表，且去重后数量
  不少于 `quorum[level]`；
- 同一 Provider 身份（如 `gitlab: alice`）只能映射一个 email，防止重复计数；
- `single` 仍使用 Phase 1 的看板会话，不依赖 Provider；
- `approval_providers` / `approvers` 的 secret 只回掩码；`/api/state` 不下发明文；
- `CONFIG_VERSION` 升到 3（新增 `quorum` 枚举值、Provider 注册表、approvers），
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
- `risk_approval_invalid`（签名/人数/email 白名单/重复等校验失败）。

### Web

- `POST /api/risk-approval/callback/{provider}`：**直接使用 Orcsy 自己的域名**，
  不单独申请回调域名。免看板登录，但必须按 Provider 的 `callback_secret` 验
  HMAC；校验 timestamp（±5 分钟）、request_id、digest、quorum、email 白名单、
  去重；成功 200，失败 400/401 返回 JSON 并审计；重复回调幂等。不要 302 到
  登录页。
- 回调路径按 Provider 区分（`/callback/gitlab`、`/callback/github`、
  `/callback/feishu`、`/callback/dingtalk`），避免多 Provider 共用密钥与
  解析分支；`callback_url` 用配置值，不从 `Host` 头推导。
- `GET /api/state`：`risk_approvals` 增加 `provider`、`required`、`approvers`
  （含解析后的 email/显示名）、`external_url`、`status`；quorum 请求不显示
  本地「批准/拒绝」按钮，只显示「打开审批」与「刷新」。
- `POST /api/approval-provider/test`（看板会话）：测试 Provider 连通性；
  `POST /api/risk-approval/refresh`（可选）：手动触发一次轮询。
- 设置页：Provider 增删改（secret 三态）、选择 Provider、quorum/白名单/
  timeout、**callback_url 可视化编辑**（空 = 仅轮询；「使用当前访问域名」一键
  填充；按 Provider 生成完整回调 URL 并提供复制/测试按钮）；回调不可达时提示
  轮询兜底。服务端保存时校验：空值或 https（本地开发允许 `http://127.0.0.1`），
  非空值必须包含 `{provider}` 占位符。
- 设置页「测试」分两种：Provider 出站连通性；本地回环自测（生成一条签名回调
  打到本机 callback 路由，验证验签/幂等）。公网反代是否可达，最终用 Provider
  的测试事件验证。

### 9.1 反代与可达性

- 反向代理把 `/api/risk-approval/callback/*` **原样 POST** 到 Orcsy web 端口，
  保留原始 body（HMAC 基于原始字节）、启用 TLS、不缓存、不插入登录跳转/WAF
  拦截；
- Orcsy 的鉴权中间件对该路径放行，但 handler 必须验签——同域不等于信任；
- 若 Orcsy 挂在路径前缀下（如 `https://domain/orcsy/`），`callback_url` 必须
  显式包含该前缀，不能从 `Host` 头推导（可伪造）；
- 设置页的「使用当前访问域名」只是把 `window.location.origin` 填进输入框供
  人工确认；服务端保存与回调校验始终以配置值为准，不信任请求 `Host`；
- 自建 GitLab 只需内网可达；GitHub/飞书/钉钉 SaaS 需要公网可达；不可达时用
  Provider API 轮询兜底，绝不降级为单人审批；
- 回调失败/超时/验签失败一律 fail closed，并写审计。

**环境矩阵**：

| 环境 | `callback_url` | 模式 | 说明 |
| --- | --- | --- | --- |
| 本地开发 | 空 | 仅轮询 | GitHub/飞书/钉钉 SaaS 无法回调 localhost；用假 Provider 测契约 |
| 本地联调 | 临时隧道 `https://xxx` | 回调 + 轮询 | cloudflared/ngrok 等，仅开发环境使用 |
| 生产 | `https://<orcsy-domain>/api/risk-approval/callback/{provider}` | 回调 + 轮询 | 公网可达 + TLS + 反代 |
| 内网 GitLab | 空或内网 URL | 轮询为主 | 内网可达时可回调 |

`callback_url` 留空即**仅轮询**；不要把 `http://127.0.0.1` 发给 SaaS Provider。

## 10. 安全与威胁模型

- 回调端点免登录但**必须**验签；无签名/签名错误/时间戳过期 → 401，审计；
- 每个 Provider 独立 `callback_secret`，支持双密钥轮换；同域不等于信任；
- HMAC 常量时间比较；`raw_body` 参与签名，避免 JSON 重排绕过；
- 重放：timestamp 窗口 + request_id 幂等 + 去重缓存；
- TLS 默认必须；生产 `callback_url` 必须 https；仅本地开发/假 Provider 允许
  `http://127.0.0.1`（显式配置），且不得把 localhost URL 发给 SaaS Provider；
- Provider secret/token 只存配置、只回掩码；日志脱敏；
- 清单不含 diff 正文/密钥；只含路径、行数、风险原因；
- 外部 Provider 是身份真值源；Orcsy 不做「同人多账号」识别，文档明确；
- 本机恶意程序仍可能读取配置/会话；Phase 2 的外部 Provider 把审批移到另一台
  设备/系统，显著提高攻击成本，但不宣称 OS 级沙箱。

## 11. Provider 适配矩阵

| Provider | 身份 | Quorum | 接入方式 | 备注 |
| --- | --- | --- | --- | --- |
| GitLab 审批工单 | GitLab 账号 | issue 评论 `/approve` 计数 | 回调 + API 轮询兜底 | **已实现**；username 映射 email，不推分支 |
| GitHub 审批工单 | GitHub 账号 | issue 评论 `/approve` 计数 | 回调 + API 轮询兜底 | **已实现**；login 映射 email，不推分支 |
| 飞书审批 | 飞书账号 | 原生会签 | 审批实例轮询 | **已实现（轮询）**；user_id/open_id 映射 email；Webhook 暂未启用 |
| 钉钉审批 | 钉钉账号 | 原生会签 | 审批实例轮询 | **已实现（轮询）**；userid 映射 email；Webhook 暂未启用 |
| Gitea protected branch reviews | Gitea 账号 | 取决于版本/保护规则 | API 轮询 | 后续按需，需核验实例能力 |
| 企业微信审批 | 企业微信账号 | 原生会签 | 回调 + 轮询 | 后续按需 |
| 内置账号 + RBAC | Orcsy 本地用户 | 自建 | 本地 | Phase 2C，排进 1.x |

首批范围已定：**GitLab、GitHub、飞书、钉钉**；建议按 GitLab → GitHub → 飞书 →
钉钉推进。GitLab/GitHub 先做，因为开发者身份与 PR/MR 审批语义最直接；飞书/
钉钉随后覆盖国内办公 IM 场景。通用 webhook 只保留为契约测试/内部桥接，不作为
首批产品能力。

## 12. 迁移与兼容

- Phase 1 `policy=off|single` 配置原样可用；
- 新增 `quorum` 值 + `approval_providers` + `approvers` 分区；
- **`CONFIG_VERSION` 升到 3**：v2→v3 为恒等迁移；旧版本（v2）遇到 v3 明确
  拒绝加载，避免把 quorum 静默降级；
- Phase 1 的内存 pending 不迁移；Phase 2 起新请求写持久化；
- 项目级覆盖（可选）：Phase 2 先全局，项目级 quorum 后置，避免配置面膨胀。

## 13. 测试与验收

- 真实飞书/钉钉租户的免费测试企业搭建与会签冒烟，见
  [`feishu-dingtalk-test-setup.md`](feishu-dingtalk-test-setup.md)；
- 假 Provider HTTP 服务：create/poll/callback、HMAC、timestamp、重放、重复回调；
- quorum：2 个白名单 email 达标、1 个不足、同一 email 多账号只计一次、任一
  白名单内拒绝一票否决；
- 白名单：未登记 email 的批准/拒绝忽略并审计；同一 Provider 身份映射两个
  email 的配置拒绝加载；
- 自批：approver=`agent:backend` / `service:*` 或等于 requested_by → 拒绝；
- digest：批准后改动 branch → stale；
- 重启：pending/approved 请求持久化，任务恢复后复用/继续等待；
- Provider 故障：连续超时 → `risk_approval_provider_error`，不放行；
- UI：quorum 进度、审批人（email）列表、外部链接、无本地批准按钮；
- 审计/runlog：request/poll/decision/invalid/stale 全留痕。

## 14. 排期与决策点

粗估：

| 阶段 | 内容 | 工期 |
| --- | --- | --- |
| 2A | Provider 接口 + 持久化 + 回调/轮询 + email 映射/白名单 + UI + 假 Provider 契约测试 | 2–3 周 |
| 2B | GitLab + GitHub 适配（同一批） | 2 周 |
| 2C | 飞书 + 钉钉审批适配 | 2–3 周 |
| 2D | 内置账号/RBAC（1.x） | 3–4 周 |

**已确认**：

1. 首批 Provider：GitLab、GitHub、飞书、钉钉；
2. 只按人数，不要求角色；只有指定 email 对应的 Provider 用户可审批；
3. 公网回调地址使用 `<domain>/api/risk-approval/callback`，轮询兜底；
4. 内置账号/RBAC 排进 1.x（Phase 2D）。

**仍需确认**：

1. 高风险默认 quorum 人数（当前 UI/示例默认 2；中/低默认 off/single）；
2. GitLab 与 GitHub 的先后（建议 GitLab 先，GitHub 紧随）；
3. 回调域名、TLS 与反代由谁提供；是否允许本机 `127.0.0.1` 调试；
4. 内置账号迁移：共享密码保留为兼容模式，还是强制首个 admin 账号。

Phase 1 的 `single` 仍是默认安全落点；Phase 2A/2B 完成前不把 quorum 写成已交付。

## 15. 2A 实现状态（2026-11-26）

已落地：

- `CONFIG_VERSION=3`（2→3 恒等迁移）：`approval_providers`、`approvers`
  （email↔Provider 身份）、`risk_approval.{provider,quorum,allowed_approvers,
  callback_url,poll_interval_seconds}`；
- `orcsy/approval_providers.py`：统一 Provider 协议、HMAC 回调校验、`fake`
  测试 Provider，以及 GitLab/GitHub 工单、飞书/钉钉审批实例适配器；
- `orcsy/quorum_approval.py`：持久化请求、轮询/回调、email 白名单去重、
  一票否决、超时/取消/Provider 故障 fail closed、重启复用；
- executor quorum 分支与 `block_reason`；Web `POST /api/risk-approval/callback/{provider}`
  （免登录但强制验签）、`full_state` 暴露 Provider/approvers/会签进度；
- 看板：Provider 选择、quorum/白名单、callback_url（含「使用当前访问域名」）、
  轮询间隔、email↔身份映射；待审批弹窗显示会签进度与外部链接；
- 测试：配置 v3、持久化、HMAC 回调、quorum/白名单/重启、executor 接线。

2B 已落地：**GitLab/GitHub 审批工单适配器**——创建 issue、轮询评论
`/approve` / `/reject`，支持 GitLab `X-Gitlab-Token` 与 GitHub
`X-Hub-Signature-256` Webhook 验签；不推送代码分支。

2C 已落地：**飞书/钉钉审批实例适配器**——获取 tenant/access token、创建审批
实例、轮询 task_list/operation_records；飞书使用 tenant_access_token，
钉钉使用 appkey/appsecret。两者当前**仅轮询**，Webhook 暂未启用（选择回调会在
`verify_callback` 明确报「请使用轮询」），不影响 quorum 正确性。

未落地：飞书/钉钉 Webhook 事件订阅（可选增强）；Provider 凭据的可视化编辑
（当前通过 config.yaml 的 approval_providers 配置，设置页选择名称）。
