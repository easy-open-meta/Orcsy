# 通用 Harness 编码执行器接入方案（评估稿）

> 状态：设计提案，**尚未实施**｜2026-11-22  
> 目标：Orcsy 负责任务调度、隔离、验收和落地；外部 Harness 负责探索、编码和测试。  
> 关联：[`plan-v1-trusted-pipeline.md` §4.10](plan-v1-trusted-pipeline.md)、[`design-project-team.md`](design-project-team.md)、[`design-review-agent.md`](design-review-agent.md)。

## 1. 结论与范围

**建议实施，但以可选编码执行器接入，不替换内置 Agent，也不将 Harness 当作 LLM provider。** 第一阶段只支持本机非交互 CLI + Git worktree 内的代码任务；保留默认 `builtin`，按项目单兵或小组成员显式选择 `external`。Orcsy 仍负责扫描和 DAG/角色路由、租约与恢复、任务状态、worktree、系统计算的 diff、提交/审查/落地、RunLog 和停止。Harness 只交付工作目录中的改动与执行结果，不直接改变 Orcsy 任务状态或决定是否合并。

| 模式 | 适用 | 首期策略 |
| --- | --- | --- |
| 单兵 `builtin` | 现有默认全部任务 | 不变 |
| 单兵 `external` | 启用隔离的 Git 代码任务 | 可选；无隔离时前置拒绝 |
| 小组成员 `external` | `produces_code=true` 的开发成员 | 可选；PM、默认只读 QA、审查员等仍走内置执行路径 |
| 无 Git、嵌套 Git/gitlink、串行主工作区 | 无可靠的单 worktree 隔离与 diff 边界 | 首期拒绝外部运行，提示改用 builtin；后期单独设计 |

不做：远程 Harness 服务、多机器调度、替换审查模型、双向共享内置工具协议、在 CLI 内复刻 Orcsy 的 PM/QA/DAG、任意命令上传即运行、自动信任 Harness 自报的提交/测试/费用。该能力与技能库中“扫描外部 AI 工具的技能”不同。

## 2. 现状与接入点

- `scheduler.py` 按周期触发项目扫描；`agent.py` 负责依赖/角色筛选、租约认领、回写与归档。单兵和小组分别构建 `TaskExecutor`（`orcsy/agent.py:195`、`orcsy/agent.py:257`）。
- `TaskExecutor.run_task` 已包住停止信号、RunLog、预算、结果映射和审查/落地（`orcsy/executor.py:283`）；具体编码发生在 `_agent_loop`（`orcsy/executor.py:495`），内置调用 `LLMClient.run_task_loop`（`orcsy/executor.py:576`）。**替换编码阶段的策略选择，而非复制整条任务生命周期。**
- 代码任务完成后，Orcsy 自己执行 WIP 提交和审查门（`orcsy/executor.py:1015`、`orcsy/executor.py:1084`）。任务指纹目前包含任务、指南、模型、角色，不含 runner 配置（`orcsy/claims.py:196`）；切换 Harness 必须使租约/续跑身份变化。
- `LLMConfig.provider` 只描述模型 API 协议，不能用 `provider: harness` 表示外部执行。现有预留方案 `runner: external`（`docs/plan-v1-trusted-pipeline.md:380`）方向正确，但其中的 `agent_profiles` 示例不覆盖单兵配置、权限隔离、预算不可观测等问题，本方案补齐这些边界。

## 3. 权责与完整流程

```text
scheduler → ProjectAgent（DAG、角色、指纹/租约、状态 in_progress）
  → TaskExecutor（准备每任务 worktree、停止信号、RunLog）
    → runner=builtin: 现有 LLM 工具循环
    → runner=external: 校验适用范围/命令 → 生成上下文 → 启动 Harness → 收集事件/退出结果
  → Orcsy 核对工作树改动、认领状态和完成证据
  → Orcsy WIP commit → 可选独立 review → 合入或主工作区增量落地
  → ProjectAgent 回写任务状态/归档/释放租约
```

**结果真值**：退出码 0 仅表示 CLI 正常退出，不等于完成任务。`completed` 需结构化成功结论 **且** Orcsy 对有效工作区计算出可提交的代码改动；无改动按现有“代码任务无变化”逻辑 `blocked`，不得信任输出中的 `files_changed`、diff、commit hash 或“已通过测试”。完成后是否真正 `done` 取决于提交、审查和落地结果。退出非 0、输出不完整/无效、启动失败或超时均不能标 `done`；保留运行证据，按故障类型阻断/取消，是否清理 worktree 遵循明确的保留策略（见 §5）。

