# 数据脱敏（发送模型厂商前本地掩除 + 写回还原闭环）开发方案

> 状态：**已定案**（2026-11-12 评审定案；两项产品决策已确认，v0.23.0 实施）
> 需求原文：**数据脱敏：密码、密钥等敏感信息先在本地处理，再发送给模型厂商。处理过程只运行在内存中不写数据到磁盘，处理完即清除。**
>
> **评审记录：**
> - 2026-11-12 决策一：默认**开启** + 高置信规则；键值对增强规则**默认关闭**（防误伤文档示例/测试夹具里的假密钥，导致模型读到被替换的字面量）。
> - 2026-11-12 决策二：模型把占位符写回磁盘（write_file/edit_file/multi_edit/run_command）时**落盘前还原原文**——否则"复制密钥到 CI 脚本""改 .env 整文件回写"类任务会产出占位符污染文件甚至真实数据丢失。

---

## 1. 需求解读

拆解为 5 条硬性要求：

| # | 要求 | 落点 |
|---|------|------|
| R1 | **敏感信息本地处理后再发送** | 唯一网络出口 `LLMClient._post` 入口处对 payload 脱敏后编码发送（llm.py:819） |
| R2 | **只运行在内存中** | 纯正则替换 + 实例字典映射，无临时文件、无持久化 |
| R3 | **不写数据到磁盘** | 映射表仅存于 `MaskingSession` 实例属性；日志只记命中次数，绝不记内容 |
| R4 | **处理完即清除** | `run_task_loop` 结束（finally）`session.clear()`；LLMClient 实例随任务结束被 GC |
| R5 | **不破坏 Agent 工作质量** | 占位符 `[REDACTED:类别:哈希]` 模型可读可引用；写回时还原原文（决策二） |

### 1.1 需求边界澄清（已定案）

1. **脱敏范围 = 发往模型厂商的数据**。看板 HTTP 响应的读侧掩码（H2 审计，`web.py:152 mask_secret`）已有完整体系，本功能不重复覆盖。
2. **本地对话历史保留原文**：`self.messages` 不动——护栏计数（重复读文件、连读熔断）依赖原文参数匹配，上下文裁剪基于原文长度估算；脱敏在 payload 副本上进行。
3. **默认态**（决策一）：`enabled=True` 高置信规则开箱即用；`enhanced_rules=False` 键值对规则默认关。
4. **写回还原**（决策二）：写盘类工具参数在 dispatch 前还原占位符为原文；llm.py:506 参数日志打在还原之前（日志只有占位符形态，不泄露原文）。
5. **热重载边界**：看板改规则后，进行中任务已建立的映射保持不变（占位符跨轮一致性优先）；新任务用新规则。

## 2. 现状梳理（改动锚点）

### 2.1 LLM 出站链路（现状）

```
Scheduler.run_cycle → ProjectAgent.scan_and_work (agent.py)
        │
        ├─[路径A 主Agent] TaskExecutor.run_task → _agent_loop
        │      └─ client = LLMClient(self.llm) (executor.py:447)
        │         └─ run_task_loop → _step_openai/_step_anthropic → _post
        ├─[路径B 审查Agent] review.review_task (review.py:102) → LLMClient → _post
        └─[路径C 子代理] subagent.py:113 LLMClient → _post
```

**唯一网络出口**：`LLMClient._post`（llm.py:819）。敏感文本只出现在：

- OpenAI 形态：`messages[].content`、`messages[].tool_calls[].function.arguments`
- Anthropic 形态：`system` 字符串、`messages[].content`（字符串或块列表：text / tool_result.content）

### 2.2 已有脱敏体系（读侧，不重复建设）

`mask_secret()`（web.py:152）看板掩码、三态保存、PBKDF2 密文存储、并发池键哈希（concurrency.py:184）。均不覆盖 Agent 消息链路（本功能目标面）。

### 2.3 可复用范式

| 范式 | 位置 | 用途 |
|------|------|------|
| `notify.configure()` 模块级热重载 | notify.py:36, web.py:208/1590/2429 | masking.configure() 直接仿照 |
| `/api/config` 分块保存 | web.py:2422-2429 notify 块 | masking 块保存样板 |
| 直构 AppState 安全自检 | tests/test_state_masking.py | 测试范本 |
| patch `llm.httpx.post` 集成 | tests/test_max_turns.py | 出站断言范本 |
| config.example.yaml 与 CONFIG_EXAMPLE 同步 | tests/test_config_example.py 强约束 | 新配置节双处同步 |

## 3. 总体设计

### 3.1 数据流（出站脱敏 + 返回侧闭环）

```
run_task_loop (llm.py:439)
  ├─ 模型返回 tool_calls → session.restore(args_json)        ← 返回侧：占位符→原文
  │    （还原在 dispatch_tool 前执行；参数日志打还原前形态）
  ├─ dispatch_tool → 工具结果回填 self.messages（本地历史=原文）
  ├─ 每轮 _step_* → _post(url, payload)
  │    └─ session.mask_payload(payload)                      ← 出站：原文→占位符
  │         （deepcopy 副本上遍历消息文本字段正则替换）
  │         → json.dumps → httpx.post → 模型厂商
  ├─ 响应（只含占位符，天然干净）→ 原样进 self.messages
  └─ finally: session.clear()                                ← 处理完即清除（R4）
```

