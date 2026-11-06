# Review Agent（任务归档前审查）开发方案

> 状态：**已实施**（v3 终审定案；全部里程碑 M1-M4 完成，18 项测试 + 看板构建通过）
> 需求原文：**所有项目默认配置 review agent，在每个任务完成后进入对归档任务前进行审查，该 agent 模型不能与任务 agent 为同一个。**
>
> **修订记录：**
> - 2026-11-06 评审一：**D2 定案**——审查 Agent 默认**不配置**，默认态不审查不告警；已归档任务**不追溯**。**D1 定案**——审查时机改为**合并前**（通过才 merge 主分支）。
> - 2026-11-06 评审二：**D3 定案**——返工次数默认跟随全局设置页，项目级可单独覆盖（`review_max_rounds`）。**v2 增强全部批准纳入本期**：①审查 agent 可跑测试命令（`review.allow_test_commands`）；②结构化审查意见（file/line/severity，看板渲染）；③返工任务通过审查后 `merge --squash` 收敛 WIP 提交（`review.squash_merge`）。

---

## 1. 需求解读

需求拆解为 4 条硬性要求（全部已定案）：

| # | 要求 | 落点 |
|---|------|------|
| R1 | **所有项目纳入审查范围** | 审查模型配置一次即对全部项目默认生效（项目级可关）；但**默认不预置**审查模型——默认态不审查（评审一定案） |
| R2 | **每个任务完成后、归档前进行审查** | 审查门在**合并主分支之前**执行（评审一定案）；通过后才归档到 `tasks/done/` |
| R3 | **审查结论必须真实影响归档**（R2 隐含） | 审查不通过不得合并、不得归档；未审查不得当通过（默认未配置态除外，见 §3.2） |
| R4 | **审查 agent 模型 ≠ 任务 agent 模型** | 模型互斥硬约束：配置校验（保存/加载拒绝冲突）+ 运行时守卫（冲突时跳过并告警，不让模型自审自批） |

术语约定：

- **任务 agent**：现有 `TaskExecutor` + `LLMClient` 工具循环，负责改代码的执行模型（生效配置 = `resolve_llm(全局 llm, 项目 llm 覆盖)`）。
- **审查 agent（review agent）**：新增的独立审查角色，只读检查（可选跑测试验证）任务成果并给出 通过/不通过 结论。独立性由「模型不同」保证（R4）。
- **归档**：任务文件从 `tasks/*.md` 移入 `tasks/done/*.md`（`agent.py:257-266`）。
- **WIP 提交**：Agent 报告完成后、审查前落在 `arranger/<name>` 工作分支上的提交（合并前审查的崩溃安全与返工上下文保障，见 §3.1/§3.5）。

### 1.1 需求边界澄清（已全部定案）

1. **「默认」的语义**：审查模型无法凭空默认——系统里只有用户配置过的大模型。定案：**默认不配置**（评审一）。默认态下所有任务照常执行归档，与旧版行为一致；用户在配置中写入审查模型（可为任意第二模型，如便宜的小模型）后，审查门即刻对所有项目生效。看板仅给**中性提示**（如「未配置审查模型，审查未启用」），不做告警施压。
2. **审查时机 = 合并前**（评审一）：审查对象是 worktree 中相对主分支的累计改动；通过才 merge。代价与收益见 §6-D1。
3. **不追溯**：`tasks/done/` 中已归档的历史任务一律不补审；补审扫描只处理新机制产生的「待审查」残留（§3.5）。
4. **审查范围**：**结果审查**（对照任务需求/验收标准核查代码变更是否真的完成、有无明显缺陷），不是全量代码评审；审查 agent 默认只读，可按配置放行测试命令（评审二批准）。
5. **返工次数**：全局默认值（设置页可调）+ 项目级覆盖（评审二）。

---

## 2. 现状梳理（改动锚点）

### 2.1 任务生命周期（现状）

```
scheduler 周期 → ProjectAgent.scan_and_work()          agent.py:97
  ├─ scan_tasks() 取 open 任务                          task_model.py:156
  ├─ _mark_in_progress()                                agent.py:215
  ├─ TaskExecutor.run_task()                            executor.py:149
  │    ├─ Agent 工具循环（LLMClient.run_task_loop）      llm.py:342
  │    ├─ action=completed → _commit_and_merge()        executor.py:344
  │    │     worktree commit → merge 主分支 → push
  │    └─ 返回 TaskResult(outcome=done/blocked/cancelled)
  └─ _write_back_result()                               agent.py:221
       ├─ 回写「## Agent 处理结果」节 + status
       └─ outcome==done → 移入 tasks/done/ 归档          agent.py:257-266
```

- 任务文件 = YAML frontmatter（`task_model.py:101`）+ 正文（`description` 含验收标准等；`_build_task_prompt` 把整个正文喂给下轮执行，天然适合承载「审查意见」给返工轮）。
- 状态机（`config.py:21`）：`open → in_progress → done / blocked / cancelled`；`blocked → open` 重新入队带 `requeue_seq` 优先（`task_model.py:267`）。
- 进程启动有中断恢复：`recover_interrupted_tasks`（`agent.py:33`）只处理 `in_progress` 残留，重置为 open 重跑。
- worktree 生命周期（`executor.py:299-340`）：每任务 `_prepare_worktree` 复位到分支尖（`reset --hard <branch>` + `clean -fd`）——**分支上已提交的内容跨任务保留，未提交改动会被清掉**。这是合并前审查必须「先提交再审查」的原因（否则返工轮会丢掉上一轮成果）。