**审查边界**：现有审查模型未配置时审查会跳过，不得声称 Harness 成果默认“已独立审查”。若开启审查，保留原合并前审查与返工；外部 Harness 的实际模型身份可能不可知，不能单凭 CLI 名称证明审查模型不同。需要独立性硬约束的配置应要求可信模型标识并在运行时比对，未知或相同即阻断审查/合并（不要自动跳过审查继续合入）；这项守卫需要与现有“审查跳过”行为一同改造和测试。

## 4. 配置与适配协议

建议新增独立 `harnesses` 注册表、项目单兵 `runner`/`harness`，以及 profile/member 层 `runner`/`harness` 覆盖；默认值全为 `builtin`/空。配置形态示意（**目标 schema，不是当前版本可运行配置**）：

```yaml
harnesses:
  local-coder:
    executable: /opt/coder/bin/coder
    args: ["run", "--non-interactive", "--output", "jsonl"]
    input: stdin                 # stdin | prompt_file；由 adapter 而非 shell 拼装参数
    output: jsonl               # jsonl | text（text 首期仅可诊断，不自动判成功）
    timeout_seconds: 1800
    model_identity: ""          # 可验证时填写；不是 CLI 昵称
projects:
  - name: app
    path: /projects/app
    runner: external            # builtin | external；项目单兵生效
    harness: local-coder
    team:
      members:
        - id: backend
          runner: external
          harness: local-coder
```

`profile` 与 `member` 的 runner/harness 解析顺序建议为项目单兵基底 → profile → member；实际小组成员必须解析出一对合法值。`llm` 仍用于内置 PM/QA/审查等，不强制外部 runner 的编码模型等于项目 `llm`；项目基础模型配置及审查模型校验不得因外部模式静默失效。保存和编辑路径必须同时覆盖 `orcsy/config.py` 的加载/验证/序列化、`orcsy/settings_service.py`、`orcsy/web.py`、看板 `api.ts` 与两处编辑器；旧 YAML 与旧任务无需变更。示例配置及 CLI 内嵌样例须继续保持字节一致。

`HarnessContext`（进程内）：`run_id、task_id、role、project_root、work_root、任务正文/验收标准、上游摘要、AGENT.md 指引、续跑说明、超时、stop_event`。提示词显式说明只改 `work_root` 下代码、不修改任务文件与 Orcsy 元数据、不自行 commit/push；提示词不是权限边界。输入用 stdin 或 Orcsy 管理的临时文件（仅 prompt_file 模式，限制权限、退出时清理、避免文件名和日志泄漏正文）；可选将 `ORCSY_WORK_ROOT` 等非敏感标识作为环境变量。不要将 API Key、任务正文或环境密钥拼进命令行参数、异常字符串、事件日志或仓库内文件。

`HarnessResult`：`status=completed|blocked|error|cancelled`、简短摘要、诊断信息、可选可信用量（无数据记 `unknown`），内部再转换到现有 `AgentResponse`；`files_changed` 由 Orcsy 重算。JSONL 至少识别版本化的 `progress` 和唯一终结 `result` 事件；未知事件只截断留痕，不影响结论；末尾缺失/重复/非法终结事件视为协议错误。单行大小、总输出字节、队列容量和日志保留均应有上限；stdout/stderr 持续消费，不能因管道填满死锁。通用 CLI 无结构化协议时先以人工配置的 adapter 做映射，**不从普通文本中的“成功”推断 completed**；首期可先用假 CLI 与一个真实 CLI 验证 JSONL 契约。

启动时使用已校验的绝对可执行文件路径、参数数组 `shell=False`、`cwd=work_root`、最小环境变量和显式超时；Web/API 不能直接接受任意可执行命令作为任务参数。若支持不同 CLI 的输入/输出格式，用适配层或预置模板翻译到同一契约，不把某 CLI 私有事件格式写进 `TaskExecutor`。运行启动与关闭同样记录可执行文件版本/配置摘要（不记录密钥）。

## 5. 安全、预算与恢复：必须先解决的门槛

