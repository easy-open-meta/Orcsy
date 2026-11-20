# Orcsy 三语 i18n 地区语境审校

> 状态：审校报告与修改建议，**未实施翻译修复**。词条及应用源码保持原样。

## 1. 结论

**目前尚未完全符合当地语境，也不能宣称三语界面已无漏译。**

- **大陆简中**：整体可理解；重点是操作语义、配置含义、安全承诺边界和提示清晰度，少量术语可统一。
- **台湾繁中**：既有明确错字/错译，也有机械简繁转换痕迹；另有大量台湾软件术语与风格统一建议。并非每个大陆用语都是错误。
- **美式英语**：整体可读，但部分按钮、动作状态、测试命令标签与安全说明含义不准；片段拼接和一词多义也造成不自然甚至误导的句子。
- **代码接线问题独立存在**：词典里有译文不等于页面实际使用；发现条件性运行时错误、静态缓存翻译、未接线系统确认、缺译及模板绑定问题。

## 2. 范围、快照与完整性

- 快照：审校完成时的工作区；基线 HEAD `857f902276c0630bacf5e57a566723f325ca9950`。20 个源码文件未变；`Dialogs.vue` 另有已逐字对账的5处并发表达式改动，已单列并复核运行语义（E12）。
- 报告生成时间（UTC）：`2026-11-20T06:48:50.714Z`。
- 英文包 **1,198** 条；繁中包 **1,199** 条；源文案/目录并集 **1,202** 个 key，另加 **3** 组语言名称、标题、描述元数据，共 **1,205** 个审校单元。
- 每种语言逐项审阅 **1,205** 个位置，合计 **3,615**；其中已有文本 **3,608** 格、缺失 **7** 格，缺失分布在 **5** 个 key。
- AST 解析 **19** 个前端源文件及两份词典，另读 `dashboard/index.html`；静态调用形态 **1,181** 个、字面量 key **911** 个；动态调用另作上下文复核。
- 两份词典重复 key 均为 **0**，解析诊断为 **0**。这是结构检查，不是语言质量通过证明。
- zhTW 按台湾语境审校；同包用于港澳浏览器不等于已完成港澳专门本地化。英语按美式编辑习惯审校；合法英式拼写不自动列为功能错误。

### 建议数量（不是错误率）

| 语言 | 保留建议 | P1 | P2 | P3 |
|---|---:|---:|---:|---:|
| 大陆简体中文 | 83 | 0 | 29 | 54 |
| 台湾繁体中文 | 415 | 0 | 324 | 91 |
| 美式英语 | 223 | 3 | 102 | 118 |

**合计721项保留建议，不等于721个确定错误。** 包括明确错译、缺译、语义澄清、上下文拆分及可选的地区风格统一。优先级表示建议复核顺序，不能代替逐项判断。完整当前值、建议、理由、定义位置、调用上下文及处理类型均在机器报告。

初审提出711项；主审结合源码否定6项建议，并执行86次修订/补充操作，最终保留721项。曾有7项待消歧，现均有源码依据并已记录结论。

## 3. 各语言代表问题

下表长文本可能节选，不能直接据此批量替换；完整内容以 JSON 为准。简中源 key 同时充当显示值，改源 key 需要调用方及目标语言同步迁移。

### 3.1 大陆简中