### 2.2 配置体系（现状）

- 全局 `llm`（`LLMConfig`，`config.py:61`）+ 项目级 `llm` 部分覆盖字典（`ProjectConfig.llm`，`config.py:45`），`resolve_llm`（`config.py:287`）字段级继承；`normalize_llm_override`（`config.py:310`）清洗；`_LLM_FIELDS`（`config.py:24`）白名单。**这套「部分覆盖 + 解析校验」是 review 配置的直接模板**。
- Web 保存链路：`Dialogs.vue` 表单 → `POST /api/project` → `_project_payload`（`web.py:946`）→ `_save_config`（`web.py:1019`，projects 序列化在 `web.py:1065-1079`）→ `_apply_config` 重建 `ProjectAgent`（`web.py:1087`，`resolve_llm` 在 `web.py:1095`）。**`_save_config` 漏写新字段 = 看板任意保存时静默丢失**（文件内多处注释已警示此类事故）。
- `ProjectAgent` 构造点 4 处：`web.py:200`、`web.py:1095`、`scheduler.py:48`、`scheduler.py:121`（均传 `resolve_llm(...)` 产物）。
- `config.example.yaml` 与 `orcsy/__main__.py:16 CONFIG_EXAMPLE` 必须字节级同步（`tests/test_config_example.py` 强制）。
- 密钥脱敏：`full_state` 只出 `api_key_masked`（`web.py:534` `_project_llm_public`）；三态密钥保存（缺键=保持/空串=清除，`web.py:979`）。

### 2.3 结论：现状差距

代码里**没有任何** review/审查机制（全库检索确认）。R1-R4 全部为新增能力，但每个都可复用现有模板（部分覆盖配置、finish 结论协议、任务文件节、看板模型选择器）。

---

## 3. 总体设计

### 3.1 流程设计（目标生命周期：合并前审查）

```
scan_and_work()                                          agent.py:97
  ├─ [新增] 补审扫描：tasks/ 内 review_pending=true 的任务 → 只跑审查（不重跑任务）
  └─ 常规 open 任务：
       _mark_in_progress()
       TaskExecutor.run_task()                           executor.py:149
         ├─ Agent 工具循环 → action=completed
         ├─ [新增] 第一步提交：worktree commit 到 arranger/<name> 分支
         │    （提交后置 review_pending 标记——此刻起崩溃可无损恢复）
         ├─ [新增] 审查门 review_before_merge()：
         │    ├─ 未配置审查模型 / 项目关闭 / 运行时守卫冲突 → 跳过（默认态）
         │    ├─ 审查 agent 循环（root=worktree，只读工具 + 可选 run_command，
         │    │    diff = git diff main...arranger/<name>）→ verdict + issues[]
         │    │    ├─ pass   → merge（返工任务且 squash_merge → --squash 收敛）
         │    │    │           → push → outcome=done
         │    │    ├─ fail   → 不合并，分支提交保留 → outcome=blocked + review=fail
         │    │    └─ error  → 不合并，分支提交保留 → outcome=blocked + review=error
         │    └─ 审查期间看板「强制停止」同样生效
         └─ blocked/cannot_do/cancelled（Agent 自报）：不进审查门（原逻辑清理 worktree）
       _write_back_result()                              agent.py:221
         ├─ done + review=pass/skip → 回写「## Agent 处理结果」+「## 审查结果」→ 归档 done/
         ├─ review=fail → review_round+1：
         │    <  生效返工上限 → status=open 返工（结构化审查意见入正文，优先领取，静默）
         │    ≥  生效返工上限 → status=blocked（分支提交保留，等人工裁决）
         ├─ review=error → 保持 review_pending + 状态待审，下轮补审只重跑审查；
         │    连续 error ≥ 生效返工上限 → status=blocked
         └─ 其余 blocked/cancelled → 原逻辑
```

**生效返工上限** = `项目 review_max_rounds ?? 全局 review.max_review_rounds`（默认 2）。

**状态机扩展**（`status` 枚举不变，新增 frontmatter 字段）：

| 展示态 | 判定 | 含义 |
|--------|------|------|
| 待审查 | `review_pending: true` | Agent 已完成并落 WIP 提交，审查门未出结论（审查中/审查 error 待重试/进程中断残留） |
| 审查通过 | 已在 `tasks/done/`，含「## 审查结果（通过）」节 | 已合并 + 归档完成 |
| 返工 | `status=open` 且 `review_round>0` | 审查不通过，带审查意见重新执行（从 WIP 提交续作） |
| 审查受阻 | `status=blocked` 且 `review_round≥上限` 或连续审查失败超限 | 等人工裁决；分支上留有成果提交可人工合并 |

不变量：**合并与归档 = 审查已通过或被跳过（默认未配置态）**；`review_pending` = 合并未发生、成果在分支上。崩溃安全：审查前已落 WIP 提交 + pending 标记，任何时点被杀都不丢成果、不重复执行 Agent。

frontmatter 新增字段（`task_model.py` 解析/写入同构扩展，风格同 `requeue_seq`）：