### 3.2 占位符与映射设计

**占位符格式**：`[REDACTED:api_key:a1b2c3d4]`（类别 + 8 位 sha256 前缀）。

- **同值同占位符**：跨轮跨消息一致——模型可跨轮引用，写回还原闭环成立。
- **不嵌套幂等**：占位符形态不匹配任何脱敏正则，重复脱敏是恒等变换，历史重发无叠加。
- **restore 只认映射内占位符**：模型幻觉占位符原样透传不误还原。
- **映射**（MaskingSession 实例属性）：
  - `_rev: dict[原文 → 占位符]`（脱敏时查/建）
  - `_fwd: dict[原值 sha256(完整) → 原文]`（还原查表全哈希精确匹配；占位符只带 8 位前缀用于展示与查找起点，杜绝前缀相同原文不同的误还原）
  - 两表同步维护，仅存内存，不落盘不进日志。

### 3.3 规则体系（三层）

**层1 高置信（默认开）**——只匹配格式自证敏感的令牌：

| 类别 | 模式要点 | 类别名 |
|------|---------|--------|
| sk- 系 key | `sk-[A-Za-z0-9_-]{20,}` | api_key |
| GitHub PAT | `ghp_/gho_/github_pat_` 前缀 | api_key |
| GitLab PAT | `glpat-` 前缀 | api_key |
| AWS KeyId | `AKIA[0-9A-Z]{16}` | api_key |
| GCP key | `AIza[0-9A-Za-z_-]{35}` | api_key |
| Slack token | `xox[baprs]-` | api_key |
| JWT | `eyJ` 三段 base64url | jwt |
| PEM 私钥块 | `-----BEGIN ... PRIVATE KEY-----` 整块 | private_key |
| Bearer 头 | `Bearer <长token>` | bearer |
| URL 查询串令牌 | `access_token=/api_key=/token=<值>` | url_token |
| 连接串密码 | `scheme://user:<密码>@host` | db_password |

**层2 增强（默认关）**：键值对形态 `api_key=xxx`、`"password": "xxx"`、`password: xxx`、`TOKEN=xxx` 等（key 保留 value 掩除，值 ≥4 字符才掩）。

**层3 自定义**：用户正则列表（≤20 条、每条 ≤500 字符、加载期须可编译），整段匹配替换，类别名 custom。

**配置**（AppConfig.masking，MaskingConfig）：

```yaml
masking:
  enabled: true          # 总开关：发送模型厂商前本地脱敏
  enhanced_rules: false  # 键值对增强规则（误伤面大，默认关）
  custom_patterns: []    # 自定义正则列表（可空）
```

### 3.4 返回侧闭环专节

1. **响应无需反脱敏**：模型从未见过原文，只能引用占位符——响应天然干净，原样进历史。
2. **幂等性**：下一轮出站时，历史里的原文再脱敏（结果一致）、占位符不命中任何规则直接透传，无 `[REDACTED:[REDACTED:...]]` 叠加。
3. **占位符进结论/日志是期望行为**：finish 总结提到 `[REDACTED:api_key:a1b2c3d4]` 会写进任务文件——密钥不落盘正是需求效果；哈希后缀让模型能区分多个不同密钥。
4. **写回还原**（决策二）：模型在 write_file/edit_file/multi_edit/run_command 参数里写出占位符（如"把密钥复制进 CI 脚本"），dispatch 前统一 `restore()` 还原为原文——复制类任务正常完成，整文件回写不丢真实数据。还原对**所有工具参数**统一生效（语义即"模型要回真实值"），不只写盘工具。
5. **内存生命周期**：`run_task_loop` finally 中 `session.clear()`；映射仅存在于 LLMClient 实例属性，任务结束实例被 GC。崩溃/强杀时进程退出映射即消失——任何时刻磁盘上没有映射或原文副本。

## 4. 详细改动清单（文件级）

### 后端 orcsy/

| 文件 | 改动 |
|------|------|
| **orcsy/masking.py**（新增） | `configure(cfg)`/`enabled()` 模块级配置；`MaskingSession`（mask_text/mask_payload/restore/clear）；内置规则预编译；`validate_patterns` 供 config 复用 |
| **orcsy/config.py** | `MaskingConfig` dataclass + `validate_masking_patterns()` + AppConfig 挂载（:501 附近）+ load_config 解析（:1226 notify 块后仿写） |
| **orcsy/llm.py** | `__init__` 建 `self._masking = MaskingSession()`；`_post` 入口 `self._masking.mask_payload(payload)`（enabled 才生效）；run_task_loop 工具参数 dispatch 前 `restore`；finally `clear()` |
| **orcsy/web.py** | `/api/config` 加 masking 分块保存（仿 notify 块 :2422）；`full_state` 暴露 masking 节；AppState 初始化与 `_apply_config` 加 `masking.configure(...)`（:208/:1590） |
| **orcsy/__main__.py** | CONFIG_EXAMPLE 常量加 masking 节（与 config.example.yaml 逐字一致） |
| **config.example.yaml** | 同步加 masking 节 |

