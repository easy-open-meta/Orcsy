# 方向方案核查报告与 V1「可信流水线」功能开发方案

> 核查对象：《Orcsy 可深化发展方向开发方案设计文档》V1.0 草案（2026-11-15，基线 V0.24.0）
> 核查时点：2026-11-15　|　基线代码：`main@b4350a6`（V0.24.0）
> 证据口径：A 官方文档 / 官方 API；B 仓库源码实证；C 运行时验证；D 权威二手；E 记忆与推断
> 定位：本文同时是**真实性评估报告**与**可执行的 V1 开发方案**。评估部分只判事实，方案部分不受原文档结构约束。

---

## 0 结论摘要

**一句话结论：原文档的「方向判断」基本成立，但「现状盘点」有 5 处硬错误、3 处把已实现能力当成缺口，以及 1 个高风险架构建议（替换内核为 adapter）需要改写；按修正后的范围，P0 四项是 10~11 周的真实工作量，不是 3 周。**

### 0.1 评估结论

| 维度 | 判定 | 说明 |
| --- | --- | --- |
| 方向（内网/离线 + 可信） | ✅ 成立，但需换差异轴 | 「可信」已被 11 万 star 的 Paperclip 做成免费基线（原子锁、预算硬闸、审批门、审计全部有），单靠「可信深度」不构成差异；真正的差异在离线零依赖、Windows 桌面形态、国产模型/镜像生态、脱敏与浏览器联调 |
| 现状盘点 | ⚠️ 部分失实 | 看板「无鉴权」、会话/锁定缺失、`depends_on_refs` 字段、企业版入口、star 数等与代码/官方 API 不符 |
| 缺口判断（P0） | ⚠️ 一半成立 | 预算硬闸、任务指纹/lease 确为空白；Run 留痕、幂等、审查门禁已有相当基础，应做「增量补齐」而非「从零建设」 |
| 改造顺序与工期 | ❌ 偏乐观 | P0-1~P0-4 全部落在 `executor/scheduler/web` 同一条链路，串行依赖强；90 天完成 4×P0 + 3×P1 对单人不可行 |
| 架构建议（adapter 替换内核） | ❌ 高风险 | Orcsy 的护城河恰在内核：团队角色、子代理、审批门、脱敏、CDP 浏览器、任务协议。整体替换会丢掉这些，应改为「旁路执行模式」 |
| 许可与商业化分析 | ✅ 成立 | Apache-2.0 允许闭源衍生与再许可、CLA 需 sublicense、商标不随协议授予，均正确 |

### 0.2 五个必须纠正的事实

1. **看板并非「无鉴权」**：V0.24.0 已有 PBKDF2 登录、7/30 天会话、失败锁定、Bearer/X-API-Key、CORS 与真实 IP 限速。
2. **「自动提交 Git」默认关闭**：`git.commit_and_merge` 默认 `false`，实际走「看板人工把关提交页」。
3. **任务依赖字段是 `depends_on`，不是 `depends_on_refs`**：后者只是 PM/QA 建单工具的**参数名**，写进 front-matter 会被忽略。
4. **状态机不是 `READY/CLAIMED/REVIEW`**：现网状态是 `open → in_progress → done / blocked / cancelled`，外加 `review_pending`、`review_round` 等 front-matter 标记；直接替换会打断看板、任务文件与全部既有测试。
5. **社区数据与页面入口不符**：Gitee API 当日返回 `stars_count=0`、`forks_count=1`（文档写作 1 Star / 0 Fork）；仓库内检索不到「企业版 / 高校版」入口。

### 0.3 方案要点（修正后）

- 保留原文档的**定位与北极星指标**，但把差异轴从「可信」改为 **「离线零依赖 + 国产化栈 + 桌面形态」**，可信能力作为入场券而非卖点。
- P0 重排为 **R1 运行台账 → R2 幂等与续跑 → R3 预算硬闸 → R4 熔断停机**（R1 是其余三项的证据基础）。
- P1 中 **看板审计** 保留（真正缺口只有「写操作审计」），**出网白名单** 改为「统一出口白名单 + 审计」而不是原文档的「默认 0 次外网请求」（与调用云端模型自相矛盾）。
- **harness adapter 降级为 P2 可选模式**：以 `runner: builtin | external` 旁路接入外部 CLI，不改内核循环；先在 1~2 个 CLI 上验证。
- 工期按 **14 周 / 1 名主程 + 0.5 名前端** 排；若只有 8 周，砍掉 R4 与 P2，保留 R1/R2/R3。

---

## 1 核查方法与证据口径

- 仓库事实一律以**当前分支源码**为准（`git log` 基线 `b4350a6`），给出文件与符号；行号会随提交漂移，故以符号名为准。
- 第三方事实以**当场访问的官方 API/文档**为准（GitHub REST、Gitee OpenAPI、Neo4j 官方文档）。
- 无法当场取证的（n8n 许可证页面本轮 404），标注「未核实」，不替代为记忆结论。
- 「规划值」（10 个团队、7×24 成功率 95% 等）不是事实命题，单独归类，不参与真假判定。

---

## 2 逐条事实核查

### 2.1 与仓库现状有关的断言

