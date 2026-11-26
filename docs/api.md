# Orcsy 看板 HTTP API 契约（1.0 稳定化 S3）

> **冻结范围**：本文件列出的端点属于 1.0 起向后兼容的稳定面——字段只增不改名，
> 删除/改名需先经过 deprecation 窗口（至少一个大版本）。未列出的端点（内部
> 调试、实验特性）不在契约内，随时可能变化。
>
> 状态：v0.28.0（对应路线图 S3）

## 通用约定

- **认证**：登录后获得两套等价凭据，任选其一随请求携带：
  1. `Authorization: Bearer <token>`（看板把 token 存 localStorage/sessionStorage）；
  2. HttpOnly Cookie（登录时由服务端 Set-Cookie，浏览器自动携带）。
  未认证访问受保护端点返回 `401`，响应体 `{"ok": false, ...}`。
- **限流**：登录失败按真实客户端 IP 熔断（X-Forwarded-For 链以受信代理解析，
  见 `web.trusted_proxies`）；锁定期间 `POST /api/login` 返回 `401`，响应
  `{"ok": false, "message": "尝试次数过多，账号已临时锁定，请 15 分钟后再试"}`。
- **请求体**：均为 `application/json`；错误返回 `{"ok": false, "message": "<原因>"}`，
  校验类错误为 `400`，冲突（任务乐观锁）为 `409`。
- **免登录端点**：`/`、`/index.html`、`/api/auth`、`/api/system/endpoints`、
  `/manifest.webmanifest`、`/appicon.png`、`/sw.js`、`/offline.html`。

## 稳定端点（冻结）

### 状态与会话

| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/api/auth` | GET | 认证状态：`{"ok", "app", "enabled", "authed"}`；看板启动首查 |
| `/api/login` | POST | `{password, remember_me?}` → `{ok, token, expires_in}`；失败按 IP 熔断 |
| `/api/logout` | POST | 注销当前会话 |
| `/api/state` | GET | 全量看板状态（项目/任务/用量/配置回显）；`?summary=true` 移动端轻量摘要 |
| `/api/config` | POST | 分块保存设置（`{llm?, scheduler?, web?, ...}`；键缺失=保持现值） |

### 调度与任务

| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/api/scan` | POST | 立即触发一轮调度扫描 |
| `/api/task` | POST | 新建/编辑任务：`{project, id(空=自动), title, priority, status, description, assignee?, depends_on?, base_version?, force_overwrite?}`；`id+base_version` 不符 → `409` |
| `/api/task/status` | POST | 任务状态变更 |
| `/api/task/delete` / `/api/task/clear` | POST | 删除 / 清空任务 |
| `/api/task/reorder` | POST | 任务排序 |
| `/api/task/stop` | POST | 强制停止执行中的任务 |
| `/api/project` | POST | 新建/编辑项目（含小组、专属模型/审查覆盖；`original_name` 供改名） |
| `/api/project/delete` / `/api/project/discard` / `/api/project/resume` | POST | 移除 / 丢弃 / 熔断恢复 |
| `/api/project/commit` | POST | 人工把关提交：`{project, message, push?}` |

### 连接与系统信息

| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/api/system/endpoints` | GET（免登录） | 可达地址清单与鉴权开关（连接向导用；不含凭据） |
| `/api/system/qr` | GET（需登录） | `?url=` → SVG 连接二维码 |

### 变更语义承诺

- `POST /api/config` 的**键缺失 = 保持现值**（全部分区一致）——看板分块保存
  与第三方脚本的局部更新都依赖此语义，变更需走 deprecation 流程。
- 任务状态域固定为 `open / in_progress / done / blocked / cancelled`。
- `/api/task` 的乐观锁：编辑已有任务时可携带最近一次 `GET /api/task`
  返回的 `version_token` 作为 `base_version`；提供基准且版本不符时返回
  `409` 与最新任务内容（`latest_task`）。未提供基准时直接保存；
  `force_overwrite: true` 可跳过版本比对。

## 已知非契约端点（会变，勿依赖）

`/api/llm/*`、`/api/mirror/*`、`/api/skill/*`、`/api/update/*`、`/api/browse`、
`/api/mkdir`、`/api/artifact`、`/api/audit`、`/api/egress`、`/api/guide`、
`/api/approval`、`/api/risk-approval`、`/api/risk-approval/callback/*`、
`/api/runlog/*`、`/api/dashboard/*`、`/api/backup`、
`/api/archive` 等仍按 0.x 节奏演进，
1.0 后逐一评估是否纳入冻结面。
