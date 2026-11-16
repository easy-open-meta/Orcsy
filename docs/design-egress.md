# 统一出口白名单（egress）设计

> 版本：V1-R6　|　落地：2026-11-16　|　模块：`orcsy/egress.py`
> 关联：`docs/plan-v1-trusted-pipeline.md` §4.8、`docs/design-audit.md`

## 1. 目标与边界

**目标**：把「应用自身发起的 HTTP」收敛到一个阀门，并留下可核对记录。任一请求
要么命中白名单，要么被拒绝并写审计；没有第三条路。

**明确不做（重要）**：

- **不承诺进程级网络隔离**。Agent 的 `run_command` 能启动任意子进程
  （`npm install`、`pip`、`git fetch`、`curl`），应用层无法拦截子进程的 socket。
  本项只约束应用自身用 `httpx` / `urllib` 发起的请求；子进程出网依靠
  `env` 注入的代理/镜像与 run 事件里的命令名做审计与约束。
- **不替代 SSRF 防护**。`web_fetch` 的 `_assert_public_url`（逐跳解析、拒绝内网/
  元数据地址）仍在白名单之后执行：白名单决定「是否允许访问互联网上的这台主机」，
  SSRF 校验决定「这个主机解析出来的 IP 是否允许被访问」。
- **不拦截浏览器导航**。`browser_navigate` 是 Agent 驱动本地 Chrome/Edge 的调试
  工具，目标受 `local_debug.allowed_hosts` 约束，不属于「应用自身 HTTP」。

## 2. 判定模型

```
URL ──► 归一化 host ──► mode=off ? ──是──► 放行（rule=off，不写拒绝审计）
                          │否
                          ▼
                 loopback（127.0.0.1 / localhost / ::1）──是──► 放行（rule=local）
                          │否
                          ▼
                 命中白名单规则？──是──► 放行（rule=derived / extra / runtime）
                          │否
                          ▼
                  denied++ / last_denied / audit(egress.deny) / EgressDenied
```

- **回环不是出网**：本机 dev_server 就绪探针、浏览器 CDP、测试桩都在回环，
  永远放行，避免「白名单把自家看板/调试链路锁死」。
- **私网地址不自动放行**：`10.x` / `192.168.x` / `169.254.x` 等只有写进配置
  （模型端点、Issue `api_base`、`egress.extra_hosts`）才放行；`web_fetch`
  另有 SSRF 硬边界，即使加白也不会访问云元数据地址。
- **未知协议**（`ftp://` 等）不会被 `httpx` 请求；判定按主机处理，不在白名单
  即拒绝。

## 3. 白名单来源（自动推导 + 显式追加）

| 来源 | 取值 | 说明 |
| --- | --- | --- |
| 模型端点 | `llm.base_url`、`review.llm.base_url`、`agent_profiles[].llm.base_url`、`projects[].llm.base_url`、`projects[].team.members[].llm.base_url` | 换模型/加项目后自动生效，无需重复抄主机名 |
| Issue 平台 | `projects[].repo.url`、`repo.api_base` | 支持自托管 Gitea/GitLab 与测试桩 |
| 镜像源 | `mirrors.BUILTIN_MIRRORS` 全部内置源 + `mirrors.custom` + `mirrors.disabled` | 内置源是固定清单；自定义/禁用源来自用户配置 |
| 更新源 | `update.repo` | 官方默认 Gitee 源也在其中 |
| 额外主机 | `egress.extra_hosts` | 网页抓取文档站、企业内网代理等 |
| 运行时 | 更新发布 API 返回的 `download_url` / `sha256_url` 主机 | 仅本次进程有效，见 §6 |

匹配语义：

- 条目只填主机名，可带端口：`api.example.com`、`api.example.com:8443`；
- `*.example.com` 同时匹配主域 `example.com` 与任意子域 `a.b.example.com`；
- 带端口的条目只匹配该端口；不带端口的条目匹配任意端口；
- 主机名统一小写、去末尾点；IPv6 用 `[::1]` 形式。

## 4. 配置

```yaml
egress:
  mode: allowlist      # allowlist（默认）| off（仅排障：全放行、不写拒绝审计）
  extra_hosts: []      # 追加主机；可带 :端口 或 *.通配
  audit: true          # 拒绝时写 logs/audit.jsonl（action=egress.deny）
```

- 旧配置没有 `egress` 节：按默认值加载，**旧行为不变的前提是模型/Issue/镜像/
  更新源都在上面自动推导范围内**；`web_fetch` 访问未登记的第三方文档站会被拒，
  需要显式加白（这是 R6 的预期行为变化）。
- 看板「设置 → 工具与网络 → 出网白名单」可视化编辑；保存走既有 `/api/config`
  分块保存，`_apply_config` 里 `init_egress(new_cfg)` 热生效。