| # | 原文档断言 | 判定 | 证据 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | 基线为 V0.24.0 | ✅ 准确 | [B] `orcsy/__init__.py` `__version__`；`pyproject.toml` | — |
| 2 | 许可证 Apache-2.0 | ✅ 准确 | [B] `LICENSE` 首行 | — |
| 3 | 看板默认 `127.0.0.1:5116` | ✅ 准确 | [B] `orcsy/config.py` `WebConfig.port = 5116`；`config.example.yaml` | — |
| 4 | 四种部署方式（Windows 安装包 / 源码 / Docker / systemd） | ✅ 准确 | [B] `build/`、`Dockerfile`、`deploy/linux/orcsy.service`、README | — |
| 5 | 定时扫描 `tasks/`，按优先级派发，每项目一 Agent，worktree 内执行 | ✅ 准确 | [B] `orcsy/scheduler.py`、`agent.py`、`worktree.py`、`task_model.py` | — |
| 6 | PM / 前后端 / QA / 审查 Agent 分工 | ✅ 准确 | [B] `orcsy/review.py`、`templates/roles/*.md`、`prompts.py` | — |
| 7 | 看板「仅回环且**无鉴权**」 | ❌ 不准确 | [B] `orcsy/auth.py`（PBKDF2-HMAC-SHA256，10 万次迭代）；`web.py` `SESSION_TTL / SESSION_TTL_REMEMBER / login() / _login_locked_until`；`config.example.yaml` `web.auth_password / api_keys / cors_origins / trusted_proxies` | 「无鉴权」只对「空密码 + 仅回环」这一种默认形态成立，不能概括产品现状 |
| 8 | host 非回环且密码为空时拒绝启动、拒绝保存 | ✅ 准确 | [B] `orcsy/web.py`（bind guard）、`tests/test_web_bind_guard.py` | — |
| 9 | 「扫描任务 → 自动提交 Git 并回写状态」 | ⚠️ 部分准确 | [B] `orcsy/config.py` `GitConfig.commit_and_merge: bool = False`；`resolve_commit_and_merge`；README「人工把关提交页」 | 自动提交是可选项且默认关闭；概述句按默认行为写会误导读者 |
| 10 | 密钥不出本地、数据脱敏 | ✅ 准确 | [B] `orcsy/masking.py`；`llm.py` 出站脱敏会话；README「密钥不出本地」 | 注意：`api_key` 本身明文存于 `config.yaml`（README 已声明），「不出本地」≠「不落盘」 |
| 11 | 单实例、实例间无内建协调机制 | ✅ 准确 | [B] `scheduler.py` 仅进程内 `_cycle_lock` | 无跨进程/跨实例租约 |
| 12 | 社区 1 Star、0 Fork | ❌ 不准确 | [A] Gitee OpenAPI `stars_count: 0`，`forks_count: 1`（检索时点 2026-11-15） | 数字颠倒；此类「自嘲式数据」失实会削弱文档可信度 |
| 13 | 页面存在「企业版 / 高校版」入口，README 未说明 | ❌ 未证实 | [B] `website/*.html`、`README*.md`、`docs/*.md` 全量检索无「企业版 / 高校版 / Enterprise」 | 可能来自推广物料或旧版页面；当前仓库与官网静态页均无 |
| 14 | 「上一轮结论」放弃按实例数切分版本（四条判断） | ⚠️ 无法核实 / 判断类 | [E] 仓库无对应决策记录 | 作为前提可在评审中确认，不宜写成既有结论 |
| 15 | 状态机为 `PENDING/READY/CLAIMED/RUNNING/REVIEW/DONE/FAILED/BUDGET_EXCEEDED` | ❌ 不准确 | [B] `task_model.py` 与 `config.py`：`VALID_STATUS = (open, in_progress, done, blocked, cancelled)`；`review_pending`、`review_round`、`requeue_seq` 为附加标记 | 状态机可以「映射」，不能「替换」；否则看板筛选、旧任务文件、`tests/test_claim_order.py` 等全部受影响 |
| 16 | 任务 front-matter 使用 `depends_on_refs` | ❌ 不准确 | [B] `task_model.py` 解析 `meta.get("depends_on")`；`depends_on_refs` 仅是 `create_tasks` 工具参数（`prompts.py`） | 按文档示例写 YAML 会导致依赖静默失效，是最容易踩的坑 |
| 17 | `risk`、`review_required` 为可选 front-matter | ⚠️ 目标而非现状 | [B] `task_model.py` 无这两个字段 | 可作为新增字段，但需先定义语义（当前 `produces_code` 已按角色决定是否审查） |
| 18 | 预算硬闸（token / 成本上限、进程终止） | ✅ 缺口成立 | [B] `orcsy/usage.py` 只有 `record()/summary()`，无阈值与终止路径 | 台账是现成的数据基础 |
| 19 | 任务指纹 / lease / checkpoint 全缺 | ⚠️ 部分成立 | [B] `agent.py` 启动恢复（`in_progress` 重置）、`review_pending` 补审扫描、`requeue_seq` 优先级、worktree 保留策略 | 已有「粗粒度续跑」，缺「细粒度幂等」：无指纹、无 TTL 租约、提交前无二次校验 |
| 20 | 审查不通过不产生合并提交 | ✅ 已实现（文档当成待做） | [B] `review.py`（独立模型、只读工具集）；`config.py` `max_review_rounds=2`；executor 审查失败保留 WIP 与 worktree | 该条应从「P0 改造」降为「已有能力，补验收用例」 |
| 21 | 审查不通过后「自动回滚」 | ❌ 未实现 | [B] `executor.py`：审查 fail/error 时保留 WIP 分支与 worktree 供返工；无 revert 流程 | 现状是「隔离 + 返工」，比回滚更安全，不建议改成自动回滚 |
| 22 | 连续失败熔断、自动停机 | ✅ 缺口成立 | [B] `scheduler.py` 无失败计数/暂停开关；仅任务内 5 重循环熔断 | 需项目级熔断器 |
| 23 | 逐轮 stdout / 工具调用 / token / 模型调用写入 RunLog | ⚠️ 部分已有 | [B] 任务文件 `append_progress`、`logs/orcsy.log`（含模型思考，`llm.py` `_clip_thinking`）、`logs/usage.jsonl`（token/角色/kind）、审查结论 | 已有「分散留痕」，缺统一 `run_id`、diff/test 工件与回放接口 |
| 24 | Harness adapter 抽象缺失 | ✅ 缺口成立 | [B] `llm.py` 既做协议适配又做 Agent 循环；无外部 harness 概念 | 但见 3.5，不建议替换内核 |
| 25 | 出网默认关闭、白名单外 0 请求 | ❌ 不准确 | [B] `hosts.py`（本地调试主机白名单、SSRF 硬禁区）、`tools_net.py` `_assert_public_url`、`mirrors.py`；模型端点是用户显式配置 | 现状是**局部防护**，没有统一出口闸；且「默认 0 次外网请求」与「调用云端模型」互斥，表述需重写 |
| 26 | 看板缺会话过期、失败锁定、写操作审计 | ⚠️ 只对一半 | [B] 会话 TTL 与登录锁定已有（`web.py`）；**写操作审计确实没有**（全仓无 `audit` 模块） | P1-3 应缩为「写操作审计 + 会话吊销 + API Key 权限最小化」 |

