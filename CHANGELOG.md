# 更新日志（Changelog）

所有显著变更记录于此。版本号遵循语义化版本（0.x 阶段 minor=新功能批次，patch=修复）；
**1.0.0 起承诺向后兼容契约**：破坏配置/数据的变更必须升 major。条目按用户可感知
的维度归类（新增/修复/变更），工程细节见 `docs/` 设计文档与各版本路线图。

## [0.36.0] — 2026-11-27

### 新增
- **风险分级审批 Phase 0+1（合入前，单机单审批人）**：新增全局 `risk_approval` 配置与本地风险评估（路径 + 内容规则）。
- 高风险合入前在合并锁外等待看板人工批准，绑定任务 commit 与 diff digest；批准后继续 premerge/合入，拒绝/超时/内容变化阻断并保留任务分支。
- 只有看板登录会话可批准，API Key 不能代批；默认只拦高风险，超时默认 60 分钟（可配 1~1440），多人会签明确留待 Phase 2。
- 审计写 `risk_approval.request|approve|reject|timeout|cancel|stale`；运行台账新增 `risk_assess` / `risk_approval_request` / `risk_approval_decision` 事件与 `risk.json`；任务正文增加「## 风险审批」。
- 看板：待审批弹窗增加风险合入审批段（等级/原因/文件数/增删行/意见），顶栏徽标合并计数；设置页新增「风险审批」分区。
- **风险审批 Phase 2A（外部会签骨架）**：配置升到 `CONFIG_VERSION=3`，新增 `approval_providers`（GitLab/GitHub/飞书/钉钉/fake 类型）、`approvers`（email↔Provider 身份）与 `risk_approval.{provider,quorum,allowed_approvers,callback_url,poll_interval_seconds}`。
- 新增持久化 quorum 引擎：按 email 白名单去重计数、一票否决、超时/取消/Provider 故障 fail closed、重启复用已批准请求；`POST /api/risk-approval/callback/{provider}` 免登录但强制 HMAC 验签。
- 看板支持 Provider 选择、高风险会签人数、email 白名单、callback_url（含「使用当前访问域名」）、轮询间隔与审批人映射；待审批弹窗显示会签进度/审批人/外部链接。
- **风险审批 Phase 2B（GitLab/GitHub 审批工单）**：Provider 创建 issue，审批人评论 `/approve` 或 `/reject 原因`，Orcsy 轮询计数；不推送代码分支。支持 GitLab `X-Gitlab-Token` 与 GitHub `X-Hub-Signature-256` Webhook 验签。
- **风险审批 Phase 2C（飞书/钉钉审批实例）**：飞书用 tenant_access_token 创建审批实例并轮询 task_list；钉钉用 gettoken 创建 processinstance 并轮询 operation_records；按 Provider 账号映射 email 计数。两者当前仅轮询，Webhook 暂未启用。
- **审批 Provider 可视化配置**：看板「设置 → 运行与成本 → 审批 Provider」支持 GitLab/GitHub/飞书/钉钉/fake 的添加、编辑与删除；密钥只显示掩码、留空保持原值；「+ 添加 Provider」旁的 ? 列出各家最小权限。
- 说明：Provider 凭据可在看板可视化配置；也可继续手改 config.yaml 的 `approval_providers`（适合自动化/CI），设置页选择名称并配置 quorum/白名单/回调。

### 修复
- 看板 keep-alive 连接上连续 POST 时，raw body 缓存未按请求重置，第二个请求会复用上一个 body，剩余字节被当成下一请求行（501 Unsupported method）。现于每个 POST 开始时重置，并新增 `test_web_keepalive_post.py` 回归。
- 审批 Provider 访问回环地址（本机自托管 GitLab 等）时不再走系统代理：此前本机常驻代理会把 `127.0.0.1` 请求转发到上游，表现为 502/连接重置/超时；公网 Provider 仍遵循代理设置。
- GitLab 审批工单：`/approve`、`/lgtm` 是 MR 保留 quick action，issue 上无法使用且不会产生 note；新增 `orcsy approve` / `orcsy reject 原因` 无斜杠写法。
- 钉钉 Provider：支持 `form_component_values`（按控件 label 提交）并兼容真实 `operation_result` 字段，修复空表单 820001 与会签永远 0/N。
- 飞书 Provider：审批实例 `form` 改为控件数组，修复真实模板 1390001 表单格式错误。
- 审查门：新增任务级 `review_no_tests`（审查不跑测试）；提示词明确「未运行测试」与 pytest 退出码 5 不算失败。
- 会签拒绝时记录拒绝人，`risk.json` 的 `by` 不再为空。
- 会签回调只按 `external_id` 匹配请求；GitLab 与 GitHub 的 issue 编号可能相同（如都叫 #3），webhook 会错投到另一家的请求。现按 Provider 名称隔离并优先匹配 pending 请求，新增回归用例。