- 设置页同时显示：推导出的主机数、进程内放行/拒绝计数、最近拒绝、运行时放行
  的更新 CDN 主机（只读）。

## 5. 各出网点接入

| 文件 | 调用点 | purpose | 拒绝后的表现 |
| --- | --- | --- | --- |
| `llm.py` | `LLMClient._post`（对话/连接测试）、`list_models` | `model_chat` / `model_list` | `LLMError` → 任务 blocked / 看板提示 |
| `issues.py` | `_get_json` | `issues` | `IssueError` → 看板 400 |
| `updater.py` | `check_update`、`download_installer` | `update_check` / `update_download` | `UpdateError` → 看板提示 |
| `mirrors.py` | `speedtest_one` | `mirror_speedtest` | 该源记不可达，不中断批量测速 |
| `tools_net.py` | `tool_web_fetch` + `_fetch_with_guard` 每一跳 | `web_fetch` | 工具返回错误文案（含加白路径） |

`web_fetch` 的重定向逐跳复检：白名单与 SSRF 校验都在每一跳重新执行，防止
「白名单页面 302 到未登记主机」。

## 6. 更新包 CDN 的运行时放行

Gitee/GitHub Release 的附件常落在与 API 不同的 CDN 域。`check_update` 在成功
解析发布 API 响应后调用 `allow_runtime_host(download_url, purpose="update_asset")`：

- 只放行该响应里给出的下载/校验文件主机，来源本身已通过白名单；
- 条目存活于本次进程（跨 `init_egress` 重建保留），并写
  `egress.runtime_allow` 审计；
- 不是「永久加白」：重启后重新经发布 API 获取，避免配置被静默写脏。

## 7. 审计

拒绝时写 `<配置目录>/logs/audit.jsonl`：

```json
{"ts":"2026-11-16T10:03:21+08:00","action":"egress.deny","actor":"system:egress",
 "ip":"","target":"evil.example","result":"denied",
 "detail":"{\"purpose\": \"model_chat\", \"url\": \"https://evil.example/v1/chat\", \"mode\": \"allowlist\", ...}"}
```

- `target` 只记主机名；`detail.url` 去掉 query/fragment（`access_token` 不落盘），
  再走 `audit.redact_text`；
- `egress.audit: false` 时不写拒绝审计（计数与最近拒绝仍在内存）；
- 写审计失败只告警，**绝不改变判定结果**（与 R5 旁路哲学一致）。

## 8. 兼容与降级

- 新增 `egress` 配置节全部有默认值；旧版本读到未知节会忽略，不影响启动。
- 降级到旧版本后：旧版本不认识 `egress` 节，看板保存可能把它写丢（与 `runlog`
  同类风险）；出网行为回到「无统一闸门」的旧状态。需要保留配置时，降级前备份
  `config.yaml`，且不要在旧版本里保存设置。
- `mode: off` 是排障逃生门：模型端点写错、企业代理需要临时放行时先关闸定位，
  排查完改回 `allowlist`。

## 9. 测试

`tests/test_egress.py`：

1. 20+ URL 判定矩阵：模型端点 / 通配主域与子域 / 端口限定 / 回环 / 未登记 /
   相似域名（`api.deepseek.com.evil.example`）/ 私网与元数据地址；
2. `off` 模式全放行；
3. 拒绝写审计，query 中 token 不落盘，`denied` 计数与 `last_denied` 正确；
4. 配置读写与推导：模型 / Issue / 更新源 / 内置与自定义镜像均进入白名单；
5. 出网点接入：`LLMClient._post` / `list_models` 拒绝且**不发起 HTTP**，
   `web_fetch` 返回加白提示，`speedtest_one` 返回 None，`check_update` /
   `_get_json` 抛领域错误；
6. 更新 CDN 运行时放行 + 审计 + 重建策略后仍保留。

## 10. 已知限制

| 限制 | 说明 / 后续 |
| --- | --- |
| 子进程出网不拦截 | 应用层无解；靠镜像/代理 env 与 run 事件命令名审计，文档不承诺「0 次外网请求」 |
| 更新包下载的重定向不逐跳复检 | `httpx.stream(follow_redirects=True)`；下载源是已放行的发布 API 给出的地址，风险可接受；如需更严可改为手动重定向循环 |
| 白名单按主机名，不绑定 IP | DNS 解析结果不受控；`web_fetch` 有 SSRF 逐 IP 校验，模型/Issue/更新源属于用户显式配置的信任目标 |
| 代理环境变量可改变实际出口 | `trust_env` 行为保持原样；本模块管的是「请求 URL」，不管代理链路 |
| 运行时 CDN 主机只存活本次进程 | 重启后重新从发布 API 获取；不写入 `config.yaml` |