| ID | 当前文本 | 建议或处理方向 | 原因 | 源码出处 |
|---|---|---|---|---|
| K0029 | 当前实际并发 {p0} | 当前生效的并发上限 {p0} | SettingsView.vue:464-467/1993 显示配额池 limit，不是此刻正在进行的请求数；原文容易混淆上限与实时并发。 | `dashboard/src/locales/zhTW.ts:34`<br>`dashboard/src/views/SettingsView.vue:1993` |
| K0339 | 返工通过后收敛提交 | 返工审查通过后压缩合并提交（squash） | 该开关实际绑定 reviewDraft.squash_merge；「收敛提交」不是清楚的 Git 操作用语。 | `dashboard/src/locales/zhTW.ts:1099`<br>`dashboard/src/views/SettingsView.vue:1930` |
| K0528 | 允许访问的主机白名单（每行一个，防内网穿透；默认仅限回环） | 允许访问的主机白名单（每行一个，限制未经授权的内网访问；默认仅允许回环地址） | 本地调试主机限制与作为远程访问方式的「内网穿透」不是同一个概念，原提示会混淆用途。 | `dashboard/src/locales/zhTW.ts:1106`<br>`dashboard/src/views/SettingsView.vue:2221` |
| K0391 | 注入 run_command 子进程：国内网络拉依赖失败时配 GOPROXY、npm registry（HTTP_PROXY）、PIP_INDEX_URL 即可；不允许覆盖 PATH/SYSTEMROOT 等系统变量。 | 注入 run_command 子进程：下载依赖失败时，可按需配置 GOPROXY、npm_config_registry、PIP_INDEX_URL 等镜像源变量，或 HTTP_PROXY / HTTPS_PROXY 等代理变量；不允许覆盖 PATH、SYSTEMROO…〔节选；完整内容见 JSON〕 | npm registry 与 HTTP_PROXY 不是同一个配置项；mirrors.py:63 明确使用 npm_config_registry，应区分镜像源与 HTTP 代理。 | `dashboard/src/locales/zhTW.ts:1113`<br>`dashboard/src/views/SettingsView.vue:2276` |
| K0497 | 启用熔断停机 | 启用失败熔断（暂停项目） | 相关说明指暂停项目领取新任务，不是关闭整个应用或机器；「停机」的作用范围不够准确。 | `dashboard/src/locales/zhTW.ts:1094`<br>`dashboard/src/views/SettingsView.vue:1829` |
| K0898 | 应用自身 HTTP（模型/Issue/镜像/更新/网页抓取）只允许白名单主机；拒绝写审计。 | 应用自身的 HTTP 请求（模型/Issue/镜像/更新/网页抓取）只允许访问白名单主机；请求被拒绝时写入审计日志。 | 明确存在反向理解风险。「拒绝写审计」按通常语法可以读成拒绝写入审计记录，而同组 K0903「拒绝时写审计」表明要表达的是请求被拒绝时记录审计。补足「请求」「时」和「日志」，才能准确表达拒绝与审计记录的关系，同时保留只允许白名单主机的访问限制。 | `dashboard/src/locales/zhTW.ts:769`<br>`dashboard/src/views/SettingsView.vue:59` |

“模版→模板”、表述简化等属于规范化建议，应与动作含义错误分开处理。移动端提示不宜一概指向桌面的右上角或顶栏。

### 3.2 台湾繁中

| ID | 当前文本 | 建议或处理方向 | 原因 | 源码出处 |
|---|---|---|---|---|
| K0138 | 復製 | 複製 | 寫入剪貼簿的 copy 是「複製」，「復製」是繁轉用字錯誤。 | `dashboard/src/locales/zhTW.ts:149`<br>`dashboard/src/App.vue:401` |
| K0160 | 已歸檔（done）的任務不受影應；任務檔案將被刪除，不可恢復。 | 已歸檔（done）的任務不受影響；其餘任務檔案將被刪除，且無法復原。 | 「影應」為錯字；清空確認需明確區分不受影響的已歸檔任務與將刪除的檔案。 | `dashboard/src/locales/zhTW.ts:171`<br>`dashboard/src/views/DashboardView.vue:344` |
| K0474 | 審查透過 | 審查通過 | 此標籤對應 pass，指符合審查要求；「透過」是經由某個途徑，不能當判定結果。 | `dashboard/src/locales/zhTW.ts:1126`<br>`dashboard/src/views/UsageView.vue:46` |
| K0375 | 視窗結束後自動恢復 | 暫停滿一個統計時段後自動恢復 | 設定旁的說明與 res_window 分鐘欄位證明是熔斷後的時間長度，不是應用程式視窗關閉；現譯把兩種 window 混淆。 | `dashboard/src/locales/zhTW.ts:1095`<br>`dashboard/src/views/SettingsView.vue:1846` |
| K0598 | 回傳專案 | 返回專案頁 | 返回箭頭執行 backToProjects，是頁面導覽，不是把專案資料傳回。 | `dashboard/src/locales/zhTW.ts:470`<br>`dashboard/src/views/CommitView.vue:249` |
| K0628 | 下載修補程式 | 下載修補檔 | 下載產物是供檢視 diff 的 .patch 資料檔，而非可執行的修補程式；應稱「修補檔」或 patch 檔。 | `dashboard/src/locales/zhTW.ts:500`<br>`dashboard/src/views/CommitView.vue:314` |