- `review_pending: bool` —— 待审查标记；WIP 提交后写入，出结论（pass/fail/返工/受阻）即清除，仅 error 重试路径保留。
- `review_round: int` —— 审查不通过累计轮数；通过后保留作审计。

### 3.2 配置设计

```yaml
# ===== 全局（config.yaml）=====
review:
  enabled: true                 # 全局总开关（默认 true，便于临时全局暂停而不清模型配置）
  llm: ~                        # 审查 agent 模型绑定；默认 ~（不配置）= 审查未启用（定案）
  max_turns: 10                 # 审查工具循环轮数上限（含跑测试的轮次）
  max_review_rounds: 2          # 返工次数全局默认：累计审查不通过达到上限即转 blocked 等人工
  allow_test_commands: true     # 审查 agent 可用 run_command 跑测试/构建验证（评审二批准，默认开）
  squash_merge: true            # 返工任务（review_round>0）通过审查后 merge --squash 收敛 WIP 提交（评审二批准，默认开）

# ===== 项目级（projects[]）=====
projects:
  - name: foo
    review_enabled: true        # 项目级开关：None/缺省 = 跟随全局 review.enabled
    review_llm:                 # 部分覆盖全局 review.llm（同 llm 绑定的字段级继承）
      model: "deepseek-chat"
    review_max_rounds: 3        # 项目级返工次数覆盖：None/缺省 = 跟随全局 max_review_rounds（评审二）
```

**配置解析**（`config.py` 新增，模板照抄 `resolve_llm`/`normalize_llm_override`）：

- `ReviewConfig` dataclass：`enabled: bool = True`、`llm: LLMConfig | None = None`、`max_turns: int = 10`、`max_review_rounds: int = 2`、`allow_test_commands: bool = True`、`squash_merge: bool = True`。**默认不配置模型**（定案）。
- `resolve_review_llm(global_review_llm, override) -> LLMConfig | None`：项目 `review_llm` 字段级覆盖全局 `review.llm`；两级都未配置 → `None`。
- `resolve_review_rounds(review_cfg, project) -> int`：`project.review_max_rounds ?? review_cfg.max_review_rounds`；两级均校验 1~10。
- **`review.llm` 不隐式继承全局 `llm`**：审查模型必须显式配置。隐式继承在单模型用户处必然撞上 R4 冲突，等于永远生成非法配置——宁可语义干净：「没配 = 审查未启用」。
- 生效审查开关 = `项目 review_enabled ?? 全局 review.enabled`。
- `allow_test_commands` / `squash_merge` 为全局级，不提供项目覆盖（如需可后续扩展）。

**生效组合矩阵**：

| 全局 review.llm | 项目 review_enabled | 与任务模型关系 | 行为 |
|---|---|---|---|
| 未配置（默认态） | — | — | **不审查**，照常执行归档（与旧版一致）；看板中性提示 |
| 已配置 | 缺省/true | ≠ 任务模型 | **审查门生效（R2/R3/R4 全满足）** |
| 已配置 | 缺省/true | = 任务模型 | **配置非法**：load_config 报错拒启 / 看板保存拒绝（400）；运行时守卫兜底跳过 |
| 已配置 | false | — | 该项目关闭审查，照常合并归档 |
| review.enabled=false | — | — | 全局暂停（保留模型配置），照常合并归档 |

### 3.3 模型互斥约束（R4）

**判重规则**：`model` 字段规范化（strip + casefold）后相同即判为「同一个模型」——同一模型名经不同 base_url 部署仍是同一模型，不得自审自批。（provider+base_url 也相同时额外告警，但判重以 model 为准。）

**校验点（三层）**：

1. **配置加载** `load_config`：逐项目解析生效任务模型（`resolve_llm`）与生效审查模型（`resolve_review_llm`），审查模型已配置且撞车 → `ValueError` 报错拒启（与现有 eager-validate 风格一致：`permission_mode` 非法、llm 绑定 typo 都是加载期 raise）。未配置审查模型不做此项校验（默认态合法）。
2. **Web 保存**：`_project_payload`（项目保存）与 `save_settings`（全局 review.llm 保存，需重算所有项目生效组合）拒绝冲突配置，400 + 明确提示「审查模型必须与任务模型不同」。看板表单同时做前端实时校验（模型下拉旁红字），保存按钮拦截。
3. **运行时守卫**：审查执行入口最终比较一次生效 model（防手改 yaml 绕过、防热重载竞态）→ 冲突时按「未配置」处理（跳过 + 告警），**绝不执行同模型审查**。

### 3.4 审查 Agent 执行器

新增 `orcsy/review.py`（约 320 行），核心 API：

```python
@dataclass
class ReviewIssue:
    file: str             # 相对项目根的文件路径
    line: int | None      # 行号（可缺失）
    severity: str         # blocker / major / minor
    message: str          # 问题描述（含建议）

@dataclass
class ReviewResult:
    verdict: str          # pass / fail / error / skipped
    summary: str          # 一句话审查结论
    issues: list[ReviewIssue]   # 结构化问题清单（fail 时；解析失败回退纯文本 notes）
    issues_raw: str       # notes 原文（结构化解析失败的兜底展示）
    model: str            # 实际审查模型（审计）
    turns: int
    prompt_tokens: int
    completion_tokens: int
    error: str = ""       # error 时的失败原因

def review_task(project, task, result: TaskResult, work_root: Path,
                diff_text: str, review_llm: LLMConfig, *,
                max_turns: int, allow_test_commands: bool,
                stop_event=None) -> ReviewResult
```