## [0.35.0] — 2026-11-26

### 新增
- **Windows ARM64 原生安装包与自更新**：CI 在 `windows-11-arm` 上产出 `orcsy-setup-{ver}-windows-arm64.exe` 与 `orcsy-update-{ver}-windows-arm64.zip`（PyInstaller 不支持交叉编译）；更新器按运行进程架构选包，ARM64 版本缺少更新包时明确报错，不会回退安装 x64 仿真版。
- **构建脚本架构参数**：`build.py` 新增 `--arch` 与 `--skip-frontend`，跨架构构建直接拒绝；`installer.iss` 通过 `/DArm64` 参数化输出名与 `ArchitecturesAllowed`；发布清单与预发布 smoke 覆盖 arm64 产物。

### 修复
- **Linux 便携包解包失败**：tar 顶层启动器 `orcsy` 与包目录 `orcsy/` 同名，`tar -xzf` 报 `Not a directory`；启动器改名为 `orcsy.sh`（`install.sh` 同步调整），预发布 smoke 改为纯 Python 校验（`build/verify_packages.py`）：覆盖 zip/tar/deb 结构并直接列出缺失成员，不再依赖 `unzip`/`dpkg-deb` 的输出格式。

## [0.34.0] — 2026-11-25

### 新增
- **上下文摘要式压缩（Phase 2）**：裁剪旧工具结果前先请求任务模型生成 ≤400 字要点，追加为「上下文压缩摘要」消息，已确认事实不再随占位化丢失；摘要失败降级为「工具名 + 结果片段」的确定性摘要。同步修正 token 估算（中文按 ~0.7 token/字符，旧实现低估约 2 倍）。
- **项目经验记忆 `PROJECT_NOTES.md`（Phase 2）**：与 `ARCHITECTURE.md` 并列的长期经验通道（测试/构建命令与坑、环境怪癖、依赖约束），任务启动注入首尾共 1500 字（最新条目在尾部优先可见），`update_project_notes` 支持 append/replace，单文件上限 8000 字符；设置页「项目架构记忆」组一键开关。
- **项目归档包含长期记忆**：`archive-project` 与看板「立即归档」的打包内容增加 `ARCHITECTURE.md`、`PROJECT_NOTES.md`、`LONG_TERM_MEMORY.md`，换机/回灌不丢项目经验。
- **项目经验记忆满额归档（Phase 2）**：`PROJECT_NOTES.md` 达 8000 字符上限后，append 返回可执行指引；`update_project_notes` 新增 `mode="archive"`，把全文归纳摘要归档到 `LONG_TERM_MEMORY.md`（带日期段落、上限 20000 字符、超出自动裁剪最旧），同时把 `PROJECT_NOTES.md` 精简为该摘要后继续追加；长期记忆不整篇注入，按需 `read_file` 读取。
- **项目级合入前重测试点（P0）**：默认关闭，显式配置 `premerge_test_enabled` 与 argv 数组 `premerge_test_command`；隔离候选中合入最新主分支或已验收 stage 基线后运行测试，白名单/审批/停止/600 秒超时约束生效；测试失败或基线变化不合入，成果分支保留。stage 模式只允许主工作区恰等于已落地增量，拒绝额外人工改动。
- **发布前 smoke 与稳定化证据**：增加打包 CLI/附件检查、配置分块保存与全目录快照恢复回归；明确 1.0 起仅维护最新 1.x 次版本。Windows 签名与真实目标安装、生产观察期仍待验收。