### 2.2 与外部事实有关的断言

| # | 断言 | 判定 | 证据 | 说明 |
| --- | --- | --- | --- | --- |
| 27 | Paperclip：MIT、自托管、Node/React、约 11 万 star、无付费层、agent 经 adapter 接入 | ✅ 准确 | [A] GitHub API：`paperclipai/paperclip`，MIT，TypeScript，114,065 stars，18,920 forks | 文档低估了它：其 README 明确列出 Claude Code / Codex / Cursor / Gemini CLI / OpenCode 等 adapter，以及 execution locks、预算 warning + hard stops、review/approval stages、durable audit log、rollback |
| 28 | Paperclip 把原子任务锁、预算硬闸、审批门、不可变审计做成免费基线 | ✅ 准确 | [A] 同上（README「Open source. Self-hosted. No Paperclip account required.」） | **这是本方案最重要的战略事实**：原文档想建立的「可信」护城河，竞品已是免费基线 |
| 29 | Neo4j：社区版适合单实例，企业版才有集群 / 在线备份 / RBAC / LDAP | ✅ 准确 | [A] Neo4j Operations Manual, Introduction（EE 明确含 clustering、online backup、RBAC、LDAP） | 用来论证「集群才是技术壁垒」成立 |
| 30 | n8n 使用 Sustainable Use License，社区版几乎不阉割 | ⚠️ 未核实 | [A] 本轮访问 `docs.n8n.io/sustainable-use-license/` 与 `n8n.io/legal/` 均未取到许可证正文 | 该论断在业界共识层面成立，但本轮未当场取证，标注存疑 |
| 31 | 改证后分叉案例（Elastic→OpenSearch、Terraform→OpenTofu、MySQL→MariaDB） | ✅ 准确 | [D] 公开事实 | 作为论据可用 |
| 32 | Apache-2.0 允许闭源衍生与再许可、不限制部署数量、CLA 需 sublicense、不授予商标 | ✅ 准确 | [D] Apache License 2.0 文本第 4 条与第 6 条 | 结论正确；建议在正式稿附条款编号 |

### 2.3 目标与规划类内容（不判定真假，只判可行性）

| # | 内容 | 评估 |
| --- | --- | --- |
| 33 | 90 天完成 P0 四项 + P1 三项 + P2 接口 | ❌ 偏乐观。P0 四项全部改 `executor/scheduler/web/llm` 主链路，且 R1 是 R2/R3/R4 的前置；按单人估算 10~11 周，加回归与文档 12 周起 |
| 34 | 连续 7×24、任务成功率 ≥95% | ⚠️ 可用，但需要先定义「任务成功率」（含 blocked 是否计入）与统计口径，否则不可复算 |
| 35 | 崩溃恢复「三处注入、0 重复提交」 | ✅ 可验收，且可自动化（现有 `tests/e2e_fake_llm.py` 提供假模型夹具） |
| 36 | 出网「默认 0 次白名单外请求」 | ❌ 不可验收（无法用单测证明进程级网络隔离）；应改为「统一出口 `egress` 检查覆盖率 100% + 审计计数 0」 |
| 37 | 10 个真实试用团队 | ⚠️ 属运营目标，与开发方案解耦；当前社区基数（0~1 star）下 90 天完成概率低 |
| 38 | V1.0 版本号 | ⚠️ 建议缓用。仓库处于 0.x 快速迭代（0.23.1 → 0.24.0 仅数日），1.0 意味着 API/配置冻结承诺；建议 0.25→0.29 逐项交付，1.0 作为「连续 4 周 dogfooding + 配置 schema 冻结」后的独立决策 |

---

## 3 对原方案定位的评估

### 3.1 成立的部分

1. **「先让一台机器跑到敢关页面」是正确的产品顺序。** 现有代码也印证这一点：单进程、文件状态、本地看板、托盘常驻，没有任何分布式组件。加集群等于换赛道。
2. **不按实例数收费、不动 Apache-2.0 的判断正确。** 协议层面无法限制部署数量，改证的分叉风险有充分先例。
3. **P2「只留只读聚合契约」的克制是对的。** 先留边界，不写协调逻辑。
4. **向后兼容承诺的方向正确。** 尤其「新增字段全部可选、不改 `depends_on` 语义」——但原文档自己的示例违反了这条（见 #16）。
5. **北极星指标（连续无人值守天数）比 star 数更贴合产品本体。**

### 3.2 不成立或需要换轴的部分

**「可信」不再是差异点，只是入场券。** Paperclip 已把 execution locks、预算 hard stop、审批阶段、durable audit、rollback 全部免费提供，且支持 6+ 家 harness。若 Orcsy 的对外叙事是「我们更可信」，用户会直接问「为什么不直接用 Paperclip」。

真正尚未被覆盖、且与 Orcsy 现有代码强耦合的差异轴是：