**执行方式：复用 `LLMClient.run_task_loop`**（`llm.py:342`）：

- 工具集：`{list_dir, find_files, read_file, grep, read_skill, finish}`；`allow_test_commands=true`（默认）时**追加 `run_command`**——用于跑测试/构建验证任务声称的「验证通过」（评审二批准）：
  - `run_command` 受**项目生效命令白名单**（`resolve_commands` 产物）约束，与任务 agent 同一闸门；
  - 访问权限模式为 `ask_on_change`/`autonomous` 时，审查 agent 的 `run_command` **走同一审批门**（与任务 agent 一致的安全语义，`executor.py:97` `_make_approval_gate` 复用）；`full_access` 不询问；
  - `allow_test_commands=false` 时回到纯只读工具集。
- `ToolContext.root = worktree`（git 项目）/ `project.path`（非 git 项目）：审查发生在**合并前**，读到的是待合并改动形态。
- 停止信号：与任务同用 `get_task_controls()` 注册 `(project, task_id)`，看板「强制停止」在审查期间同样生效。
- `max_turns = review.max_turns`（默认 10）。
- LLM 瞬时故障重试沿用 `LLMClient` 内建（4 次退避，`llm.py:232-234`）。

**结论协议：复用 `finish` 工具 + 新增可选 `issues` 字段**（评审二批准的结构化意见）：

| finish 参数 | 审查语义 |
|---|---|
| `action="completed"` | verdict = **pass** |
| `action="blocked"` | verdict = **fail** |
| `summary` | 一句话审查结论 |
| `issues`（**新增，可选**） | 结构化问题数组：`[{file, line?, severity: blocker/major/minor, message}]`；仅审查模式的提示词要求提供 |
| `notes` | 补充说明（结构化 issues 解析失败时的纯文本兜底） |
| `files_changed` / `commit_message` | 审查场景留空 |

- `finish` 工具 schema（`tools.py`）追加可选 `issues` 参数；`parse_finish_args`/`finish_response`（`llm.py:260-282`）透传该字段。任务 agent 的提示词不提及 `issues`——不提供即缺省，现有任务流程零影响。

**审查输入**（合并前审查的特有输入）：

- 任务全文（含验收标准）与「## Agent 处理结果」草案（summary/files_changed/notes）。
- **改动 diff**：git 项目 `git diff main...arranger/<name>`（三点 diff = 相对 merge-base 的累计改动，含返工轮的历史 WIP 提交），截断上限约 80KB，超限保留 diffstat + 重点文件全文可由审查 agent 用 read_file 自行查看。
- 非 git 项目无 diff：审查 agent 直接只读探查项目目录（review 之前执行 Agent 的改动已在盘上）。

**审查提示词**（`prompts.py` 新增 `build_review_prompt`）要点：

- 角色：**独立审查 Agent**，与执行 Agent 非同一模型；禁止修改文件、禁止执行 git 写操作（合并由系统在通过后统一处理）。
- 要求：对照需求与验收标准逐条核对（用 read_file/grep 抽查实现真实性，防「声称完成实则未做」）；`allow_test_commands` 时**必须实际跑任务指明的测试/构建命令**验证，不能只信 Agent 的自述；只报实质问题（缺陷/未满足需求/明显质量问题），不纠缠风格；无问题则通过。
- 问题上报格式：fail 时以 `issues` 数组给出（每条含 file/line/severity/message，severity 按 blocker=阻断验收 / major=功能缺陷 / minor=次要问题分级）；禁用纯文本结论。

### 3.5 审查结论与状态机细节

- **pass**：合并主分支——`review_round>0`（返工过）且 `squash_merge=true` 时用 **`merge --squash`** 收敛分支上全部 WIP 提交为单提交（提交信息取 Agent 的 `commit_message`，兜底 `fix: <task.title>`，正文附 `Task: <id>` 与审查轮数）；否则原 `merge --no-ff`。之后 push → 清 `review_pending` → 回写「## 审查结果（通过）」（时间/审查模型/结论/轮数/tokens；返工任务附各轮问题摘要一行）→ 归档到 `tasks/done/`（原 `agent.py:257-266` 逻辑）。
- **fail**：**不合并**；分支 WIP 提交保留（返工上下文）；清 `review_pending`；回写「## 审查结果（不通过）」（同上 + 结构化问题清单，任务文件渲染为 `- [severity] file:line — message` 列表）；`review_round+1`：
  - `review_round < 生效返工上限`（项目 `review_max_rounds` ?? 全局默认 2）→ `set_task_file_status(path, "open")`（自动带 `requeue_seq` 插队，`task_model.py:267-284`）→ 下轮任务 agent 重新执行。**返工续作**：`_prepare_worktree` 复位到分支尖 = WIP 提交内容完整保留（§2.1 已析），Agent 从上次成果继续修问题，而非从零重做。**返工输入**：任务正文已含「## 审查结果」问题清单（含结构化条目），`_build_task_prompt` 的 `task.description` 天然带给下轮 agent。返工轮的执行静默（不触发完成/受阻提醒，防骚扰）。
  - `≥ 生效返工上限` → `status=blocked`（进度注明「连续 N 次审查不通过，等人工裁决；成果保留在 arranger/<name> 分支」）。**审查受阻不清理 worktree**（区别于 Agent 自报 blocked 的清理逻辑），人工可检视、可手动合并分支。