其他明确例子包括「每輪最多工數」「安全限製」「強製啟用」，以及全栈开发误作「全堆疊」。但「並發／並行」「記錄／紀錄」「覆蓋／覆寫」「平臺／平台」等应按上下文与产品词表统一，不全都判作错字。

### 3.3 美式英语

| ID | 当前文本 | 建议或处理方向 | 原因 | 源码出处 |
|---|---|---|---|---|
| K0067 | Exit | Sign out | 该按钮调用登出操作；“Exit”通常表示退出应用或页面，不能准确说明会结束登录会话。 | `dashboard/src/locales/en.ts:77`<br>`dashboard/src/App.vue:283` |
| K0567 | AI generate | Generate with AI | 按钮标签应使用自然的动词短语；“AI generate”是中文词序的直译。 | `dashboard/src/locales/en.ts:579`<br>`dashboard/src/views/CommitView.vue:340` |
| K0597 | Human-gated commit | Manual review and commit | “Human-gated commit”不自然，且把人工把关机械写成 gate；该页面是人工检查变更并完成提交，建议用清楚的功能名称。 | `dashboard/src/locales/en.ts:611`<br>`dashboard/src/views/CommitView.vue:250` |
| K0973 | QA | Test command | 项目表单这里是 pDraft.test 命令输入（示例 npm test），不是 QA 角色；与 Run command 的标签形式一致。 | `dashboard/src/locales/en.ts:1007`<br>`dashboard/src/components/Dialogs.vue:1442` |
| K0470 | Committing | Will commit | 此處是提交前的檔案數摘要，不是正在提交；使用未來動作，且和後接數量組合自然。 | `dashboard/src/locales/en.ts:482`<br>`dashboard/src/views/CommitView.vue:352` |
| K0753 | Saved: dashboard login disabled | Saved: dashboard password protection is off. | 清除的是看板存取密碼，不是禁用登入入口；其他 API key 驗證仍可能存在。應說明密碼保護移除，不能把它寫成無法登入或泛稱所有驗證必定關閉。 | `dashboard/src/locales/en.ts:773`<br>`dashboard/src/views/SettingsView.vue:947` |

另需复核「内建/自行建立」来源标签、命令执行误作 command changes、日志自动打开与日志存在性混淆，以及内存映射不落盘被扩大成所有敏感数据从不落盘的承诺。合法开发者术语如 egress、mutating、circuit breaker 无需为了口语化而全部删除。

## 4. 代码接线与实际语境问题

以下问题不能只靠润色词典解决。源码级探针使用当前翻译内核、真实 Vue ref/computed、当前词典和 AST 提取表达式，配合模拟 DOM/存储与合成数据。**探针运行成功表示成功复现缺陷，不是产品验收全绿。**