| 差异轴 | 现状证据 | 竞品覆盖情况 |
| --- | --- | --- |
| **离线/内网零依赖** | 单 Python 进程 + 标准库密码学 + 文件状态，无 Postgres/Redis/Node 运行时 | Paperclip 需 Node 服务 + Postgres，属「可自托管」而非「可离线」 |
| **Windows 桌面形态** | 安装包、托盘、开机自启、零 UAC 自更新（`build/`、`tray.py`、`autostart.py`、`updater.py`） | 竞品以 Docker/Linux 为主 |
| **国产模型与镜像生态** | 国产 OpenAI 兼容端点、`mirrors.py` 镜像测速、`env-setup` 静默装链 | 英文产品基本不覆盖 |
| **出站脱敏** | `masking.py` + 出站会话 + 占位符写回还原 | 竞品未强调 |
| **浏览器联调闭环** | 纯 Python CDP（`orcsy/browser/`）+ `dev_server_*` + `http_request` 主机白名单 | 多数竞品只给代码编辑 |
| **多角色小队 + DAG + 只读子代理** | 团队角色模板、`depends_on`、`delegate_explore` 五重熔断 | Paperclip 有 org chart，但无「PM 拆单 → 前后端并行 → QA 提单 → 审查」同构流水线 |

因此修正后的定位句建议为：

> **能在断网内网、Windows 工作站上用国产模型跑完「拆单 → 开发 → 联调 → 审查 → 提交」的无人值守研发流水线，代码与密钥不出网。**

「可信」（留痕、幂等、预算、熔断）作为支撑该定位的**工程底座**，不单独当卖点。

### 3.3 关于「审查门禁已存在」这一事实的连锁影响

原文档把「审查不通过不合并」列为 P0-3 的验收要点，但该能力自 `review.py` 起就已存在，并且附带返工上限（`max_review_rounds`）、独立模型强制不同（`assert_review_model_distinct`）、补审扫描。这意味着：

- P0-3 应缩为「项目级熔断 + 停机原因可解释 + 人工恢复入口」；
- 原文档中「自动回滚」与现状的「保留 WIP + 返工」冲突。建议**不改现状**：保留 WIP 才是可审计的（回滚会销毁证据），与「可信」目标一致；
- 省下的约 1.5 周应投入 R1 的 diff/test 工件归档。

---

## 4 V1「可信流水线」功能开发方案（修订版）

### 4.1 范围

**本期做**：R1 运行台账与回放、R2 幂等与续跑、R3 预算硬闸、R4 熔断停机、R5 写操作审计、R6 统一出口白名单；P2 预留只读聚合。

**本期不做**：多实例/集群、SSO/RBAC、低代码编排、新增 Agent 角色、更换许可证、以 star 为目标的运营动作、替换内核为 adapter。

### 4.2 现有能力盘点（禁止重造）

| 能力 | 现状位置 | 本期动作 |
| --- | --- | --- |
| 任务状态与 DAG | `task_model.py`（`open/in_progress/done/blocked/cancelled`、`depends_on`） | 只加可选字段，不改状态域 |
| Worktree 生命周期 | `worktree.py`（setup/cleanup/remove/prune） | 接入 run 事件，不改语义 |
| 审查门 | `review.py`、`executor._complete_with_review` | 记录审查事件与工件 |
| 用量台账 | `usage.py`（`usage.jsonl`，含 kind/role/agent_name） | 复用为预算计量的数据源 |
| 脱敏 | `masking.py`、`llm.py` 出站会话 | RunLog 写盘前复用同一套 sanitize |
| 进度留痕 | `executor.append_progress`（写任务文件） | 保留；RunLog 为并行旁路，不替代 |
| 模型思考日志 | `llm.py`（`_clip_thinking`） | 保持；改为同时写 run 事件 |
| 登录与会话 | `auth.py`、`web.py`（TTL/锁定/Bearer） | 只补审计与吊销 |
| 启动恢复 | `agent.py`（`in_progress` → `open`）、`review_pending` 补审 | 增强为「有 WIP 则续作，不重放」 |

### 4.3 R1 运行台账与回放（P0，3 周）

**目标**：任一 `run_id` 能在看板还原「谁在什么时间、用哪个模型、调了什么工具、改了什么、测试结果、审查结论、花了多少 token」。

> **实现状态（2026-11-15，分支 `feat/trusted-pipeline-r1`）**：已落地 `orcsy/runlog.py`、
> executor/llm/review 事件接入、`/api/runs` 三只读端点、任务详情「运行记录」回放；
> 覆盖测试 `tests/test_runlog.py`（含 HTTP 路由）与 `tests/e2e_fake_llm.py`（真实执行链路）。
> 2026-11-16 追加：目录迁移（`last_root` / journal / 活跃 run 合并 / 进度与 UI 锁定），
> 设计见 [`docs/design-runlog-dir-migration.md`](design-runlog-dir-migration.md)；
> R2 任务指纹 / 租约 / 断点续跑 / 提交前二次校验已实施，设计见
> [`docs/design-task-claims.md`](design-task-claims.md)。
> 本小节的解释性文字保留，作为后续 R2~R6 的契约依据。

**落盘位置**：默认在配置目录下（`<配置目录>/runs/<project>/<run_id>/`），**不写进受管仓库**。理由：

- 受管仓库可能是用户的生产仓库，写 `.orcsy/` 需要改 `.gitignore`，且非 git 项目同样需要留痕；
- 现有 `usage.jsonl` 已采用「配置目录/logs」模式（`usage.py: usage_store_path`），保持一致；
- 需要「证据随仓库走」时，用可选 `runlog.scope: repo` 写入 `<repo>/.orcsy/runs/` 并自动追加 `.gitignore` 条目。

目录结构：

```
<data_dir>/runs/<project>/<run_id>/
  meta.json        # run_id / task_id / fingerprint / role / model / 起止 / exit_reason
  events.jsonl     # append-only，seq 单调递增
  diff.patch       # 任务工作分支相对基线的 diff（截断上限可配）
  test.log         # run_command 中测试类命令的输出摘要
  review.json      # verdict / issues / review_model / tokens
  usage.json       # 本 run 的 token 与折算成本
```