- **error**（审查调用大模型失败/超时/协议解析失败）：**不合并**；分支提交保留；`review_pending` 保持；状态回写审查失败说明；下轮「补审」扫描只重跑审查（不重跑任务、不重复消耗执行 token）；连续 error 达生效返工上限 → `status=blocked`（人工重新入队会整任务重跑，可接受）。
- **skipped**（默认未配置 / 项目关闭 / 运行时守卫冲突）：直接走原合并路径（`_commit_and_merge`），归档时在结果节追加一行 `- 审查：未执行（原因）`；默认未配置态为中性标注，看板不告警。
- **补审扫描**（`scan_and_work` 新增步骤）：`tasks/*.md` 中 `review_pending=true` 的文件 → 只执行审查门（复用 `review_task` + verdict 处理），不重跑 Agent。覆盖两类场景：审查 error 待重试、进程在「WIP 提交后、审查结论前」被强杀的残留。
- **`recover_interrupted_tasks` 调整**（`agent.py:33`）：启动恢复跳过 `review_pending=true` 的任务（交给补审），仅重置普通 `in_progress` 残留——避免把已完成待审的任务整轮重跑。
- `blocked`/`cannot_do`/`cancelled` 的 Agent 自报结果不进审查门（本就不合并不归档，worktree 清理照旧）。

### 3.6 可观测性

| 面 | 内容 |
|---|---|
| 任务文件 | 「## 审查结果」节（通过/不通过/失败/未执行 + 模型名 + 结构化问题清单 `- [severity] file:line — message`）；`review_pending`/`review_round` 字段；WIP 提交哈希写进审查节（审计可溯） |
| 看板任务详情 | 正文含审查节（现有正文渲染直接可见）；**结构化审查意见在详情视图渲染为带严重度色标（blocker 红/major 橙/minor 灰）的条目列表**（file:line 可点击定位的增强留 v2）；任务卡片对 `review_pending` 显示「待审查」徽标 |
| 看板项目/设置 | 项目卡片 & 设置页展示审查配置状态：`生效审查模型`、`未配置（审查未启用）`中性提示、`⚠ 与任务模型冲突`、`生效返工次数`（全局或项目覆盖值）（`full_state` 项目对象新增 `review` 块：`enabled/effective_model/model_configured/effective_max_rounds/warning`） |
| 日志 | `review.<project>` logger：审查开始/结论/跳过原因/测试命令执行；复用 `logging_setup` |
| 用量台账 | `usage.record` 增加 `kind="review"` 条目（`task_id` 同任务 ID，模型记审查模型），token 消耗可与执行区分（`usage.py` JSONL 条目加可选 `kind` 字段，聚合视图兼容缺省） |
| 通知 | 复用 `notify_task_end` 旁路：审查不通过转 blocked / 连续失败超限时提醒；返工轮静默（防骚扰） |

---

## 4. 详细改动清单（文件级）

### 4.1 后端 `orcsy/`