| ID / 优先级 | 发现与影响 | 证据 | 后续修复方向 |
|---|---|---|---|
| E01 / P1 | **任务依赖选项的译者名称被任务变量遮蔽**<br>条件成立时抛出 TypeError: t is not a function；不是所有任务弹窗无条件崩溃。 | `dashboard/src/components/Dialogs.vue:868-876`<br>JSON `probes.results.currentSourceLevel.completedDependency` | 重命名局部任务变量并保留翻译函数，增加含完成任务的依赖选项测试。 |
| E02 / P1 | **密码警告使用普通引号，插值和系统文案都未正确处理**<br>文本保留字面量 ${pwd}，没有插入模拟密码；相关警告前缀还是简体中文。 | `dashboard/src/components/Dialogs.vue:1170-1177`<br>JSON `probes.results.currentSourceLevel.passwordWarning` | 用完整可翻译句子和参数替换构造警告；仅在隔离测试中使用合成密码，不在报告、控制台或测试证据中记录真实密码。 |
| E03 / P1 | **高影响操作确认存在未接入翻译的前端系统文本**<br>丢弃改动、删除技能、强制停止、重启等确认仍可能显示简体中文；词典已有近似文案不代表调用已接入。 | `dashboard/src/views/CommitView.vue:104-107`<br>`dashboard/src/views/SkillsView.vue:45-52`<br>`dashboard/src/views/DashboardView.vue:234-235`<br>`dashboard/src/views/ProjectsView.vue:202-203`<br>`dashboard/src/App.vue:152` | 将系统确认完整接入 t/tFmt，并保留任务标题、文件名等用户数据原文；仅测试确认展示，不实际执行破坏性操作。 |
| E04 / P2 | **初始化时翻译导致切换语言后保留旧字符串**<br>角色、权限、执行结果与审查标签等初始化为普通字符串；KeepAlive 或长期挂载组件不会自动重算。四组已在真实 Vue/当前翻译内核的源码级探针中复现。 | `dashboard/src/components/Dialogs.vue:62-67`<br>`dashboard/src/components/Dialogs.vue:272-278`<br>`dashboard/src/views/SettingsView.vue:489`<br>`dashboard/src/views/SettingsView.vue:624`<br>`dashboard/src/views/UsageView.vue:36-49`<br>JSON `probes.results.currentSourceLevel.cachedTranslations` | 保存源 key，渲染时翻译，或在 computed 中建立 options/maps；72 个初始化候选不等于72个已复现用户缺陷。 |
| E05 / P2 | **固定风险标签及内置分类元数据显示原始中文**<br>部分固定风险名和内置分类 label/description 直接渲染，不能仅靠添加词条解决。 | `dashboard/src/components/Dialogs.vue:232-236`<br>`dashboard/src/components/Dialogs.vue:1667`<br>`dashboard/src/components/Dialogs.vue:1683`<br>`dashboard/src/views/SettingsView.vue:348-352`<br>`dashboard/src/views/SettingsView.vue:2013-2014`<br>`dashboard/src/views/SettingsView.vue:2031`<br>`dashboard/src/views/SettingsView.vue:2081-2082` | 为内置系统元数据使用稳定代码或可翻译源 key；不要把用户自定义分类名和描述自动翻译。 |
| E06 / P2 | **片段拼接和共用多义词破坏句意**<br>探针显示 Auto-startEnable / Auto-startClose、Settings web.auth_password as...、重复 Only the 等组合；未启用认证时的前后句也存在含义冲突。恢复在草稿、排除文件和暂停项目里应分别表达不同动作。 | `dashboard/src/components/Dialogs.vue:1903`<br>`dashboard/src/components/Dialogs.vue:1915-1916`<br>`dashboard/src/views/SettingsView.vue:1128`<br>`dashboard/src/views/SkillsView.vue:105-106`<br>JSON `probes.results.currentSourceLevel.composedEnglish` | 改用完整条件句和有上下文的 key；不要全局把 Settings 改成 Set、Close 改成 Off，或把重新纳入文件误写为 Git restore。 |
| E07 / P2 | **两条实际调用文案缺失两种目标语言**<br>编辑冲突警告与 AI 多行提交占位文案在英文/繁中均回退简中，共4格；冲突源字符串还含字面量反斜线 n。 | `dashboard/src/components/Dialogs.vue:1072`<br>`dashboard/src/views/CommitView.vue:344`<br>JSON `probes.results.currentSourceLevel.fallbacks` | 调用方转义修正、源 key 与两份语言包同步迁移；警告需准确区分覆盖最新版本和放弃未保存修改。 |
| E08 / P2 | **嵌套仓库 tooltip 的绑定被解析为静态属性**<br>属性名包含多余引号，Vue AST 将 :title 表达式当成静态属性，tFmt 不会按预期执行。 | `dashboard/src/views/ProjectsView.vue:384`<br>JSON `probes.results.currentSourceLevel.nestedRepositoryTooltip` | 修正绑定语法并用含嵌套仓库的合成项目验证 tooltip。 |
| E09 / P3 | **数量、单位和地区格式需要上下文而非后缀替换**<br>通用 tasks 后缀会产生1 tasks；用量单位默认 zh，不跟随初次界面语言，中文单位格式器还输出简体万/亿。数字分组在当前三地区相同，不把硬编码 zh-CN 单独判为错误数值。 | `dashboard/src/views/UsageView.vue:15-22`<br>`dashboard/src/views/UsageView.vue:33`<br>`dashboard/src/utils.ts` | 按数量使用完整模板或复数规则；首次默认单位跟随语言，保留用户已保存的单位选择；英文用户主动选中文单位不是漏译。 |
| E10 / P3 | **目录不对称和伪占位符需清理但不能冒充在线故障**<br>3格缺失位于未发现静态调用的旧/近似 key；App 实际使用带换行前缀的更新文案且有翻译。repo &#124;&#124; 主仓库是旧伪表达式，tFmt不执行JS默认值表达式。两份包重复 key 均为0。 |  | 确认运行构造调用后再移除或统一；不能用目录存在证明当前界面调用，更不能直接把伪表达式当JS执行。 |
| E11 / P3 | **后端消息和生成内容需要单独定义语言边界**<br>后端系统 message 和 AI 内容仍有中文来源，不是只修改前端 pack 就能解决；第三方 Naive locale 已响应式接线。 | `dashboard/src/api.ts`<br>`dashboard/src/lang.ts`<br>`dashboard/src/App.vue` | 系统消息优先代码加参数；AI生成内容单独定义语言策略。用户任务、日志、命令、模型名、技能内容保持原文。 |
| E12 / P1 | **弹窗中的括号开头语句被解析为上一行的函数调用**<br>当前 Dialogs.vue 的消息桥接初始化在隔离执行中抛出 TypeError；任务冲突分支在有最新任务时未调用警告，无最新任务时也会抛出 TypeError。语法解析没有报错，但运行含义已改变。 | `dashboard/src/components/Dialogs.vue:18-20`<br>`dashboard/src/components/Dialogs.vue:1068-1072`<br>JSON `probes.results.currentSourceLevel.dialogStatementBoundary` | 将桥接赋值及冲突对话框调用写成独立语句，再验证应用初始化及任务冲突警告。此项由收口时的源文件变更引入审校范围，与词典翻译本身分开处理。 |