### 修复
- 普通合并冲突执行 abort，stage 增量 apply 先预检，避免已知冲突直接污染主工作区；不对用户工作区执行无条件 reset。
- 合入前重测关闭后保留已配置命令；补审合入经过同一门禁。API 文档按当前登录锁定 401 与可选乐观锁兼容行为修正。
- **看板多语言切换修复与兜底**：修复切换语言后部分下拉选项与标签仍残留旧语言（角色/工具分类/权限/模型协议/存储目标等在组件初始化时一次性取值、未随语言更新的 11 处）；切换语言后整页刷新一次，确保所有数据源按新语言重新初始化（路由与语言偏好保留）。
- **任务草稿找回**：切语言整页刷新或异常中断后重新打开看板，会提示「检测到未完成的任务草稿」，一键回到对应任务弹窗恢复继续编辑；页面卸载前同步落盘，防抖窗口内的最后输入不丢。
- **项目卡片布局**：展开某张项目卡的任务列表时，同行其余卡片不再被拉高出底部空白；修复窄卡片下标题被徽章行挤压成竖排一字一行的问题（看板与项目页同修）。

### 变更
- 文档：README 特性区按主题合并精简（21 → 11 条，英文区 -63% 字符），补齐 Phase 2 记忆与归档描述；中文 README 版本徽章与包版本对齐。

## [0.33.0] — 2026-11-23

### 新增
- **多目标存储与备份/归档（S2 + 评估⑤）**：新增 `storage_targets` 注册表，把配置备份与项目任务归档的存储目标统一为 `local` / `webdav` / `s3`；`backup` 由扁平单目标（`target_type`/`url`/`username`/`password`）改为 `target` 引用目标名（CONFIG_VERSION 1→2，旧扁平 backup 自动迁移，幂等）
- **S3 兼容存储目标**：`orcsy/storage.py` 提供 Local / WebDAV / S3 三类 provider；S3 用 stdlib `hmac/hashlib` 实现 AWS Signature V4（零第三方依赖）；远程请求统一过出网白名单（修复历史 WebDAV 直连绕过）；MinIO、腾讯云 COS、阿里云 OSS 等 S3 兼容接入点可用
- **项目任务归档（评估⑤）**：`archive-project` 把一个项目的 `tasks`（含 `done/`）+ `AGENT.md`（可选 `runs/<项目>`）打包为快照推送到存储目标；定位「快照分发 / 合规审计」，单向（自动回灌后续再设计）
- **备份/归档看板设置区**：设置 → 数据与合规 → 备份与归档，存储目标增删改、备份/归档目标与保留份数，凭据三态脱敏（键缺失保持 / 空串清除 / 非空换新）
- **CLI 扩展**：`backup-config --target`、`restore-config --target`、`archive-project`

## [0.32.3] — 2026-11-22

### 新增
- **老版本升级接入三新角色的两处入口（devops / ui_critic / security）**：升级只扩充了可选角色清单，老配置的预设池仍是原四个默认预设。设置页「多模型项目小组」新增「补齐内置角色预设」按钮——按内置角色元数据生成三个预设进草稿（带职责模板与默认轮数，模型默认继承全局），确认后保存生效；新建项目的默认小组名单扩展为七角色，预设缺位时成员留空引用走角色内置默认。指南 §10.13 附两步接入说明

## [0.32.2] — 2026-11-22

### 修复
- **更新按钮文案双 V 前缀**：Release tag 自带版本前缀（Gitee 发 `V0.32.0`、GitHub 发 `v3.8.1`），顶栏「正在安装 v{target}…」「发现新版本 v{p0}」等文案又自带 v 前缀，渲染成「正在安装 vV0.32.0…」。新增 `fmtVer` 剥离 tag 的 V/v 前缀后归一展示（纯版本号原样通过），接入顶栏安装按钮、App「发现新版本」弹窗与设置页更新提示三处；手动导入更新包的版本从文件名提取、本就是纯版本号，不受影响

## [0.32.1] — 2026-11-22

### 修复
- **设置页页脚按钮间距**：`.sfoot` 增加 `gap: 8px`——「保存更新配置 / 检查更新」等并排按钮此前紧贴（与 `.btnrow` / `.pkg-btns` 的间距规范对齐），设置页所有共用该页脚的配置卡片一并统一

## [0.32.0] — 2026-11-22