| 文件 | 改动 |
|------|------|
| `config.py` | ① 新增 `ReviewConfig`（enabled/llm/max_turns/max_review_rounds/allow_test_commands/squash_merge，llm 默认 None）挂入 `AppConfig`；② 新增 `resolve_review_llm` / `normalize_review_llm`（照抄 `resolve_llm`/`normalize_llm_override`，`config.py:287-320`）与 `resolve_review_rounds`；③ 新增 `assert_review_model_distinct(task_llm, review_llm)`（model 规范化判重）；④ `ProjectConfig` 加 `review_enabled: bool \| None`、`review_llm: dict \| None`、`review_max_rounds: int \| None`；⑤ `load_config` 解析全局 `review` 节（max_review_rounds/项目级均校验 1~10）+ 项目三字段，逐项目做生效组合互斥校验（显式冲突 raise） |
| `task_model.py` | ① `Task` 加 `review_pending: bool`、`review_round: int`；② `parse_task_file` 解析两字段（宽容风格，`task_model.py:125-131` 同款）；③ 新增 `set_task_file_review_pending(path, bool)`（同 `set_task_file_order` 写法，`task_model.py:237`）；④ 状态枚举不动 |
| **`review.py`（新增）** | `ReviewIssue`/`ReviewResult` dataclass + `review_task()`：组装审查 prompt（diff 注入）、按 `allow_test_commands` 构建工具集、复用项目命令白名单与审批门、`LLMClient.run_task_loop`、finish→verdict 映射、`issues` 结构化解析（含纯文本兜底）、usage 汇总 |
| `prompts.py` | 新增 `build_review_prompt(project, task, result, diff_text, allow_test_commands)`；审查系统提示词（角色/禁改约束/审查要求含跑测试/issues 上报格式/finish 映射/合并协议归属系统） |
| `executor.py` | ① `TaskResult` 加 `review: ReviewResult \| None = None`；② `run_task` 的 `action==completed` 分支改为：**先 WIP 提交到 arranger 分支**（原 `_commit_and_merge` 的 commit 步骤前移）→ 置 `review_pending` → 审查门 → pass 才 merge（返工且 squash_merge → `--squash`，否则 `--no-ff`）+ push（原 merge 步骤）；fail/error 不合并且**不清理 worktree**；③ 跳过路径（未配置/关闭/守卫冲突）= 现行为（提交后直接合并）；④ 非 git 项目：无提交/合并，审查直接读项目目录，fail 同样走返工 |
| `llm.py` | `finish_response`（`llm.py:269`）透传可选 `issues` 字段（list[dict] 宽容解析） |
| `tools.py` | `finish` 工具 schema 追加可选 `issues` 参数（对象数组：file/line/severity/message） |
| `agent.py` | ① `ProjectAgent.__init__` 接收 `review_cfg: ReviewConfig` + `review_llm: LLMConfig \| None`（构造点 4 处同步：`web.py:200/1095`、`scheduler.py:48/121`）；② `_write_back_result` 按 `result.review` 分派：pass/skip → 审查节 + 归档；fail → 返工/blocked（不清理 worktree 已由 executor 保证）；error → 保持 pending 待补审；③ `scan_and_work` 新增补审扫描步（`review_pending` → 只跑审查）；④ `recover_interrupted_tasks` 跳过 `review_pending` 任务 |
| `usage.py` | `record` 增加可选 `kind="task"/"review"`；聚合与 `full_state` 输出兼容缺省 |
| `web.py` | ① `_project_payload`（`web.py:946`）接收 `review_enabled`/`review_llm`/`review_max_rounds`（api_key 三态同 `llm`，`web.py:979` 模式），互斥校验拒绝冲突、rounds 校验 1~10；② `_save_config` projects 块（`web.py:1065-1079`）**必须**序列化三个 review 字段（防静默丢失），全局节序列化 `review`（含 allow_test_commands/squash_merge）；③ `full_state` 项目对象加 `review` 状态块（模型掩码复用 `_project_llm_public`，`web.py:534`；含 `effective_max_rounds`）；④ `save_settings`（`web.py:1675`）支持全局 review 节保存 + 全项目互斥复检；⑤ `ProjectAgent` 构造（`web.py:200/1095`）传入 review 配置 |
| `scheduler.py` | 构造点（`scheduler.py:48/121`）传入 review 配置 |
| `config.example.yaml` + `__main__.py CONFIG_EXAMPLE` | 新增 `review:` 示例节（`llm: ~` 默认不配置 + 注释说明全部字段），两者字节级同步（`tests/test_config_example.py` 强制） |
| `__main__.py` | 帮助文本/示例注释同步 |

### 4.2 看板 `dashboard/src/`

| 文件 | 改动 |
|------|------|
| `components/Dialogs.vue` | 项目弹窗在「绑定专属模型」块（`Dialogs.vue:561-589`）后新增「审查 Agent」区：`审查开关`（三态：跟随全局/开/关）+「绑定审查专属模型」（provider/base_url/api_key/model，复用 `fetchProjectModels`/`api.models` 下拉与三态密钥模式）+「返工次数」（三态数字输入：跟随全局/1~10 自定义，旁注显示全局当前值）；前端实时校验「审查模型 ≠ 生效任务模型」红字提示；`saveProject()`（`Dialogs.vue:239`）组装 `review_enabled/review_llm/review_max_rounds` |
| `views/SettingsView.vue` | 全局设置「审查 Agent」卡：enabled、模型绑定、max_turns、**max_review_rounds（全局返工次数默认值）**、allow_test_commands 开关（旁注「受项目命令白名单与访问权限约束」）、squash_merge 开关；未配置审查模型时显示**中性提示**（「审查未启用：配置一个与任务模型不同的审查模型即可开启」），不做告警施压 |
| `views/DashboardView.vue` | `toggleProject`（`DashboardView.vue:113-135`）**必须原样回传** `review_enabled/review_llm/review_max_rounds`（防启用开关把绑定冲掉——与 llm 字段同一教训）；项目卡片/任务卡片「待审查」徽标（review_pending） |
| 任务详情视图 | 结构化审查意见渲染：severity 色标条目列表（blocker 红/major 橙/minor 灰，file:line + message）；纯文本兜底（issues_raw）按普通段落渲染 |
| `api.ts` | `ProjectT` 加 `review_enabled/review_llm/review_max_rounds`（`api.ts:22+`）；`projectSave` payload、`state` 类型补 `review` 块与 `ReviewIssueT` |
| `store.ts` | `openProjectNew/openProjectEdit` 透传新字段（`store.ts:116-121`） |

### 4.3 文档

- `README.zh-CN.md` / `README.md`：配置参考加 `review:` 节说明（含 allow_test_commands/squash_merge/返工次数两级语义）；架构图任务生命周期加审查门（合并前）。
- `docs/用户指南.md` / `docs/user-guide.md`：新增「任务审查（Review Agent）」章节：配置方法（默认不配置，配置即全局生效）、双模型要求、合并前审查语义、返工与人工裁决流程（返工次数全局/项目两级）、结构化审查意见说明、徽标说明。

---

## 5. 测试计划（沿用 tests/ 纯脚本风格：`check_*` + `main()`）