### 4.1 关键复现实证

- 依赖候选含已完成任务时：`TypeError: t is not a function`；条件之外不推断为必现崩溃。
- 切换后权限串：`Follow global (current: 变更前询问)`；角色与执行结果仍是旧语言，重新翻译后的对照值正确。
- 英文拼接：`Auto-startEnable` / `Auto-startClose`；完整探针输出见 JSON。
- E12 的语句边界问题已作为当前源码的附加发现单独记录；它不是三语词典审校结论。
- 密码警告保留字面量插值，模拟值没有被插入；不展示、不测试真实凭据或公开连接。
- 嵌套仓库 tooltip 被 Vue 解析为静态属性，非正常动态绑定。

### 4.2 原始中文及初始化候选的复核

- 对169处原始字符串/模板候选逐项结合源码分类；其中91处属于需要翻译接线的前端系统文案出现位置。91不是独立功能数，也不是唯一字符串数。
- 其他候选包括运行时才翻译的引导/设置元数据、路由元数据、明确选择的中文数值单位、开发诊断、重复父子表达式及嵌套翻译误报，未简单算成漏译。
- 72个初始化翻译候选中，有4组完成源码级复现；不能宣称72个全是已见界面故障。弹窗标题初始 ref 会在打开时重设，不另算冻结缺陷。
- 内置分类元数据应与自定义名称区分；原始任务内容、日志、diff、模型名及技能正文不应被自动翻译。

