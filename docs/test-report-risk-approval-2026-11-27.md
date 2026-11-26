# 风险审批 Phase 1/2 实际测试报告（2026-11-27）

> 测试分支：`test/risk-approval-full`（`a296354` + 修复 `b5358d0`）
> 环境：Windows 10.0.26300 x64、Chrome DevTools MCP、`uv run`、本机回环
> 范围：风险审批 Phase 0+1（单人）与 Phase 2A/2B/2C（会签骨架、工单/审批实例适配器、设置页）
> 结论：**核心链路通过**；真实外部 Provider、真实 LLM、公网回调未实测（见 §4）。

## 1. 测试方式

- 启动隔离的 `AppState + make_server` 实例（不碰用户真实配置/数据目录）；
- 用 chrome-devtools MCP 操作真实看板页面；
- Phase 1 使用真实 `RiskApprovalManager`；Phase 2 使用 `fake` Provider + 真实
  `QuorumApprovalManager`，并通过真实 HTTP `POST /api/risk-approval/callback/fake1`
  投递带 HMAC 的会签决定；
- 任务执行链路使用脚本化 `LLMClient`（不联网）驱动真实 `TaskExecutor`：
  修改 `orcsy/updater.py` → WIP 提交 → 风险门禁暂停 → 页面批准 → 合入。
- 注：本机 Python 回环客户端偶发 `ConnectionReset`/`Timeout`（浏览器请求正常，
  重试即成功），测试用重试规避；按环境抖动处理，不判为产品缺陷。

## 2. 已通过的项目

| # | 场景 | 结果 |
| --- | --- | --- |
| 1 | 看板加载，顶栏「待审批 2」 | 通过：同时出现单人 + 会签两张卡 |
| 2 | Phase 1 单卡展示 | 通过：高风险、项目/任务、原因、文件数、+1/-0、commit、意见框、批准/拒绝 |
| 3 | Phase 1 页面「批准合入」 | 通过：卡片消失、`RiskApprovalManager` 返回 approved；无 501/协议错位 |
| 4 | Phase 2 会签卡展示 | 通过：`fake1 · 0/2`、审批人白名单、无本地批准按钮、外部链接、刷新 |
| 5 | Phase 2 签名回调（2 个白名单 email） | 通过：`QuorumApprovalManager` 置 approved，卡片消失 |
| 6 | 重复回调 | 通过：HTTP 200，`status=duplicate`，不重复计数 |
| 7 | 设置页风险审批字段 | 通过：Provider、quorum、白名单、callback_url、轮询间隔、审批人映射均渲染 |
| 8 | 「使用当前访问域名」 | 通过：填入 `http://127.0.0.1:5124/api/risk-approval/callback/{provider}` |
| 9 | 高风险策略切换为 quorum + 保存 | 通过：`/api/state` 返回 `policy.high=quorum`、provider/quorum/白名单/callback 均落盘 |
| 10 | 非法 callback_url（http 非 localhost、无 `{provider}`） | 通过：后端 400，配置保持原有效值 |
| 11 | 完整任务链路（脚本化 Agent） | 通过：改 `orcsy/updater.py` → WIP `arranger/real-task@9d4b85a` → 风险卡 → 页面批准 → `OUTCOME=done`，merge commit `3c63c72`，文件变 `version = 2` |
| 12 | keep-alive 连续 POST | 发现并修复：第二个 POST 复用旧 body 导致 501；修复后回归通过 |

## 3. 测试中发现并修复的问题

- **看板 keep-alive POST body 错位**：`BaseHTTPRequestHandler` 在同一连接复用
  实例，`_raw_body_bytes` 缓存未按请求重置，第二个 POST 读到上一个请求的 body，
  剩余字节被当成下一请求行（`501 Unsupported method`）。已修复为每个 POST
  开始重置缓存，并新增 `tests/test_web_keepalive_post.py` 回归。

## 4. 未能实测的项目（及原因）

| 项 | 原因 | 现状/建议 |
| --- | --- | --- |
| 真实 GitLab/GitHub 审批工单 | 无真实仓库 token / 测试 issue 环境 | 适配器仅有 mock HTTP 单测；需在测试仓库用 `/approve`、`/reject` 验证 |
| 真实飞书/钉钉审批实例 | 无企业应用凭据、审批模板 | 适配器仅有 mock HTTP 单测；需确认 `approval_code`/`process_code`、表单字段、`originator_user_id` |
| 真实 LLM 任务 | 无可用模型密钥 | 使用脚本化 `LLMClient`；真实模型的工具调用/轮次未覆盖 |
| 公网 HTTPS 回调 + 反代 | 本机 localhost 测试 | 仅验证 localhost HTTP + 签名回调；TLS、反代、raw body 转发需部署后验证 |
| 真实 email 白名单身份 | 使用 fake 身份映射 | 未验证 GitLab username / GitHub login / 飞书 open_id / 钉钉 userid 的真实映射 |
| 飞书/钉钉 Webhook | 当前适配器仅轮询，回调明确 fail closed | 需后续事件订阅实现与验签 |
| 2D 内置账号/RBAC | 尚未实现（1.x） | 不属本次测试范围 |
| 多进程/重启后的 UI 复用 | 本次为单进程 UI 测试 | 持久化/重启复用已有单元测试覆盖，未做 UI 级重启演练 |
| 宽屏桌面布局 | 浏览器视口约 908px，命中移动端布局 | 设置页/待审弹窗已验证移动布局；宽屏布局未单独截图验证 |

## 5. 复现要点

- 测试实例使用独立临时目录与端口，不读写用户真实配置；
- Phase 1/2 种子请求通过真实 manager 创建；Phase 2 回调使用
  `sign_callback(secret, raw_body)` 生成 `X-Orcsy-Signature`/`X-Orcsy-Timestamp`；
- 完整任务链路使用脚本化 `LLMClient`，真实执行 `TaskExecutor.run_task`。
- 测试脚本位于临时目录，不随仓库提交；本报告只记录结果与边界。

## 6. 结论

Phase 1 与 Phase 2A/2B/2C 的**核心 UI、配置、审批状态机与 executor 接线**已在
真实看板 + 真实 HTTP 回调 + 脚本化 Agent 下跑通；测试还发现并修复了一个
keep-alive POST 的真实缺陷。

上线前仍需按 §4 用真实 GitLab/GitHub/飞书/钉钉凭据与真实模型做一轮供应商
侧冒烟，并验证公网反代下的回调可达性与验签。