| 测试文件 | 覆盖点 |
|----------|--------|
| `tests/test_review_config.py`（新） | `resolve_review_llm` 继承链（项目覆盖→全局→None）；`resolve_review_rounds`（项目覆盖→全局，1~10 校验）；`normalize_review_llm` 清洗；互斥判重（同 model 拒绝、不同 model 通过、大小写/空白归一）；`load_config` roundtrip（review 全部字段含 allow_test_commands/squash_merge + 项目三字段）；老配置无 review 节 → 默认 enabled+未配置模型+rounds=2（迁移兼容）；显式冲突配置加载报错 |
| `tests/e2e_review.py`（新，FakeLLMClient 脚本化审查结论，仿 `e2e_fake_llm.py`） | ① pass → **先合并后归档**：主分支含改动、任务在 `tasks/done/` 且含「## 审查结果（通过）」；② fail → **主分支无改动**（未合并），分支保留 WIP 提交，status=open + `review_round=1` + 结构化意见落盘（`[severity] file:line` 格式），返工轮 `_prepare_worktree` 后 WIP 内容仍在；项目 `review_max_rounds=1` 时一次 fail 即 blocked（项目覆盖生效）；默认 rounds=2 时二次 fail 才 blocked；③ 审查 error → 不合并、`review_pending` 保持，下轮补审只跑审查不重跑任务，超限 → blocked；④ 未配置审查模型（默认态）→ 无审查直接合并归档，结果节含「未执行」标注；⑤ `allow_test_commands` 开/关时审查工具集含/不含 `run_command`（FakeLLM 断言下发 schema），且命令白名单拦截生效；⑥ 返工后 pass + `squash_merge=true` → 主分支单提交（squash）、无 merge commit；`squash_merge=false` → `--no-ff` merge commit；⑦ 模拟审查中强杀 → WIP 提交 + pending 标记在，补审接续，`recover_interrupted_tasks` 不重置该任务；⑧ 强制停止在审查期间生效；⑨ Agent 自报 blocked 不进审查门；⑩ finish `issues` 字段结构化解析 + 非法/缺失回退纯文本 |
| `tests/test_review_web_api.py`（新，仿 `e2e_project_api.py`） | 项目保存含三个 review 字段 roundtrip（含 review_max_rounds）；同模型保存被拒（400）；`full_state` review 块掩码无明文密钥 + `effective_max_rounds` 正确（对齐 `test_state_masking.py` H2）；`toggleProject` 回传不丢 review_llm/review_max_rounds；全局 review 节保存与全项目互斥复检 |
| `tests/test_config_example.py`（已有） | 新 review 节同步进 `CONFIG_EXAMPLE` 后仍字节级一致 |
| `tests/e2e_fake_llm.py`（已有） | 未配置 review 时旧路径行为不变（提交→合并→归档照旧）——回归护栏 |
| `tests/test_project_llm.py`（已有） | 不受影响，继续保证任务模型绑定语义 |

---

## 6. 关键决策与备选方案（全部定案）

| # | 决策 | 定案 | 备选（未采纳） | 理由 |
|---|------|------|------|------|
| D1 | 审查时机 | **合并前**（✅ 评审一）：WIP 提交到 arranger 分支 → 审查 → 通过才 merge | 合并后归档前审查 | 坏代码不进主分支；审查对象为待合并改动形态。代价：WIP 提交保全返工上下文（`_prepare_worktree` 会清未提交改动，§2.1）、`review_pending` 崩溃标记、executor 审查门改造 |
| D2 | 默认配置策略 | **默认不配置审查模型；已归档任务不追溯**（✅ 评审一）：默认态=不审查（与旧版一致），配置模型即全局生效，看板中性提示 | 默认开启 + fail-open 告警施压；fail-closed 不归档 | 「能力可选、默认无感」；不打算用审查的用户零打扰 |
| D3 | 返工机制与次数 | **自动返工**：回 open + 审查意见入任务文件 + WIP 提交续作；次数上限**默认跟随全局设置页**（`review.max_review_rounds`，默认 2），**项目级 `review_max_rounds` 可覆盖**（✅ 评审二） | 直接 blocked 等人工；照常归档仅标记；仅全局单一值 | 无人值守推进是系统核心价值；两级次数适配「重要项目多轮返工、普通项目快速收敛」的差异化需求；上限防死循环 |
| D4 | 审查工具面 | **只读 + 可选测试命令**（✅ 评审二批准）：默认 `allow_test_commands=true`，审查 agent 可用 `run_command` 跑测试/构建；受项目命令白名单与访问权限审批门约束，可全局关闭 | 纯只读（v1 原案）；放行全部工具 | 审查能实测验证而非轻信自述，显著降低假阳性；副作用由既有白名单/审批体系兜底 |
| D5 | 审查执行形态与结论协议 | **复用 `run_task_loop` + finish 协议，新增可选 `issues` 结构化字段**（✅ 评审二批准：file/line/severity/message，看板渲染） | 单轮无工具 JSON 结论；纯文本 notes | 复用全部基建；结构化意见让返工输入与看板展示精确可操作；任务 agent 不受影响（不要求该字段） |
| D6 | 判重规则 | **`model` 名规范化比较** | (provider, base_url, model) 三元组 | 同名模型换端点仍是同一模型；三元组会被「同模型双端点」绕过 |
| D7 | review.llm 继承 | **不隐式继承全局 llm，显式配置才生效** | 缺省继承全局 llm | 隐式继承在单模型处必然产生非法组合；「未配置=审查未启用」边界清晰 |
| D8 | 返工成果保全 | **WIP 提交到 arranger 分支**（审查前必提交） | fail 后清 worktree 从零重做；改 `_prepare_worktree` 保留未提交改动 | 现有复位语义下未提交改动必被清；提交是唯一可靠的崩溃安全点 |
| D9 | 审查受阻现场 | **保留分支提交与 worktree**，人工可检视可手动合并 | 照 Agent-blocked 逻辑清理 worktree | 审查受阻 ≠ 成果无效；清理会逼人工重做 |
| D10 | 返工提交收敛 | **返工任务通过审查后 `merge --squash`**（`review.squash_merge`，默认开，✅ 评审二批准）；未返工任务维持 `--no-ff` | 全部 squash；全部 --no-ff | 返工轮的多个 WIP 提交收敛为单提交保持主分支历史整洁；未返工任务保持现有 merge 语义不变 |