| 场景 | 方案与首期限制 |
| --- | --- |
| 文件/网络/命令权限 | 外部进程不受 Orcsy 内置文件工具沙箱、命令白名单、审批模式及应用 HTTP 出网白名单约束；worktree **不是** OS 沙箱。首期标为“可信本地 CLI / 高权限”，仅经管理员显式启用；如需对不可信 Harness 提供强隔离，另交付容器/OS 级目录与网络约束，不宣称现有审批/出网策略已覆盖子进程。 |
| 凭据 | 仅继承必需环境，允许显式列出的变量引用；不把 `config.yaml` 全量环境注入。注意 CLI 可能读取用户主目录凭据和自行联网，产品文案必须说明。对输出和异常沿用审计/RunLog 脱敏，测试包含 token 回显。 |
| 提交权限 | Orcsy 管理的 worktree 中 Harness 不应自行 commit/push/切分支；启动前/结束后核对 HEAD、分支与工作树，HEAD 漂移或 worktree 越界则阻断落地，不直接使用其自报 diff。OS 权限无法单靠提示词禁止其操作其他目录，强约束依赖沙箱。 |
| 预算 | `BudgetTracker` 目前依据内置 LLM 用量计量，外部 CLI 未必提供可信实时 token/成本。**硬预算启用且无法获得可靠流式用量/终止能力时拒绝启动**，不能填零成本继续执行；能够计量时接入相同硬闸并杀进程树。超时/资源上限与成本预算是不同保证。 |
| 停止与超时 | 复用任务 stop_event；定期轮询并终止整个进程树（参考 `orcsy/tools_cmd.py:282`），等待受限时间回收管道，落 `cancelled`/`timeout` 事件。服务关闭也必须回收子进程。 |
| 崩溃与续跑 | 运行启动/结束记 RunLog；进程重启后不能假设原 CLI 已退出，先用记录的进程身份检查/回收遗留子进程，再依既有任务恢复；严禁自动二次提交。因 CLI 失败/超时留下的未提交改动默认保留以供人工审查，不直接带入下一次自动执行，需显式重新入队或经可验证恢复步骤。 |
| 租约/指纹 | 把 `runner`、Harness ID、可执行文件与参数的稳定摘要纳入指纹（不含密钥），切换执行器不能复用旧 claim/review 续跑标记；WIP commit 前维持二次校验。 |

## 6. 分期实施与验收

| 阶段 | 改动 | 验收标准 |
| --- | --- | --- |
| M0 契约与安全基线 | 定稿 schema、能力矩阵、JSONL 结果协议、运行前检查；假 CLI fixture | 明确预算/隔离/审查无法保障时拒绝运行；不破坏旧配置 |
| M1 后端旁路 | `orcsy/harness.py` 负责进程与事件；`executor.py` 只选择编码策略；`agent.py` 传生效 runner；`claims.py` 指纹增量 | 假 CLI 成功、无改动、非零退出、无终结结果、超时/停止、异常输出和重启恢复均可解释；builtin 回归通过 |
| M2 配置与产品入口 | 配置加载/保存、Web 状态/保存、看板选择与预检、双语使用说明 | YAML/API/Web 往返不丢字段，密钥不出 API，旧配置继续是 builtin；无 Git/嵌套 Git 显示不可用原因 |
| M3 真实 CLI 验证 | 选择 **一个**有稳定非交互接口的 CLI 做 adapter，版本能力探测；再评估第二种 CLI | Windows/Linux 各跑一次真实工作树任务：编码→系统 diff→审查→落地；停机和凭据/网络提示与真实行为一致 |

测试建议新增 `tests/test_harness.py`（fixture CLI、进程树、协议、输出上限、预算闸、密钥脱敏）；扩展 `tests/test_team_config.py`、`tests/test_team_web_api.py`、`tests/test_task_dependencies.py`、`tests/test_task_stop.py`、`tests/test_review_config.py`、`tests/test_config_roundtrip.py`。验证 `python tests/run_all.py`、看板构建，并人工走单兵与小组并行 worktree、审查通过/拒绝、手动 requeue、服务重启四条链路。

## 7. 开发前需要明确的产品决策

1. **“通用”范围**：建议先定义为本机 CLI 的统一输入/输出契约，不承诺任意第三方 CLI 零配置可用；后续再做具名 adapter。
2. **部署信任等级**：是否只允许用户本机可信 CLI？若要求对第三方/远程不可信 Harness 保证文件与网络隔离，M0 必须先增加 OS/container 沙箱，工期与产品表述均改变。
3. **预算与审查强制性**：建议硬预算不可计量时 fail closed；审查要求独立模型时，无法确认 Harness 的模型身份也 fail closed。若业务接受“费用未知”和“身份未知”的风险，应以显式关闭相关硬保证的配置表达，不做隐式降级。
4. **首个验收 CLI**：建议选择已有稳定非交互 + 结构化输出 + 可中断能力的 CLI；以实际版本能力探测结果确定 adapter 细节，不预先把某厂商参数当作通用标准。

**粗估**：按一名熟悉项目的开发者，M0–M2 约 2–3 周，首个真实 CLI 的跨平台联调及安全测试另需约 1–2 周；若加入强 OS 沙箱、精准外部成本计量或远程 Harness，则需单独估算。此估计属于方案假设，不代表已完成开发或已有供应商兼容性验证。