## 5. 缺译与占位符清单

| ID | 缺失语言 | 是否有当前调用证据 | 说明 |
|---|---|---|---|
| K1201 | 台湾繁体中文、美式英语 | 是 | 编辑冲突警告；源字符串另含字面量反斜线n，需调用方与词典同步迁移。 |
| K1202 | 台湾繁体中文、美式英语 | 是 | AI多行提交说明的输入占位文案。 |
| K0272 | 台湾繁体中文 | 未发现；目录差异 | 旧{n}时长key；附近{m}版本有翻译，未发现该key静态调用。 |
| K1199 | 美式英语 | 未发现；目录差异 | 无换行前缀的旧安装日志文案；App实际使用带前缀且有翻译的key。 |
| K1200 | 美式英语 | 未发现；目录差异 | 无换行前缀的旧更新提示；App实际调用的带前缀版本有翻译。 |

因此，7格目录缺失中，**4格对应2个已确认调用的key**，另3格属于目录不对称，不能冒充3个已复现在线泄漏。

旧词条K0648中的 `{repo || 主仓库}` 与 `{repo}` 不一致是目录问题：`tFmt` 进行字面参数替换，不执行JS默认值表达式；未发现静态调用，不声称已造成运行时变量丢失。对提案里的花括号也须分清真实占位符、代码示例与结构重写指令。

## 6. 术语与地区用语建议

| 含义 / 场景 | 大陆简中 | 台湾繁中 | 美式英语 |
|---|---|---|---|
| 用户工作项目 | 项目 | 專案 | Project |
| 列表元素 | 项 / 条目 | 項目 | Item / entry |
| 应用配置页 | 设置 | 設定 | Settings |
| Git repository | 仓库 | 儲存庫（或存放庫，选定后统一） | Repository |
| 只读访问 | 只读 | 唯讀 | Read-only |
| 队列与并发上限 | 队列 / 并发上限 | 佇列 / 並行數上限 | Queue / concurrency limit |
| 软件包 / 用户定制 | 包 / 自定义 | 套件 / 自訂 | Package / custom |
| 执行记录与原始日志 | 执行记录 / 日志 | 執行紀錄 / 日誌 | Run history / logs |
| 模型审查与人工审批 | 审查 / 批准 | 審查 / 核准 | Review / approve |
| 草稿恢复 | 恢复草稿 | 還原草稿 | Restore draft |
| 文件重新纳入提交 | 重新纳入 | 重新納入 | Include again |
| 暂停项目继续运行 | 恢复运行 | 繼續執行 | Resume |
| 内置与用户创建来源 | 内置 / 自行创建 | 內建 / 自行建立 | Built-in / created |
| CNY成本金额 | 人民币元（CNY） | 人民幣元（CNY） | CNY |

这些是产品词表建议，不要求把第三方官方术语改名，也不改命令、API、配置字段、枚举和用户输入。Token需区分模型token与访问令牌，不做无上下文全局替换。

### 不可机械全局替换

- 「透過」表示借由某方式时成立；审查合格是「通過」，不能把所有透過替换。
- 「回傳」用于返回数据；导航应「返回」。GUI视窗、上下文视窗与统计时间窗也要区分。
- 製作中的「製」不是强制/限制的「制」；複製/重複不等于恢复；專案不等于列表項目。
- 英文 Settings是名词，Set是动词；Close、Off、Disable及Sign out按动作拆开。
- “1 tasks”不能简单把所有tasks后缀替换成total，应使用数量模板或复数规则。

## 7. 已完成的语义消歧与否决依据

7项初审待消歧问题均已结合源码解决，详见 JSON `contextResolutions`。其中关键依据如下：