## 7. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 审查增加每任务时长与 token 成本（一次审查约 3~8 轮，跑测试再加数轮） | `max_turns=10` 封顶；diff 截断 80KB；用量台账 kind=review 单列可核算；项目级可关；默认态零成本 |
| 审查误判（假阴性卡死好任务 / 假阳性放过坏任务） | 返工上限两级可调 + blocked 等人工兜底（误判可看板一键改状态，分支成果仍在）；`allow_test_commands` 让审查实测验证降低假阳性；提示词限定「只报实质问题」；上线后观察误判率调 prompt |
| 审查跑测试的副作用/耗时 | 与任务 agent 同一命令白名单与审批门（ask_on_change/autonomous 下同样挂起）；`allow_test_commands` 可全局关闭；`max_turns` 封顶轮数 |
| 返工轮多次 WIP 提交污染主分支历史 | `squash_merge`（默认开）在通过时收敛为单提交；提交信息统一带 Task ID 可溯 |
| `_save_config` 漏写新字段导致静默丢失 | 改动清单显式标注（§4.1 web.py 行）；roundtrip 测试（§5）断言保存→加载字段不丢 |
| `toggleProject` 回传冲掉 review 字段 | 同上，测试覆盖（含 review_max_rounds） |
| 手改 config.yaml 绕过校验 | 运行时守卫（§3.3 第 3 层）：冲突即跳过并告警，不执行同模型审查 |
| 审查期间进程被杀 | WIP 提交 + `review_pending` 标记在审查前落盘：成果不丢、补审接续、不重跑 Agent（§3.5） |
| 主分支在审查期间被人工推进导致 merge 冲突 | 审查通过后的 merge 走原 `_commit_and_merge` 失败路径：提交保留分支、任务 blocked 等人工（现行为，`executor.py:362-367`） |

## 8. 迁移与兼容

- **存量配置**：无 `review` 节 → `ReviewConfig(enabled=True, llm=None, max_review_rounds=2, allow_test_commands=True, squash_merge=True)` → 默认态「不审查」，**行为与旧版完全一致**（提交→合并→归档照旧），零破坏升级。
- **已归档任务不追溯**（定案）：`tasks/done/` 中历史任务永不补审；补审扫描只认新机制写入的 `review_pending` 标记，旧任务文件无此字段（解析为 false）天然不受影响。
- **升级时在途任务**：配置审查模型前完成的任务不受影响；配置后新完成的任务进入审查门。
- 存量任务文件无 `review_pending/review_round`：解析为 false/0（同 `requeue_seq` 宽容规则）。
- 用户配置一次全局 `review.llm`（≠ 任务模型）后，所有项目即刻生效（R1 的产品落点：配置一次、全局默认、项目可关）。
- `config.example.yaml`/`CONFIG_EXAMPLE`、README、用户指南同步更新；`tests/test_config_example.py` 保证模板同步不漏。

## 9. 工作量估算与里程碑

| 里程碑 | 内容 | 估算 |
|--------|------|------|
| M1 配置层 | `config.py`（ReviewConfig 全字段/两级解析/互斥校验/rounds 校验）+ 模板/示例同步 + `test_review_config.py` | 1.5 人日 |
| M2 审查执行与状态机 | `review.py`（含 issues 解析、工具集开关）+ `prompts.py` + `llm.py`/`tools.py` finish issues 透传 + `executor.py` 审查门/WIP 提交/pending 标记/双 merge 策略 + `agent.py` verdict 分派/补审扫描/恢复调整 + `task_model.py` 字段 + `e2e_review.py` | 4.5 人日 |
| M3 Web/看板 | `web.py` 四处 + `Dialogs.vue`（模型绑定 + 返工次数三态输入）/`SettingsView.vue`/`DashboardView.vue`/任务详情结构化意见渲染/`api.ts`/`store.ts` + `test_review_web_api.py` | 3 人日 |
| M4 收尾 | 用量/通知接线、回归全跑、README/用户指南、联调 | 1.5 人日 |

合计约 **10.5 人日**（评审二批准的三项增强 +2.5：测试命令 0.5 / 结构化意见 1 / squash 收敛 0.5 / 返工次数两级 0.5）。

## 10. 待确认问题

**无**——D1/D2（评审一）、D3 两级返工次数与三项 v2 增强（评审二）均已定案，方案可进入开发，按 M1→M4 顺序实施。