### 看板 dashboard/

| 文件 | 改动 |
|------|------|
| **dashboard/src/api.ts** | masking 类型（enabled/enhanced_rules/custom_patterns）+ config 提交类型 |
| **dashboard/src/views/SettingsView.vue** | SECTIONS 加 `masking` 分区；卡片仿 notify：两个 NSwitch + custom_patterns textarea + 保存按钮 + hint「本地内存处理、不落盘、写回自动还原」 |

### 文档与版本

| 文件 | 改动 |
|------|------|
| **docs/design-data-masking.md** | 本文档 |
| **docs/用户指南.md / user-guide.md** | 设置页新增「数据脱敏」章节（含返回侧闭环说明） |
| **README.zh-CN.md / README.md** | 功能简述一行 |
| **orcsy/__init__.py / pyproject.toml / README 徽章** | 0.22.0 → 0.23.0 |

## 5. 测试计划

沿用 tests/ 纯脚本风格（check_* + main() + uv run python）：

**tests/test_masking.py**：
1. 单元：每条高置信规则命中/不误伤（普通单词、示例文档 `sk-xxxx` 不命中）；增强开关两侧；自定义正则；enabled=false 全透传。
2. 闭环：同值同占位符、幂等无嵌套、restore 还原/未知占位符透传、clear 后映射失效。
3. payload：OpenAI/Anthropic 两形态脱敏，`self.messages` 历史保持原文。
4. 集成（仿 test_max_turns.py patch `llm.httpx.post`）：含假 `sk-` 密钥的读→写工作流，出站 body 含占位符无原文、落盘文件为还原原文。

**回归**：test_config_example.py（双处同步强约束）、test_state_masking.py、test_max_turns.py、e2e 系列。

## 6. 关键决策

| # | 决策 | 理由 |
|---|------|------|
| D1 | 唯一出口 `_post` 处脱敏，而非各调用点 | 主/审查/子代理三路径与未来新增路径天然全覆盖；单点可测 |
| D2 | payload 副本脱敏，历史保留原文 | 护栏计数/上下文裁剪依赖原文；重发历史幂等 |
| D3 | 高置信默认开、增强默认关 | 格式自证的令牌误伤趋近零；键值对规则误伤文档示例/测试夹具（评审定案） |
| D4 | 写回还原而非拦截 | 拦截会频繁打断"改配置/复制密钥"类任务；还原使闭环完整且不泄露（评审定案） |
| D5 | 占位符带 8 位哈希后缀 | 跨轮一致性 + 模型可区分多密钥 + 不嵌套幂等；还原走全哈希精确匹配 |
| D6 | 任务级 MaskingSession 而非全局单表 | 跨项目/任务隔离，攻击面最小；生命周期天然对齐任务 |

## 7. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 高置信规则漏检非标准格式密钥 | 层3 自定义正则兜底；设计文档明示规则清单供用户评估 |
| 增强规则误伤测试夹具 | 默认关闭（决策一）；hint 说明开启代价 |
| 占位符哈希前缀碰撞 | 还原走全哈希精确匹配，前缀仅展示；碰撞仅影响展示区分度不影响正确性 |
| 模型幻觉占位符格式 | restore 只认映射内条目，未知形态透传 |
| 脱敏后 token 估算偏差（占位符比原文短） | 忽略不计——偏差方向是估算偏保守，反而更安全 |
| 性能：每轮 deepcopy + 全消息正则 | 高置信规则只扫长文本消息；基准测试见 test_masking 性能检查（万字符消息 <10ms 量级） |

## 8. 迁移与兼容

- 默认开启对既有用户行为有变化：出站内容中格式自证的密钥被替换。看板设置页可一键关闭（enabled=false 完全透传，零开销路径）。
- 无 schema 迁移：配置节缺省即默认值；旧 config.yaml 无 masking 节照常加载。
- 看板 API 向后兼容：full_state 新增字段，前端旧版忽略未知字段。

## 9. 工作量估算

| 项 | 量级 |
|----|------|
| masking.py（规则 + Session） | ~200 行 |
| config.py 配置 + 校验 | ~40 行 |
| llm.py 接线 | ~10 行 |
| web.py 保存/展示/热重载 | ~30 行 |
| 前端分区 | ~80 行 |
| 测试 | ~250 行 |
| 文档 | 双指南各 1 节 |

## 10. 待确认问题

全部已定案（见评审记录）。遗留边界已知悉并接受：

- 热重载改规则后进行中任务映射保持不变（一致性优先）。
- 自定义正则的灾难性回溯风险由「加载期 re.compile 即报错 + 长度上限」缓解，不做运行时超时（模型请求本身有 timeout 兜底）。
