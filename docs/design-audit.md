# 写操作审计设计（V1-R5）

> 版本 V1.0 ｜ 2026-11-16 ｜ 关联：`docs/plan-v1-trusted-pipeline.md` §4.7
> 状态：已实施（分支 `feat/trusted-pipeline-r5`）

---

## 1 目标

让「谁在什么时候、从哪台机器、改了什么」可追溯，且不把敏感信息写进日志。

范围：登录成功/失败、退出、任务增删改/状态/排序/停止、设置保存、
项目增删改/提交确认/改动丢弃/Issues 拉取/熔断恢复、审批响应、技能/镜像/
更新/自启等运维动作、静态 API Key 的写调用。

非目标：读请求全量记录（状态轮询会造成噪音）、请求体/任务正文留档、
跨实例聚合审计（P2 控制平面预留）。

---

## 2 落盘与格式

`<配置目录>/logs/audit.jsonl`，append-only，一行一个 JSON：

```json
{"ts":"2026-11-16T14:26:42+08:00","action":"task.save","actor":"user",
 "ip":"192.168.1.20","target":"demo/T-101","result":"ok",
 "detail":"{\"status\":200,\"keys\":[\"description\",\"id\",\"project\"]}"}
```

- 单文件超过 5MB 轮转为 `audit-YYYYmmdd-HHMMSS.jsonl`，保留最近 5 份；
- 写失败只告警，绝不影响业务响应；
- 查询只读尾部 512KB，支持 `action` / `actor` 子串过滤，最多返回 1000 条。

---

## 3 主体（actor）与 IP

| 凭证 | actor |
| --- | --- |
| 会话 Cookie / Bearer 会话 Token | `user` |
| 静态 API Key（`X-API-Key` 或 Bearer 里是 key） | `api_key:<sha256 前 8 位>` |
| 未开启鉴权 | `local` |
| 登录失败 | `anonymous` |

IP 走与登录限速相同的 `get_client_ip`：直连取对端 IP；只有对端在
`web.trusted_proxies` 内才采信 `X-Forwarded-For`，防伪造与防误锁。

---

## 4 记录内容约束

- `detail` 只放状态码、白名单键名等摘要；任务正文、密码、api_key、
  仓库 token、请求体原值一律不写；
- `target` 与 `detail` 统一过 `runlog.redact_text`（复用脱敏规则）；
- API Key 只记哈希前缀，原始 key 不落盘，也不回显到看板；
- 登录失败只记原因（密码错误/锁定），不记输入值。

---

## 5 代码接入点

| 位置 | 改动 |
| --- | --- |
| `orcsy/audit.py` | `AuditLog`（记录/轮转/尾部查询）、配置目录锚定、模块单例 |
| `orcsy/web.py` | `_json` 记录状态码；`do_POST` finally 统一按路径映射审计；登录/退出显式记录；`GET /api/audit` 只读查询 |
| 前端 | 设置页「审计日志」：动作/操作者过滤、刷新、最近 300 条 |
| `scheduler.py` / `AppState` | 启动时初始化审计路径 |

### 动作映射（节选）

`auth.login` / `auth.logout` / `task.save` / `task.status` / `task.reorder` /
`task.stop` / `task.delete` / `task.delete_batch` / `task.clear` /
`settings.save` / `project.save` / `project.delete` / `project.commit` /
`project.discard` / `project.git_init` / `project.issues_sync` /
`project.resume` / `approval.respond` / `skill.*` / `mirror.apply` /
`app.autostart` / `app.restart` / `update.apply` / `update.install` /
`runlog.migration_retry`。

401 未认证请求不重复记审计（登录失败单独记录），避免噪音。

---

## 6 测试

`tests/test_audit.py`：

1. 记录/倒序/action/actor 过滤；
2. 敏感文本脱敏（假密钥不落盘）；
3. 超限轮转与备份数量上限；
4. Web 登录失败/成功、任务创建与状态变更、设置保存、API Key 写调用；
5. 审计查询接口需要登录、action 过滤生效；
6. 日志全文不含登录密码和 API Key 原文。

---

## 7 已知边界

- 只审计「经由 Orcsy API/看板」的写操作；直接改 `config.yaml` 或任务文件
  不会留痕（Git/文件系统是最终事实源）；
- 审计文件不加密、不签名；需要防篡改时应配合只读挂载或外部日志采集；
- 登录失败记录包含 IP 与原因，不包含密码；锁定细节仍由登录限速状态机维护；
- 读请求（含 API Key 轮询）不逐条记录，避免把审计文件变成访问日志。