事件类型（`events.jsonl` 每行一条）：

```json
{"seq": 12, "ts": "2026-11-15T10:03:21+08:00", "type": "tool_call",
 "actor": "backend", "data": {"name": "edit_file", "ok": true, "ms": 42,
 "args_digest": "sha256:…", "args_preview": "src/app.py"}}
```

`type` 枚举：`run_start` / `claim` / `turn` / `model_call` / `tool_call` / `progress` / `review_start` / `review_verdict` / `commit` / `merge` / `run_end`。`args_preview` 必须过 `masking`，只留文件名/命令名，不落完整参数。

**改造点（按文件）**：

| 文件 | 改动 |
| --- | --- |
| `orcsy/runlog.py`（新增） | `RunRecorder`：`start()/event()/artifact()/finish()`；按 retention 清理；线程安全；写盘失败只告警（沿用 usage 的旁路容错哲学） |
| `orcsy/executor.py` | `run_task()` 首尾开/关 run；`_agent_loop()` 的进度、工具调用、审查、提交处发事件；`LLMError/TaskStopped/异常` 三个出口写 `exit_reason` |
| `orcsy/llm.py` | `LLMClient` 增加可选 `event_sink` 回调（避免反向依赖 runlog）；每轮写 `model_call`（模型、tokens、耗时、是否截断） |
| `orcsy/review.py` | 审查结束写 `review_verdict` 与 `review.json` |
| `orcsy/task_service.py` | 状态变更写 `claim`/`run_end`（人工改状态也留痕） |
| `orcsy/web.py` | 新增只读 API（见 4.9） |
| `dashboard/src/views/DashboardView.vue` | 任务抽屉新增「运行记录」Tab：timeline + diff + review + usage |
| `dashboard/src/api.ts` | 新增 runs 客户端 |

**验收**：用 `tests/e2e_fake_llm.py` 跑一轮，断言 `meta.json`、`events.jsonl` 的 seq 连续、`diff.patch` 非空、`review.json` 存在；检索任意 `run_id` 可还原 6 类事件；日志与事件中不出现 `sk-` 明文（复用 `tests/test_masking.py` 的断言方式）。

**风险与对策**：事件量（每轮 1~3 条，单任务数百条）→ 单文件上限 + 超限滚动；`diff.patch` 可能很大 → 默认 2MB 截断并标注；磁盘增长 → 默认保留 30 天 / 单项目 500MB，可在设置页调。

### 4.4 R2 幂等与断点续跑（P0，3 周）

**目标**：同一任务重复扫描不重复执行；进程在任意阶段被杀后重启，不重复提交、不残留脏 worktree。

**任务指纹**：

```
fingerprint = sha256(
  规范化任务正文（front-matter + 正文，统一换行/去尾空白）
  + 排序后的 depends_on 指纹
  + sha256(AGENT.md)
  + agent_profile 名 + 生效 model 标识
)
```

只存 hash，不存原文；任务文件被人工编辑即指纹变化，允许重新执行（符合直觉）。

**租约（lease）**：

```json
{"task_id": "T-101", "project": "app1", "fingerprint": "…",
 "run_id": "20261115-100321-T-101-a1b2", "owner": "pid:1234@host",
 "acquired_at": 1763200000, "heartbeat_at": 1763200030, "ttl": 900}
```

- 存 `<data_dir>/state/projects/<project>.json`，进程内 `threading.Lock` + 文件级排他（Windows `msvcrt.locking` / POSIX `fcntl`）+ 原子替换写；
- 心跳由执行线程每 30s 更新；`CLI` 模式下同样生效（不依赖 Web）；
- 过期租约可被回收，回收记 `claim` 事件（`data.reclaimed=true`）。

**续跑决策表**（重启时）：

| 现状 | 决策 |
| --- | --- |
| `in_progress` + 无 WIP 提交 + 无 worktree | 重置 `open`，重新执行（现有行为） |
| `in_progress` + 有 WIP 提交 + worktree 存在 | 保留 worktree，置 `open` 且打 `resume_run`，从续作提示开始（不重放已完成轮次） |
| `review_pending` + 工作分支存在 | 只补审查（现有 `review_pending` 扫描，增强为按 run 事件判断是否已审过） |
| 提交前（已拿到 commit hash 未写状态） | 以 `commit` 事件为准，直接回写 `done`，不重复提交 |

**提交前二次校验**：在 `commit`/`merge` 前重算 fingerprint；若与租约不一致或租约已易主，中止并保留 WIP，写 `exit_reason=stale_lease`。

**验收（可自动化）**：

1. 同一任务连续扫描 100 次（假模型）→ 1 次提交、1 条 `done`、1 个 run；
2. 在 `RUNNING` / `REVIEW` / 提交前三个点用 `ORCSY_TEST_CRASH_AT` 注入 `os._exit(1)` → 重启后无重复提交、`git status` 干净、worktree 可清理；
3. 人工改任务正文 → 指纹变化 → 允许重跑（不应被租约挡住）；
4. 旧任务文件（无新字段）解析与执行不变（回归 `tests/test_task_dependencies.py`、`tests/test_claim_order.py`）。

### 4.5 R3 预算硬闸（P0，2 周）

**目标**：超预算时停止执行并留下可解释状态，不再出现「一觉醒来账单翻倍」。

**配置**（全局 + 项目覆盖）：

```yaml
budget:
  enabled: false                # 默认关闭，避免默认行为突变
  max_tokens_per_task: 300000
  max_tokens_per_project_per_day: 5000000
  max_cost_per_task_cny: 3.00   # 需配 prices 才生效
  prices:                       # 每百万 token 的人民币单价（按模型名匹配）
    deepseek-chat: {prompt: 1.0, completion: 2.0}
    glm-4-plus:   {prompt: 5.0, completion: 5.0}
```