| 问题 | 已核实结论 | 证据 |
|---|---|---|
| 密码是加密还是哈希 | PBKDF2-HMAC-SHA256带盐哈希；英语hash并未译错。不能顺着中文“密文”把英文改成encrypted。中文建议精确说密码哈希/密碼雜湊。 | `orcsy/auth.py:29-45`、`orcsy/settings_service.py:146-160` |
| 台湾金额“元”的币种 | 配置明确是CNY；标签应说人民币，不随界面改成TWD/USD，也不改数值。 | `orcsy/config.py:273-288`、`orcsy/budget.py:205-209`、双语指南成本章节 |
| 脱敏不落盘的范围 | 内存映射不落盘，不等于全部原始敏感信息永不出现在磁盘；模型请求体脱敏也不等于认证头脱敏。finish不还原，其余工具参数在分派前还原。 | `orcsy/llm.py:760-773`、`orcsy/masking.py` |
| 保留天数为0 | 关闭按时间归档/清理，不排除独立的总大小上限清理。 | `orcsy/logging_setup.py:74`、`:90`、`:142` |
| 人工最终提交 | 是默认策略，不是无条件承诺；开启commit_and_merge可自动提交合并。沙箱内部WIP提交与最终人工提交是不同层面。 | `orcsy/executor.py:1009-1014`、`orcsy/config.py` |
| npm registry与HTTP_PROXY | 前者是包源地址，后者是网络代理，不能当作同义参数。 | `orcsy/mirrors.py:63` |
| 停机与熔断暂停 | 暂停领取新任务不等于整个应用停机；长超时提示中的强制停止也应只指可被停止的任务执行。 | 对应词条调用上下文及当前配置/调度源码 |

初审被否决的6项及原因保存在 JSON `curation.rejectedInitialSuggestions`；另有86次修订或补充，其中英文 hash 的初审误判已纠正，不应依据不精确源文案改成 encrypted。合法地区变体与开发者用词也不应自动抹除。

## 8. 后续修复顺序（本报告未执行）

1. 优先修复E01–E03、E12与高影响警告语义，补含完成任务、警告插值、破坏性确认展示和语句边界的隔离测试。
2. 修正E04–E08的响应式翻译、完整句子、上下文key、缺译及模板绑定；同步迁移源key、调用方和两份词典。
3. 按模块审阅721项建议，先确定术语表，再逐批处理台湾错字/错译、英语动作状态及简中准确性。结构重写建议不能直接粘贴为译文。
4. 为数量和单位使用地区感知规则；保留已保存的用户选择，不因切换语言换算币值或翻译用户内容。
5. 每批检查AST重复key、占位符、调用覆盖及切换响应性；构建后再做桌面/移动端三语真实GUI回归，包含长文本、弹窗、下拉选项和确认警告。

## 9. 证据等级、方法与限制

- **B级：当前源码/目录事实**。AST提取、定义与调用行号、后端实现、21个保护文件SHA256，以及完整逐项审阅账本。
- **C级：针对性源码级执行**。真实Vue和当前lang内核/词典的隔离探针，复现冻结翻译、条件错误、缺译回退、拼接及静态属性解析；另对当前Dialogs语句边界变化做了隔离执行复核；不是整页浏览器验收。
- **编辑判断（非事实证据等级）**。给出建议、理由、置信度及地区假设；不冒充母语用户调研或唯一正确译法。
- **A级：官方语体参考（原则而非强制字典）**：微软官方本地化风格指南提供自然、准确、上下文与一致性的原则；其产品语气不是Orcsy强制规范。
- 本轮未触发 Orcsy 的真实业务模型请求，未读取生产配置或凭据；未执行真实丢弃、删除、停止、重启或公开访问操作，未修改应用词典，未运行与报告无关的构建或全量回归。
- 新建议尚未渲染到产品，因此没有对“修改后”做长度适配/GUI验收，也不宣称旧的全功能零报错验收目标已达成。
- 后端动态系统message、AI生成语言及第三方库所有内置文案需另定范围；用户任务内容、日志、技能正文不属于应强制翻译的系统词条。Naive UI已有响应式locale接线。