### 新增
- **更新源仓库 URL 支持 GitHub**：自更新源此前硬编码按 Gitee 解析（API 路径与 access_token 查询串鉴权），填 GitHub URL 会拼出死地址。现按仓库 URL 的 host 识别平台——host 含 `github` 走 `api.github.com/repos/{o}/{r}/releases/latest` + `Authorization: Bearer` 头鉴权（带 `X-GitHub-Api-Version` 固定版本头）；其余（gitee.com / 自托管 Gitea 等 v1 兼容源）保持既有 Gitee 行为，自托管镜像不受影响。两平台 Release JSON 的 tag_name / assets / browser_download_url / digest 字段结构一致，附件仍认 `orcsy-update-*.zip` + 同名 `.sha256`（或 API `digest` 字段）
  - GitHub 私有仓库：附件下载鉴权头仅在内存留存（不进 update_state、不随 /api/state 外发）；httpx 跨域重定向自动剥离 Authorization，匹配 302 落预签名 CDN 的要求
  - GitHub 的 API 域名（api.github.com）与仓库域名不同：出网白名单在 repo 含 github.com 时自动推导放行，updater 另有运行时放行兜底，无需手工补 egress.extra_hosts
  - 看板设置页「应用」的更新源标签/占位符与 en / 繁中语言包同步更新；config.example.yaml 注释同步

## [0.31.1] — 2026-11-22

### 修复
- **升级安装线程非预期异常兜底**：0.30.0 的 `updater._auto_backup_config` 引用未导入的 `_Path`，每次手动升级都在安装线程抛 `NameError`；而 `_install_worker` 只捕获 `UpdateError`——异常无声杀死线程，无结果标记、无错误提示，前端挂单版本永不回归，看板遮罩永久卡「应用正常升级中」（0.31.0 包已修复 `_Path` 缺失）。本次加固：非预期异常一律落安装日志（含 traceback）、写失败结果标记（重启后如实弹窗）、翻 error 态，绝不静默；新增 `RESULT_REASONS[install-unexpected-error]` 文案与回归测试

## [0.31.0] — 2026-11-21

### 新增
- **Phase 2 扩展专业职能角色（§3.1）**：VALID_AGENT_ROLES 与 BUILT_IN_ROLES 新增三个标准角色——
  - **🛠️ 运维/DevOps 工程师（`devops`）**：容器化/CI/CD/K8s 声明/环境配置的编写维护（可写代码）
  - **👁️ UI 视觉审查专家（`ui_critic`）**：浏览器渲染页面的纯视觉把关，逐条 `report_bug` 派给前端修复（只读验证角色）
  - **🔒 安全合规审计工程师（`security`）**：变更范围静态扫描 + 依赖 CVE 检索 + 数据流风险识别（只读验证角色）
  - 三者均附内置职责模板（英文 markdown）+ `build_role_prompt_extra` 专属分支 + 看板角色选项/图标/语言包
- **空 worktree 自愈兜底**：任务沙盒建立完成后自动校验非空基线——worktree 检出零跟踪文件而基准分支有内容时，自动强制移除重建，消除实测偶发的「Agent 面对空目录只能 blocked」（约 1/8 运行）

### 变更
- **只读验证角色语义统一**：`ui_critic` / `security` 与 QA 同列「只读验证型角色」——报 Bug 后保持打开等回归（不误归档 done）；验收通过零文件改动时正常完成（不误判空转 blocked）。executor 两处 `team_role == "qa"` 特判改为 `in READONLY_VERIFY_ROLES`

## [0.30.0] — 2026-11-21

### 新增
- **冲突自愈（§3.3）**：worktree 成果合入主工作区冲突不再直接 blocked——标记续跑保留成果，同一角色下轮消解冲突后重新合并（限 2 次自愈，耗尽转人工）
- **DAG 可视化（§3.2）**：项目卡新增「依赖图 (DAG)」——分层 SVG 节点图，按状态着色、依赖边箭头、点击节点直达任务详情，无第三方依赖
- **按 Agent 聚合视图（④）**：用量页角色行可点击展开该角色最近运行清单
- 小组成员卡新增「接口 base_url」覆盖——混合多家接入点时逐成员独立指定端点

### 修复
- git 提交身份设置保存漏写（分块保存路径）
- RunSummaryT 类型补 turns 字段


### 新增
- 小组成员卡新增「接口 base_url」覆盖（留空继承预设/项目/全局）——混合多家接入点时每个成员可独立指定端点；「获取模型列表」按成员实际生效的 base_url 拉取

## [0.28.0] — 2026-11-21

### 新增
- **配置格式版本化（S1）**：`config_version` schema 世代号 + 注册表迁移器——旧世代配置加载即迁移，世代超前拒绝启动，存盘恒写当前世代；破坏性 schema 变更从此必须注册迁移器并附历史样本
- **历史配置回归集（S1）**：0.24 / 0.26 世代真实样本进入测试集，save→load 深度无损 + 字节级稳定断言覆盖全部 23 个顶层分区——系统性消灭「看板保存静默重置用户配置」类缺陷
- **配置备份/恢复（S2）**：`orcsy backup-config` / `orcsy restore-config` CLI；备份归档（config.yaml + sessions.json，chmod 600）支持本地目录与 WebDAV 推送；**升级前自动备份**（install_update 内置，失败不阻止升级）——兑现「升级失败可回滚」
- 熔断/恢复行为细节与文档同步（运行台账保留期指路等）