计量点：`LLMClient` 每轮结束读 `usage`（任务 + 子代理合并，沿用 `executor` 的 `_subagent_usage` 合并逻辑），累计到 `BudgetTracker`。

**执行语义**：

1. **软停**：项目当日预算耗尽 → 调度器不再领取新任务，看板显示「预算用尽（至 00:00 重置）」，正在跑的任务继续；
2. **硬停**：单任务超限 → 抛 `BudgetExceeded`（继承 `TaskStopped` 语义，工具循环边界生效）→ 终止子代理 → 保留 WIP → 任务置 `blocked`，front-matter 写 `block_reason: budget_exceeded`，run 写 `exit_reason=budget_exceeded`；
3. **不改状态域**：不新增 `BUDGET_EXCEEDED` 状态（会打断看板筛选、`VALID_STATUS` 与旧任务兼容），以 `block_reason` + outcome 区分。原文档要的是「可区分、可统计、不被 `failed` 淹没」，`outcome/block_reason` 已满足；
4. **先估后调**：调用前用 `max_tokens` 做上界预估，已超限则不发请求（避免「最后一次调用又烧一笔」）。

**验收**：设 1000 token 上限 → 1 轮内停止；`usage.jsonl` 有 `outcome=blocked` 且 `block_reason=budget_exceeded`；任务未归档、WIP 保留；下一次调度不领取该项目任务。

### 4.6 R4 熔断与停机（P0，2 周）

**目标**：系统性故障（模型 401、端点全挂、审查连续不通过）不再「安静地跑一整夜」。

**配置**：

```yaml
resilience:
  max_consecutive_failures: 3      # 同项目连续失败阈值
  window_minutes: 120              # 统计窗口
  max_failure_rate: 0.6            # 窗口内失败率
  min_samples: 5                   # 低于此样本量不判失败率
  auto_resume: false               # 默认人工恢复
```

**计数口径**（写入 state 文件与 run 事件）：`llm_error`、`blocked`（非用户取消）、`review fail` 计为失败；`open/reassigned/wait_retest/cancelled` 不计。

**触发后**：

- 项目进入 `paused`，调度器跳过领取（已有任务继续跑完）；
- 看板顶部横幅显示：触发原因、阈值、影响的任务、最近 5 条失败 run 链接；
- `POST /api/project/resume` 人工恢复（写审计事件）；`auto_resume: true` 时在下一个窗口清空计数后自动恢复（默认关闭，避免掩盖系统性故障）；
- 暂停状态持久化，重启后仍暂停。

**与审查门的关系**：审查不通过**不回滚**、不产生合并提交（现状已满足）；连续审查失败计入熔断计数。若用户显式配置 `on_review_fail: reset`，才清理 WIP（默认 `keep`）。

### 4.7 R5 写操作审计（P1，1 周）

**目标**：谁在什么时候改了任务/配置/提交，可追溯。

- `logs/audit.jsonl`：`ts / actor（user|api_key:…|scheduler|agent）/ ip / action / target / diff_digest / result`；
- 覆盖动作：登录成功/失败、任务增删改与状态变更、设置保存、`/api/project/diff` 提交确认、API Key 调用、项目增删、强制停止；
- 参数只记摘要与关键字段（复用 `masking`），不记密钥与完整任务正文；
- 看板「设置 → 审计」只读列表 + 按 actor/action 过滤；仅登录用户可见。

**与现有会话机制的关系**：会话 TTL、失败锁定、Bearer、CORS 均已存在，**不重做**；本期只补：会话主动吊销（改密码后使旧会话失效，现有 `_invalidate_sessions` 已具备，暴露到设置页即可）与审计。

### 4.8 R6 统一出口白名单（P1，3 周）

**目标**：把「会出网的地方」收敛到一个阀门，并留下可核对记录。

**现实约束（重要）**：Agent 的 `run_command` 可以启动任意子进程（`npm install`、`pip`、`git fetch`），**进程级网络隔离不可能由应用层保证**。因此本项只承诺：

1. 应用自身发起的 HTTP（模型调用、Issue 平台、镜像测速、网页抓取、更新检查）**全部**经 `orcsy/egress.py` 判定；
2. 白名单 = 显式配置的模型端点 host + Issue 平台 host + 镜像源 host + `egress.extra_hosts`；
3. 非白名单拒绝并写审计；默认 `mode: allowlist`；
4. 子进程出网不拦截，但 `env` 注入的代理与镜像、以及 run 事件中的命令名可审计。

```yaml
egress:
  mode: allowlist      # allowlist | off（off 仅用于排障）
  extra_hosts: []
  audit: true
```

**验收**：单测遍历 20 个 URL 断言 allow/deny；配置 DeepSeek 端点后 `llm` 请求通过、未配置的 `evil.example` 被拒且审计有记录；文档中删除「默认 0 次外网请求」这类不可验证表述。

### 4.9 P2 预留：只读聚合契约

`GET /api/state/export?project=&since=` 返回只读聚合（任务状态计数、run 摘要、用量、熔断状态），**不提供**跨实例调度、锁或写入。固定 schema 版本号，便于未来控制平面消费，但本期不实现任何协调逻辑。

### 4.10 外部 Harness（P2 可选，4 周，独立开关）

**不改内核，只加旁路。** 现有 `llm.py` 的 Agent 循环承载了团队角色、子代理、审批、脱敏、浏览器工具，替换为 adapter 等于砍掉护城河；正确做法是增加一种任务执行方式：

```yaml
agent_profiles:
  claude-runner:
    runner: external                 # builtin（默认）| external
    command: ["claude", "-p", "{prompt_file}", "--output-format", "stream-json"]
    timeout_seconds: 1800
    capabilities: {stream_events: true, interrupt: true, self_diff: false}
```