### 官方参考（已读取）

- Microsoft Writing Style Guide: Word choice：`https://learn.microsoft.com/en-us/style-guide/word-choice/`；自然、准确、熟悉的措辞与一致术语；不是Orcsy必须遵循的专有产品口吻。
- Microsoft Chinese (Simplified) Localization Style Guide：`https://download.microsoft.com/download/1/5/9/159cb91c-b61b-4385-97ca-80ccc7ff1fa0/zho-chn-StyleGuide.pdf`；已读取自然性、可理解性和上下文相关章节。
- Microsoft Chinese (Traditional, Taiwan) Localization Style Guide：`https://download.microsoft.com/download/2/9/3/293e6d41-ba40-451b-a41e-94bdb6242ae3/zho-twn-StyleGuide.pdf`；已读取台湾语法、语境和避免逐字翻译相关章节；不代表所有台湾用户偏好完全一致。

## 10. 交付核验清单

| 用户目标 / 约束 | 对应产物与实证 | 核验边界 |
|---|---|---|
| 扫描当前三语全部i18n | JSON coverage、6个批次逐ID覆盖、reviewLedger的1,205×3格 | 覆盖源/目录并集及3组元数据，不将任意用户内容算系统译文 |
| 判断是否符合当地语境 | 721项完整当前值/建议/理由/出处，地区词表与代表例子 | 建议不等于确定错误；未做母语用户研究 |
| 核对含义和实际调用 | 7项消歧、169处源文字面量分类、72处初始化候选、E01–E12与当前隔离探针输出 | 不把目录存在当作界面接线、不把探针通过当产品全绿 |
| 结果可复查 | `docs/i18n-audit.md`、`docs/i18n-audit-findings.json` | JSON包含完整建议与覆盖账本，不仅是摘要 |
| 不破坏现有代码/数据 | 20个源码文件未变；Dialogs.vue 的5处已知改动逐字对账后，报告完成时再核验全部21个当前哈希一致；只新增两份报告 | 未实施修复、未提交、未推送 |

### 机器报告阅读提示

- `findings`：完整721项建议；`remediationType`区分单值翻译、源key迁移、上下文拆分、整句模板等。
- `reviewLedger`：每个单元、每种语言的当前值、是否缺失、是否有建议；no issue flagged仅表示未标记问题，不是全场景正确认证。
- `technicalIssues` / `probes`：代码接线发现和实际复现结果。
- `missingTranslations` / `sourceLiteralReview` / `initializationReview`：缺译矩阵、原始文本分类与初始化检查范围。
- `sourceSnapshot` / `contextResolutions` / `curation`：源码快照、消歧结论和审校修正轨迹。

**最终状态：本次新增两份审校报告，未修改语言包或应用源码，未提交/推送。后续修复仍需逐项审阅和独立验收。**

## 11. 高优先级修复结果（2026-11-20）

本轮已应用**高优先级**修复，未做全局地区风格替换：

- 迁移简中源 key：K0029、K0352、K0390、K0497、K0528、K0898、K1035、K0753、K0892（调用点与两份语言包同步）。
- 更新英文词条 44 条、繁中词条 55 条，均为明确错字、语义反转或缺译。
- 补齐 K1201 编辑冲突警告、K1202 提交说明占位文案的英/繁翻译；修正 K1201 的字面量反斜线换行。
- 新增「开放手机访问并沿用现有密码」词条，密码警告改为参数注入且两条分支都走翻译。
- 修复 3 个 P1 接线问题：任务依赖选项的译者名称遮蔽（E01）、密码警告插值与未翻译分支（E02）、Dialogs 语句边界（E12）。
- 构建通过，`dashboard_html.py` 与最终构建一致；语言包无重复 key；AI 提交说明回归通过。
- 详细证据见 JSON `postFixStatus`。E03–E11 与 P2/P3 建议仍保留为提案，未在本轮处理。