### 修复
- 看板登录密码迁至「应用」独立卡片：保存与调度/端口/日志保留解耦（旧实现改密码会顺带提交其它半编辑草稿）
- 子代理并发预算迁至「运行与成本」独立分区，保存与工具/命令/权限大包解耦

## [0.27.0] — 2026-11-21

### 新增
- **`llm.extra_body` 扩展请求体**：逐键合并进 chat/completions 请求（同名键覆盖内置字段）——中转/自建服务的兼容逃生舱（设置页 JSON 编辑框）
- **文本工具协议兜底（`tool_protocol: text`）**：服务端工具解析器与推理输出互斥的中转（think 混入正文触发解析 400）也能跑通完整任务——请求不带 tools，工具清单注入 system，模型正文输出 `<<TOOL_CALLS>>` 标记块由客户端解析；任务与审查循环共用
- Docker 镜像内置 **pytest**：Python 项目测试环境开箱即用
- 连接向导/二维码支持公网域名地址（public_url）

### 修复
- 文本协议双重转义还原（弱模型把 `\n` 写成字面 `\n` 导致文件单行化）
- 思考截断轮（推理烧满 max_tokens）不再硬失败，走 nudge 恢复路径
- 文本协议指令与 nudge 针对推理模型强化（标记块必须出现在正文）
- `save_config` 漏写 llm.extra_body（看板任意保存静默重置）；review.llm 写回同步补上
- 修复 pending_changes 缓存键缺陷（同项目名不同目录命中陈旧探测结果）

## [0.26.0] — 2026-11-20

### 新增
- 首次安装 7 步使用引导动画（含大模型接入与小组底座步骤）
- 窄窗口 / 移动端归档列表可见（轻量元数据）
- 看板响应提速（嵌套仓库 git 探测缓存，~0.9s → ~0.17s）

### 修复
- 项目页崩溃（回滚中断留下的懒加载断言）；设置页分组标签不随语言切换；手机连接弹窗关闭无反应
- 全站三语残留清零，繁體包全面修正

## [0.25.0] — 2026-11-17

### 新增
- Phase 1 团队记忆：项目架构黑板报（ARCHITECTURE.md 协同维护与开工透传）
- 需求批次跨 Agent 动态共享剪贴板（session_share/session_read，批次终态物理销毁）
- 任务详情页依赖前序成果一键查看与关联跳转
- 嵌套 Git 仓库兼容与人工把关提交

## [0.24.0] — 2026-11

### 新增（V1 可信流水线 R1~R6）
- 运行台账与回放（run_id → 事件 / diff / 测试 / 审查 / 用量）
- 幂等与断点续跑（任务指纹、租约心跳、提交前二次校验）
- 预算硬闸（token/成本上限，软停 + 硬停）
- 熔断停机与人工恢复
- 写操作审计（audit.jsonl）与统一出口白名单（egress）
- 移动端远程访问：受信代理真实 IP、Cookie+Bearer 双认证、连接向导与二维码、PWA

## [0.22.0] — 2026-11

### 新增
- 多模型项目小组（PM / 后端 / 前端 / QA）与 DAG 任务依赖引擎
- 强制 Worktree 沙盒隔离 + 人工把关提交
- 任务内只读子代理并发探索（delegate_explore）
- 防死循环四重熔断、QA 回归闭环（report_bug → 修复 → 回归）
- 登录密码 PBKDF2 密文存储与终端重置（orcsy reset-password）

## 签名与支持窗口

- Windows 安装包代码签名：**暂缓**。当前 GitHub 自动发布的 Windows 安装包与更新包均未签名；个人自签证书不建立公众信任，也不消除 SmartScreen 警告。本轮仅校验最终附件 SHA256（防损坏/错配，不认证发布者），S5 签名出口仍未完成。
- 1.x 支持窗口：只支持最新的 1.x minor 版本（该 minor 的补丁版本可继续修复）；发布新的 minor 后，旧 minor 不再接收 backport。
- 0.x：仅提供升级路径，不承诺 backport 或长期维护；0.x 修复进入当前开发线，用户应升级到最新版本。