`orcsy/harness.py` 协议：`spawn(ctx) -> process`；`ctx` 含 worktree、任务正文、上游摘要、环境变量（密钥以引用注入，不写明文）；事件解析优先 JSONL，否则按行落 `stdout`；退出码 `0/非0`，另留 `78`（预算）、`79`（需人工）；超时用进程树终止（复用 `tools_cmd` 的实现）。外部执行**同样过审查门与 RunLog**，diff 由系统计算，不信任 harness 自报。

**分期**：先 Claude Code CLI + 通用 CLI 两个；Codex/OpenCode 仅配置接入，不写死版本矩阵；每个 harness 配一条契约测试（假二进制回放固定输出）。

### 4.11 数据模型与接口

**任务 front-matter（全部可选，旧任务零修改）**：

```yaml
---
priority: P1
status: open
depends_on: [T-102]        # 注意：不是 depends_on_refs
review_required: true       # 新增；false 仅对只读角色生效（PM/QA 验证）
budget:
  max_tokens: 20000
  max_cost_cny: 3.00
---
```

`review_required: false` 的安全边界：写代码的角色（backend/frontend/qa-开发态）忽略该字段，仍强制审查；避免「一键关审查」被误用。

**新增只读 API**：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/runs?project=&task_id=&limit=` | run 列表（摘要） |
| GET | `/api/runs/{run_id}` | meta + events（分页） |
| GET | `/api/runs/{run_id}/artifact/{name}` | `diff.patch` / `test.log` / `review.json`（白名单 + 大小上限） |
| GET | `/api/audit?action=&actor=&limit=` | 审计列表 |
| POST | `/api/project/resume` | 熔断后人工恢复（写审计） |
| GET | `/api/budget/status` | 当日/当日各项目预算余量 |

**配置新增段**：`runlog`、`budget`、`resilience`、`egress`（均带默认值，旧配置加载不受影响；`config.example.yaml` 同步注释）。

### 4.12 兼容性与迁移

- 任务文件：不新增必填字段，不改 `depends_on` 语义，不新增 `status` 取值；
- 端口、四种部署方式、`AGENT.md`、看板既有 API 不变；
- 新增状态目录与 run 目录在配置目录内，不污染受管仓库；
- 旧 `usage.jsonl` 继续读写（新字段缺省兼容，沿用 `kind` 的兼容写法）；
- 配置加载对未知键保持现状（警告 + 忽略），新增段缺失走默认值；
- 升级后首次启动自动建 `<data_dir>/state/` 与 `<data_dir>/runs/`，失败只告警不阻断启动。

### 4.13 测试与验收

沿用仓库「直跑式」测试风格（`main()` + `assert`，无 pytest），新增：

| 测试文件 | 覆盖 | 关键断言 |
| --- | --- | --- |
| `tests/test_runlog.py` | 事件 seq、截断清理、脱敏、工件读取 | 无 `sk-` 明文；超限删除最旧 |
| `tests/test_claims.py` | 指纹稳定性、租约 TTL、并发领取、易主 | 10 线程并发领取同一任务只有 1 个成功 |
| `tests/e2e_crash_recovery.py` | 三处 `os._exit(1)` + 重启 | 1 次提交、无脏 worktree、run 记录唯一 |
| `tests/test_budget.py` | 任务/项目/日窗、预估拦截、软停 | 超限 1 轮内停止；`block_reason` 正确 |
| `tests/test_resilience.py` | 连续失败、窗口失败率、恢复、重启保持 | 第 3 次失败后不再领取；resume 后可领取 |
| `tests/test_egress.py` | 白名单、拒绝、审计 | 未配置 host 被拒且计数 +1 |
| `tests/test_audit.py` | 登录/任务/设置/提交动作 | 每条有 actor 与 ip；密钥不入库 |
| `tests/test_harness.py`（P2） | 假 CLI 契约、超时、退出码 | 超时杀进程树；diff 由系统计算 |

回归：`uv run python tests/run_all.py`（前端改动后另跑 `npm run build` + `uv run python tools/gen_page.py`）。

### 4.14 实施计划（14 周，1 主程 + 0.5 前端）

| 里程碑 | 周期 | 交付 | 退出标准 |
| --- | --- | --- | --- |
| M0 基线冻结 | 第 1 周 | 修正文档事实错误；冻结新特性；定义 run/event schema | 评审通过 schema；`run_all` 全绿 |
| M1 运行台账 | 第 2–4 周 | `runlog.py` + executor/llm/review 挂钩 + 回放 API/视图 | `test_runlog` 通过；任意 run 可还原 |
| M2 幂等续跑 | 第 5–7 周 | 指纹、租约、续跑决策表、提交前校验 | 100 次扫描 1 次提交；三处崩溃 0 重复 |
| M3 预算硬闸 | 第 8–9 周 | `budget.py` + 软停/硬停 + 看板余量 | `test_budget` 全绿；超限不吞成 failed |
| M4 熔断停机 | 第 10–11 周 | `resilience.py` + 暂停横幅 + 人工恢复 | 连续失败自动暂停且可解释 |
| M5 审计 + 出口 | 第 12–13 周 | `audit.jsonl`、`egress.py`、设置页 | 20 URL 判定用例；写操作 100% 留痕 |
| M6 外部 harness（可选） | 第 14 周起 | `harness.py` + 2 个 CLI + 契约测试 | 假 CLI 回放通过；不破坏 builtin |

**若只有 8 周**：做 M0–M3，砍 M4 的自动恢复与 M5 的 egress（保留审计），P2 全部延后。理由：R1/R2/R3 直接决定「敢不敢关页面睡觉」，R4 依赖 R1 的失败计数，可通过人工看日志过渡。

### 4.15 版本与发布

- 0.25.0：R1；0.26.0：R2；0.27.0：R3；0.28.0：R4；0.29.0：R5/R6；
- 1.0 的门槛：连续 4 周 dogfooding 无「重复提交/预算越界」事故、配置 schema 冻结、`run_all` 与移动端回归全绿。**不按日历定 1.0。**

### 4.16 风险与止损

| 风险 | 影响 | 对策 | 止损线 |
| --- | --- | --- | --- |
| RunLog 拖慢主链路 | 任务变慢、用户关掉 | 写盘异步队列 + 失败降级；单事件 <1KB；压测单任务开销 <3% | 开销 >10% 则改采样 |
| 租约误伤（时钟/心跳） | 任务被抢或永久锁死 | 单调时钟计时；TTL 3 倍心跳；看板可手动释放 | 连续 2 次误抢则回退为「仅指纹校验」 |
| 预算估算偏差 | 误杀正常任务 | 默认关闭；预估用上界但不以预估判死，只拦「已确定超限」 | 误杀率 >5% 则只用实际值 |
| 熔断导致整夜空转 | 无人值守失效 | 默认人工恢复 + 通知；横幅给出原因与一键恢复 | 用户反馈「太吵」则默认改 `auto_resume` |
| 外部 harness 版本漂移 | 维护成本失控 | 只做 2 个 + 契约测试；版本不匹配直接报错不猜 | 1 个季度内无使用则移除 |
| 范围膨胀 | 14 周失守 | 每里程碑冻结范围；P2 不实现 | 连续两周延期则砍 M6 |

---

## 5 对原文档的修改清单（可直接替换的表述）

| 原文位置 | 建议改为 |
| --- | --- |
| 1.1「仅回环且无鉴权」 | 「默认仅回环；设置密码后启用 PBKDF2 登录（7/30 天会话、失败锁定），非回环地址强制要求密码或 API Key」 |
| 1.1「自动提交 Git 并回写状态」 | 「回写任务状态为默认行为；自动提交/合并由 `git.commit_and_merge` 控制（默认关闭，走看板人工把关提交页）」 |
| 1.1「1 Star、0 Fork」 | 按检索时点写「0 Star、1 Fork」，或删除该行（对方向判断无贡献） |
| 1.1「页面存在企业版/高校版入口」 | 删除或改为「官网规划中的高校版（仓库内暂无入口）」 |
| 3.4 状态机表 | 增加「与现网状态映射」列：`PENDING→open`、`CLAIMED→in_progress(租约)`、`REVIEW→review_pending`、`BUDGET_EXCEEDED→blocked+block_reason` |
| 4.4 front-matter 示例 | `depends_on_refs:` 改为 `depends_on:`；并注明 `depends_on_refs` 是 `create_tasks` 工具参数 |
| 4.1「run 记录位于 `.orcsy/`」 | 改为「默认配置目录下 `runs/`，可选 `runlog.scope: repo` 写入仓库 `.orcsy/` 并自动 ignore」 |
| 4.5「替换为 harness adapter」 | 改为「保留 builtin 内核，新增 `runner: external` 旁路；外部执行同样过审查与留痕」 |
| 6.「出网默认 0 次白名单外请求」 | 改为「应用自身 HTTP 出口 100% 经 `egress` 判定；子进程出网不保证拦截，以审计与代理/镜像约束」 |
| 7.「阶段一 1–3 周完成 RunLog/StateStore/lease/预算」 | 改为「M1–M3 共 8 周（1 主程）」，阶段三顺延至 14 周 |

---

## 附录 A 核查证据清单

| 等级 | 来源 | 用途 |
| --- | --- | --- |
| A | GitHub REST `api.github.com/repos/paperclipai/paperclip`、`search/repositories` | Paperclip 许可、star/fork、语言 |
| A | `github.com/paperclipai/paperclip`（README） | 自托管、adapter 列表、execution locks / budgets / approval / audit / rollback |
| A | Neo4j Operations Manual, Introduction | 社区版单实例 / 企业版集群、在线备份、RBAC、LDAP |
| A | Gitee OpenAPI `gitee.com/api/v5/repos/java-and-net/Orcsy` | stars 0 / forks 1 / Apache-2.0 / 描述 |
| B | `orcsy/__init__.py` | 版本 0.24.0 |
| B | `orcsy/auth.py`、`orcsy/web.py`（`SESSION_TTL*`、`login`、`_login_locked_until`） | 鉴权、会话、锁定 |
| B | `orcsy/config.py`（`VALID_STATUS`、`GitConfig.commit_and_merge`、`ReviewConfig.max_review_rounds`） | 状态域、提交默认值、返工上限 |
| B | `orcsy/task_model.py`（`depends_on` 解析） | 依赖字段命名 |
| B | `orcsy/review.py`、`orcsy/executor.py` | 审查门、审查失败保留 WIP |
| B | `orcsy/usage.py` | 台账只记录不限额 |
| B | `orcsy/hosts.py`、`orcsy/tools_net.py`、`orcsy/mirrors.py` | 局部出网防护 |
| B | `orcsy/scheduler.py`、`orcsy/agent.py` | 无项目级熔断；启动恢复 |
| B | `config.example.yaml` | 现有配置项与默认值 |
| D | Apache License 2.0 第 4/6 条 | 闭源衍生、再许可、商标 |
| E | n8n 许可证（本轮未取到正文） | 标注存疑，未作为结论依据 |

## 附录 B 与原文档的关系

- 保留：定位句、北极星指标、非目标清单、Apache-2.0 与 CLA 判断、P2 只读契约、向后兼容承诺。
- 修订：现状盘点 5 处、P0 范围与顺序、P1-3 缩范围、P1-2 改写、adapter 降级、工期从 90 天改为 14 周、版本号策略。
- 新增：R5 写操作审计、R6 统一出口、续跑决策表、预算状态映射、可自动化验收用例、止损线。

相关文档：[`docs/roadmap.md`](roadmap.md)（功能演进）、[`docs/architecture.md`](architecture.md)（分层与模块边界）、[`docs/design-review-agent.md`](design-review-agent.md)（审查门）、[`docs/design-mobile-remote-access.md`](design-mobile-remote-access.md)（移动端与鉴权基线）。
