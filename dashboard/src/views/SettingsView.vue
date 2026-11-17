<script setup lang="ts">
import { NButton, NCard, NDynamicTags, NInput, NInputNumber, NProgress, NSelect, NSwitch, NTag, NTabs, NTabPane } from 'naive-ui'
import { computed, onUnmounted, reactive, ref, shallowRef, watch } from 'vue'
import { api, type AuditEntryT, type MirrorRowT, type ModelInfo } from '../api'
import { beginRestartWait, openDirBrowser, settingsSection, store } from '../store'
import { cmpVersion, fmtTokens } from '../utils'
import { miIcon } from '../mi'
import { tNow as t, tFmt } from '../lang'

const message = () => window.$message

// ---- 功能分区：左侧「一级分类 → 子项」菜单，右侧只渲染当前分区（懒加载） ----
// key 与 settingsSection 联动（跨视图跳转定位，如看板「设置」按钮直达对应分区）
interface SettingItem { key: string; label: string; icon: string; desc: string }
interface SettingGroup { key: string; label: string; icon: string; items: SettingItem[] }

const SETTING_GROUPS: SettingGroup[] = [
  {
    key: 'model', label: t('模型与团队'), icon: 'smart_toy',
    items: [
      { key: 'llm', label: t('大模型接入'), icon: 'smart_toy',
        desc: t('OpenAI 兼容协议覆盖 DeepSeek / GLM / Kimi / 通义 / Ollama 等；保存后立即生效。') },
      { key: 'profiles', label: t('多模型项目小组 (Team)'), icon: 'group',
        desc: t('配置 PM / 后端 / 前端 / QA 角色专属大模型与 API Key 预设池，供各项目 Team 引用。') },
      { key: 'review', label: t('任务审查（Review Agent）'), icon: 'rate_review',
        desc: t('独立模型合并前审查任务成果；未配置审查模型时审查未启用。') },
    ],
  },
  {
    key: 'runtime', label: t('运行与成本'), icon: 'schedule',
    items: [
      { key: 'sched', label: t('调度与看板'), icon: 'schedule',
        desc: t('扫描间隔、并行数保存后立即生效；看板端口下次启动生效。') },
      { key: 'budget', label: t('预算与成本'), icon: 'savings',
        desc: t('任务级 token/成本上限与项目当日上限；超限停止执行，未配置单价时只算 token。') },
      { key: 'resilience', label: t('熔断与恢复'), icon: 'health_and_safety',
        desc: t('连续失败或窗口失败率超阈值时暂停项目领取新任务；默认人工在看板恢复。') },
    ],
  },
  {
    key: 'data', label: t('数据与合规'), icon: 'security',
    items: [
      { key: 'masking', label: t('数据脱敏'), icon: 'security',
        desc: t('密码/密钥等敏感信息发送模型厂商前在本地掩除；只在内存处理、不写磁盘，任务结束即清除。') },
      { key: 'runlog', label: t('运行台账'), icon: 'receipt_long',
        desc: t('每个任务一轮执行留一份可回放证据（事件 / diff / 测试 / 审查 / 用量）；保存后立即生效。') },
      { key: 'audit', label: t('审计日志'), icon: 'history',
        desc: t('登录、任务/项目/设置变更、提交确认与 API Key 写调用留痕；只读，不记录密钥与正文。') },
    ],
  },
  {
    key: 'tools', label: t('工具与网络'), icon: 'handyman',
    items: [
      { key: 'tools', label: t('Agent 工具与访问权限'), icon: 'handyman',
        desc: t('全局工具开关、命令白名单与访问权限模式；项目未覆盖时按这里生效。') },
      { key: 'mirrors', label: t('镜像源管理与测速选源'), icon: 'sync_alt',
        desc: t('Agent 装环境/拉依赖前测速，哪个快用哪个。') },
      { key: 'egress', label: t('出网白名单'), icon: 'lan',
        desc: t('应用自身 HTTP（模型/Issue/镜像/更新/网页抓取）只允许白名单主机；拒绝写审计。') },
    ],
  },
  {
    key: 'app', label: t('通知与应用'), icon: 'notifications',
    items: [
      { key: 'notify', label: t('提醒设置'), icon: 'notifications',
        desc: t('任务完成/受阻时提醒；手动停止不打扰。保存后立即生效。') },
      { key: 'app', label: t('应用'), icon: 'settings',
        desc: t('更新源、自更新检查、看板登录密码与开机自启动。') },
    ],
  },
]

const SECTION_GROUP: Record<string, string> = Object.fromEntries(
  SETTING_GROUPS.flatMap(g => g.items.map(it => [it.key, g.key])))
const openGroups = reactive<Record<string, boolean>>({})

// 移动端：分类下拉 + 子项胶囊（窄屏不渲染整棵手风琴，避免菜单吃掉半屏）
const mobileGroup = ref(groupOfSection(settingsSection.value))
const groupOptions = SETTING_GROUPS.map(g => ({ label: g.label, value: g.key }))
const mobileGroupItems = computed(() =>
  SETTING_GROUPS.find(g => g.key === mobileGroup.value)?.items || [])

watch(mobileGroup, (key) => {
  const group = SETTING_GROUPS.find(g => g.key === key)
  if (!group) return
  if (!group.items.some(it => it.key === settingsSection.value)) {
    settingsSection.value = group.items[0].key
  }
})

function groupOfSection(key: string): string {
  return SECTION_GROUP[key] || SETTING_GROUPS[0].key
}

function toggleGroup(key: string): void {
  const wasOpen = !!openGroups[key]
  for (const k of Object.keys(openGroups)) openGroups[k] = false
  if (!wasOpen) openGroups[key] = true
}

function selectSection(key: string): void {
  settingsSection.value = key
  const g = groupOfSection(key)
  if (!openGroups[g]) {
    for (const k of Object.keys(openGroups)) openGroups[k] = false
    openGroups[g] = true
  }
}

// 跨视图定位 / 刷新后恢复：自动展开当前分区所属分类
watch(() => settingsSection.value, (key) => {
  const g = groupOfSection(key)
  mobileGroup.value = g
  for (const k of Object.keys(openGroups)) openGroups[k] = false
  openGroups[g] = true
}, { immediate: true })

// ---- 草稿模式：首份数据到后回填一次，之后轮询不覆盖未保存的输入 ----
const draft = reactive({
  provider: 'openai' as 'openai' | 'anthropic',
  model: '', base_url: '', api_key: '',
  temperature: 0.2, max_tokens: 8192, max_context_tokens: 131072,
  enable_thinking: false, timeout_seconds: 300,
  interval: 300, parallel: 1, port: 5116, retention: 7, max_total_mb: 200,
  // 运行台账（V1-R1）：开关 + 保留期 + 占用上限 + 自定义目录（留空 = 默认）
  runlog_enabled: true, runlog_retention: 30, runlog_max_mb: 500, runlog_dir: '',
  // 预算硬闸：0 = 不限制；单价表在 config.yaml 维护
  budget_enabled: false, budget_task_tokens: 0, budget_day_tokens: 0,
  budget_task_cost: 0,
  // 熔断停机（V1-R4）：rate 用百分比展示，保存时 /100
  res_enabled: true, res_consec: 3, res_window: 120,
  res_rate_pct: 60, res_samples: 5, res_auto: false,
  // 统一出口白名单（V1-R6）：extra_hosts 只填主机名（可带端口 / *.通配）
  egress_mode: 'allowlist' as 'allowlist' | 'off',
  egress_extra: [] as string[],
  egress_audit: true,
})
// ---- 任务审查（Review Agent）草稿：模型绑定 + 返工次数 + 测试命令 + squash ----
const reviewDraft = reactive({
  enabled: true,
  provider: 'openai' as 'openai' | 'anthropic',
  model: '', base_url: '', api_key: '',
  max_turns: 10, max_review_rounds: 2,
  allow_test_commands: true, squash_merge: true,
})
const reviewKeyMasked = shallowRef('')
const reviewKeyConfigured = shallowRef(false)
const reviewKeyTouched = shallowRef(false)
const reviewSaving = shallowRef(false)
const reviewModelOptions = ref<ModelInfo[]>([])
const reviewFetching = shallowRef(false)
// ---- 密钥/令牌三态（H2）：后端只回掩码，不回填明文。未触碰的字段保存时不带
//      对应的键（后端保持原值）；触碰过（含又清空）才上报，空串 = 显式清除 ----
const apiKeyMasked = shallowRef('')
const apiKeyConfigured = shallowRef(false)
const apiKeyTouched = shallowRef(false)
const updTokenMasked = shallowRef('')
const updTokenConfigured = shallowRef(false)
const updTokenTouched = shallowRef(false)
// ---- 软件更新配置草稿 ----
const updDraft = reactive({ repo: '', token: '', auto_check: true })
// 掩码占位跟随轮询刷新（保存新密钥/令牌后不用刷新页面就能看到新掩码；
// 只动展示用的掩码，不碰用户正在输入的草稿）
watch(() => store.state?.llm.api_key_masked, (v) => {
  if (v !== undefined) apiKeyMasked.value = v
})
watch(() => store.state?.llm.key_configured, (v) => {
  if (v !== undefined) apiKeyConfigured.value = v
})
watch(() => store.state?.update_cfg?.token_masked, (v) => {
  if (v !== undefined) updTokenMasked.value = v
})
watch(() => store.state?.update_cfg?.token_configured, (v) => {
  if (v !== undefined) updTokenConfigured.value = v
})
// ---- Agent 工具开关草稿（全局默认；常规工具未列出 = 启用，本地调试工具未列出 = 关闭） ----
const toolsCatalog = ref<{ name: string; description: string; group?: string | null }[]>([])
const toolsDraft = reactive<Record<string, boolean>>({})

const browserTools = [
  'browser_navigate', 'browser_snapshot', 'browser_click',
  'browser_type', 'browser_wait', 'browser_screenshot',
  'browser_console', 'browser_network', 'browser_eval',
  'browser_close',
]
const devServerTools = [
  'dev_server_start', 'dev_server_logs', 'dev_server_stop',
]

const browserGroupEnabled = computed({
  get: () => browserTools.some(k => toolsDraft[k]),
  set: (val: boolean) => {
    for (const k of browserTools) toolsDraft[k] = val
  },
})

const devServerGroupEnabled = computed({
  get: () => devServerTools.some(k => toolsDraft[k]),
  set: (val: boolean) => {
    for (const k of devServerTools) toolsDraft[k] = val
  },
})

// ---- 三级页面导航与分组控制（Agent 工具与命令白名单） ----
const toolsTab = shallowRef<'tools' | 'commands' | 'debug' | 'perms'>('tools')
const cmdSearch = shallowRef('')
const toolSearch = shallowRef('')

const defaultToolCategories = [
  {
    key: 'file_ops',
    label: t('文件操作与编辑'),
    icon: 'folder',
    description: t('读取、创建、编辑、重命名及删除工作区代码和文本文件'),
    tools: ['read_file', 'write_file', 'edit_file', 'multi_edit', 'delete_file', 'move_file', 'list_dir'],
  },
  {
    key: 'code_search',
    label: t('代码搜索与定位'),
    icon: 'search',
    description: t('按通配符模式查找文件以及跨文件正则搜索内容'),
    tools: ['find_files', 'grep'],
  },
  {
    key: 'command_exec',
    label: t('终端命令执行'),
    icon: 'terminal',
    description: t('在项目根目录执行白名单内的构建、测试、检查与系统命令'),
    tools: ['run_command'],
  },
  {
    key: 'task_collab',
    label: t('任务管理与协议'),
    icon: 'task_alt',
    description: t('拆解多阶段子任务并在任务完成或受阻时给出结构化答复'),
    tools: ['create_tasks', 'finish'],
  },
  {
    key: 'session_bus',
    label: t('批次共享状态'),
    icon: 'share',
    description: t('同一需求批次内跨角色传递临时端口、测试账号、动态凭证等运行态信息'),
    tools: ['session_share', 'session_read'],
  },
  {
    key: 'project_memory',
    label: t('项目架构记忆'),
    icon: 'account_tree',
    description: t('维护 ARCHITECTURE.md 全局架构黑板报：PM 首次梳理、开发重大接口变更后追加'),
    tools: ['update_architecture'],
  },
  {
    key: 'subagent',
    label: t('子代理并发协同'),
    icon: 'group',
    description: t('派生只读子代理并行探索多个模块/目录，单轮拿到汇总结论'),
    tools: ['delegate_explore'],
  },
  {
    key: 'external_net',
    label: t('网络与外部知识'),
    icon: 'language',
    description: t('查阅专业技能库文档、抓取公网页面正文与镜像源延迟测速'),
    tools: ['read_skill', 'web_fetch', 'speedtest_mirrors'],
  },
]

const defaultCommandCategories = [
  {
    key: 'python',
    label: t('Python 生态'),
    icon: 'code',
    description: t('Python 解释器、包管理与测试/Lint 工具'),
    commands: ['python*', 'py', 'pip*', 'pytest', 'uv', 'uvx', 'poetry', 'tox', 'ruff', 'mypy', 'black', 'flake8'],
  },
  {
    key: 'node',
    label: t('Node / 前端'),
    icon: 'javascript',
    description: t('Node 运行时、包管理器与前端构建/测试工具'),
    commands: ['node', 'npm', 'npx', 'pnpm', 'yarn', 'bun', 'tsc', 'eslint', 'prettier', 'vitest', 'jest'],
  },
  {
    key: 'compiled',
    label: 'Rust / Go / .NET / Java',
    icon: 'build',
    description: t('系统级与强类型后端编译型语言工具链'),
    commands: ['cargo', 'go', 'dotnet', 'gradle*', 'mvn', 'java', 'javac'],
  },
  {
    key: 'cpp',
    label: t('C / C++ 构建'),
    icon: 'construction',
    description: t('C/C++ 编译器、构建工具与测试套件'),
    commands: ['make', 'cmake', 'ctest', 'ninja', 'gcc', 'g++', 'clang', 'clang++', 'msbuild'],
  },
  {
    key: 'vcs',
    label: t('版本控制'),
    icon: 'account_tree',
    description: t('Git 代码仓库与分支版本管理'),
    commands: ['git'],
  },
  {
    key: 'package_managers',
    label: t('包管理器'),
    icon: 'inventory_2',
    description: t('系统级包管理器（缺工具链时按需环境引导，受审批约束）'),
    commands: ['winget*', 'choco', 'scoop', 'brew', 'apt*', 'apt-get', 'dnf', 'nvm'],
  },
  {
    key: 'system_shell',
    label: t('系统 Shell / 实用工具'),
    icon: 'terminal',
    description: t('系统 Shell、网络下载与归档解压'),
    commands: ['powershell', 'pwsh', 'curl', 'tar', 'setx'],
  },
  {
    key: 'windows_probe',
    label: t('Windows 探测与排障'),
    icon: 'desktop_windows',
    description: t('Windows 环境排障、进程查看与文件探测（只读/信息性）'),
    commands: ['where', 'cmd', 'dir', 'type', 'echo', 'set', 'tasklist', 'systeminfo', 'findstr', 'more', 'ver', 'hostname', 'whoami'],
  },
  {
    key: 'posix_probe',
    label: t('POSIX 只读探测与统计'),
    icon: 'data_object',
    description: t('跨平台/Unix 常用只读与文本统计命令（只读无害）'),
    commands: ['ls', 'cat', 'wc', 'head', 'tail', 'grep', 'sort', 'uniq', 'cut', 'tr', 'diff', 'stat', 'du', 'df', 'pwd', 'printf', 'date', 'sleep', 'uname', 'which', 'id', 'basename', 'dirname', 'touch', 'md5sum', 'sha256sum', 'printenv'],
  },
]

const toolCategories = computed(() => {
  return store.state?.tool_categories?.filter(c => c.key !== 'local_debug' && c.key !== 'browser_auto') ?? defaultToolCategories
})

const commandCategories = computed(() => {
  return store.state?.command_categories ?? defaultCommandCategories
})

const mutatingToolsSet = new Set([
  'write_file', 'edit_file', 'multi_edit', 'delete_file', 'move_file', 'run_command',
  'create_tasks', 'dev_server_start', 'browser_click', 'browser_type', 'browser_eval',
])
const riskyToolsSet = new Set(['delete_file', 'move_file', 'run_command', 'dev_server_start'])

function getToolRiskTag(name: string): { label: string; type: 'success' | 'warning' | 'error' | 'info' } {
  if (name === 'finish') return { label: '核心协议·恒可用', type: 'info' }
  if (riskyToolsSet.has(name)) return { label: '高危·需审批', type: 'error' }
  if (mutatingToolsSet.has(name)) return { label: '变更·需审批', type: 'warning' }
  return { label: '只读安全', type: 'success' }
}

function getToolDesc(name: string): string {
  const found = toolsCatalog.value.find(t => t.name === name)
  return found ? found.description : ''
}

function toggleToolsGroup(tools: string[], enable: boolean): void {
  for (const name of tools) {
    if (name === 'finish') continue
    toolsDraft[name] = enable
  }
}

function toggleCommandsGroup(cmds: string[], enable: boolean): void {
  for (const name of cmds) {
    commandsDraft[name] = enable
  }
}

function toggleAllCommands(enable: boolean): void {
  for (const c of commandsCatalog.value) {
    commandsDraft[c] = enable
  }
}

function getGroupToolsCount(tools: string[]): { enabled: number; total: number } {
  let enabled = 0
  for (const t of tools) {
    if (t === 'finish' || toolsDraft[t]) enabled++
  }
  return { enabled, total: tools.length }
}

function getGroupCommandsCount(cmds: string[]): { enabled: number; total: number } {
  let enabled = 0
  for (const c of cmds) {
    if (commandsDraft[c] !== false) enabled++
  }
  return { enabled, total: cmds.length }
}

function getFilteredTools(toolNames: string[]) {
  const q = toolSearch.value.trim().toLowerCase()
  return toolsCatalog.value.filter(t => {
    if (!toolNames.includes(t.name)) return false
    if (!q) return true
    return t.name.toLowerCase().includes(q) || t.description.toLowerCase().includes(q)
  })
}

function getFilteredCommands(cmdNames: string[]) {
  const q = cmdSearch.value.trim().toLowerCase()
  if (!q) return cmdNames
  return cmdNames.filter(c => c.toLowerCase().includes(q))
}

const filteredToolCategories = computed(() => {
  const q = toolSearch.value.trim().toLowerCase()
  if (!q) return toolCategories.value
  return toolCategories.value.filter(cat => {
    if (cat.label.toLowerCase().includes(q) || cat.description.toLowerCase().includes(q)) return true
    return cat.tools.some(t => {
      const toolObj = toolsCatalog.value.find(item => item.name === t)
      return t.toLowerCase().includes(q) || (toolObj && toolObj.description.toLowerCase().includes(q))
    })
  })
})

const filteredCommandCategories = computed(() => {
  const q = cmdSearch.value.trim().toLowerCase()
  if (!q) return commandCategories.value
  return commandCategories.value.filter(cat => {
    if (cat.label.toLowerCase().includes(q) || cat.description.toLowerCase().includes(q)) return true
    return cat.commands.some(c => c.toLowerCase().includes(q))
  })
})

const toolsTotalCount = computed(() => toolsCatalog.value.filter(t => !t.group && t.name !== 'http_request').length)
const toolsEnabledCount = computed(() => toolsCatalog.value.filter(t => !t.group && t.name !== 'http_request' && (t.name === 'finish' || toolsDraft[t.name])).length)
const cmdsTotalCount = computed(() => commandsCatalog.value.length)
const cmdsEnabledCount = computed(() => commandsCatalog.value.filter(c => commandsDraft[c] !== false).length)
const debugActiveCount = computed(() => {
  let count = 0
  if (browserGroupEnabled.value) count += browserTools.filter(k => toolsDraft[k]).length
  if (devServerGroupEnabled.value) count += devServerTools.filter(k => toolsDraft[k]).length
  if (toolsDraft['http_request']) count += 1
  return count
})
// ---- 命令白名单开关草稿（全局默认；只上报禁用项，未列出 = 放行） ----
const commandsCatalog = ref<string[]>([])
const commandsDraft = reactive<Record<string, boolean>>({})
// ---- Agent 子进程环境变量草稿（多行 KEY=VALUE，代理/国内镜像） ----
const envDraft = shallowRef('')
// PATH 自动修复开关：任务启动时把扫描到的缺失工具目录持久写入用户 PATH
const pathAutofix = shallowRef(false)
const toolsSaving = shallowRef(false)
// ---- 本地调试工具与浏览器配置草稿 ----
const localDebugDraft = reactive({
  browser: 'auto' as 'auto' | 'chrome' | 'edge',
  browser_executable: '',
  headless: true,
  hosts_text: 'localhost\n127.0.0.1\n::1',
  navigation_timeout_seconds: 30,
  service_ready_timeout_seconds: 60,
  max_services: 3,
  artifacts_retention_days: 7,
})
// ---- 子代理并发预算草稿：厂商并发上限不公开，只能按 429 反馈自适应 ----
const subagentDraft = reactive({ max_parallel: 4, adaptive: true })
// 运行时实际生效值：自适应撞过 429 时可能低于配置值；多池取最小（瓶颈池）
const subagentLiveLimit = computed<number | null>(() => {
  const pools = store.state?.subagent_cfg?.pools ?? []
  if (!pools.length) return null
  return Math.min(...pools.map(p => p.limit))
})
const testingBrowser = shallowRef(false)
async function onTestBrowser(): Promise<void> {
  testingBrowser.value = true
  try {
    const res = await api.testBrowser({
      browser: localDebugDraft.browser,
      browser_executable: localDebugDraft.browser_executable.trim(),
    })
    if (res.ok) {
      message()?.(res.message, 'success')
    } else {
      message()?.(res.message || res.error || t('测试失败'), 'error')
    }
  } catch (e: any) {
    message()?.(e.message, 'error')
  } finally {
    testingBrowser.value = false
  }
}
// ---- 访问权限草稿（全局默认；项目可在对话框里逐个覆盖） ----
const permOptions = [
  { label: t('变更前询问（发生变更时询问：写/改/删/移文件、执行命令都挂起等批准）'), value: 'ask_on_change' },
  { label: t('自主模式（有风险时询问：仅 delete_file / move_file / run_command）'), value: 'autonomous' },
  { label: t('完全访问（减少确认次数：一律不询问，与历史行为一致）'), value: 'full_access' },
]
const permDraft = reactive({ mode: 'full_access' as string, timeout_minutes: 30 })
// ---- Git 提交与合并开关（默认关闭：改动直接保留在工作区） ----
const gitCommitAndMergeDraft = shallowRef(false)
// ---- 任务结束提醒草稿（完成/受阻时提示音 + 系统通知；cancelled 不提醒） ----
const notifyDraft = reactive({ sound: true, system: true })
const notifySaving = shallowRef(false)
// ---- 数据脱敏草稿：开关两枚 + 自定义正则（每行一条） ----
const maskingDraft = reactive({ enabled: true, enhanced_rules: false, patternsText: '' })
const maskingSaving = shallowRef(false)
// ---- 镜像源管理与测速选源（草稿态：自定义源可增删、内置/自定义皆可禁用；
//      测速结果是服务端数据，随轮询即时刷新——Agent 自测后看板也能看到） ----
const mirrorTesting = shallowRef(false)
const mirrorTestingCat = shallowRef<string | null>(null)
const mirrorApplying = shallowRef(false)
const mirrorsSaving = shallowRef(false)
const mirrorCustom = ref<Record<string, string[]>>({})   // 草稿：类别 → 用户添加 URL
const mirrorDisabled = ref<string[]>([])                 // 草稿：禁用的源 URL
const mirrorSpeeds = ref<Record<string, number | null>>({})
const mirrorTestedAt = shallowRef('')
// 后端存 ISO 形态（2026-11-04T03:11:27），展示转成标准时间（T 换空格）
const mirrorTestedAtText = computed(() => mirrorTestedAt.value.replace('T', ' '))
const addCat = shallowRef('goproxy')
const addUrl = shallowRef('')
let inited = false
watch(() => store.state, (s) => {
  if (!s || inited) return
  inited = true
  updDraft.repo = s.update_cfg?.repo ?? ''
  updDraft.token = ''  // 不回填明文：只显示掩码占位，留空即保持原值
  updTokenMasked.value = s.update_cfg?.token_masked ?? ''
  updTokenConfigured.value = s.update_cfg?.token_configured ?? false
  updTokenTouched.value = false
  updDraft.auto_check = s.update_cfg?.auto_check ?? true
  toolsCatalog.value = s.tools_catalog ?? []
  const defaultOff = new Set(s.default_off_tools ?? [
    ...browserTools, ...devServerTools, 'http_request',
  ])
  // finish 只是展示行（常开且禁用），不进草稿、不参与保存
  for (const t of toolsCatalog.value) {
    if (t.name === 'finish') continue
    const defVal = !defaultOff.has(t.name)
    toolsDraft[t.name] = s.tools_cfg?.[t.name] ?? defVal
  }
  commandsCatalog.value = s.commands_catalog ?? []
  for (const c of commandsCatalog.value) commandsDraft[c] = s.commands_cfg?.[c] ?? true
  envDraft.value = Object.entries(s.env_cfg ?? {})
    .map(([k, v]) => `${k}=${v}`).join('\n')
  pathAutofix.value = s.env_path_autofix ?? false
  permDraft.mode = s.permissions_cfg?.mode ?? 'full_access'
  permDraft.timeout_minutes = s.permissions_cfg?.timeout_minutes ?? 30
  const dbg = s.local_debug_cfg
  if (dbg) {
    localDebugDraft.browser = dbg.browser ?? 'auto'
    localDebugDraft.browser_executable = dbg.browser_executable ?? ''
    localDebugDraft.headless = dbg.headless ?? true
    localDebugDraft.hosts_text = (dbg.allowed_hosts ?? ['localhost', '127.0.0.1', '::1']).join('\n')
    localDebugDraft.navigation_timeout_seconds = dbg.navigation_timeout_seconds ?? 30
    localDebugDraft.service_ready_timeout_seconds = dbg.service_ready_timeout_seconds ?? 60
    localDebugDraft.max_services = dbg.max_services ?? 3
    localDebugDraft.artifacts_retention_days = dbg.artifacts_retention_days ?? 7
  }
  gitCommitAndMergeDraft.value = s.git_cfg?.commit_and_merge ?? false
  subagentDraft.max_parallel = s.subagent_cfg?.max_parallel ?? 4
  subagentDraft.adaptive = s.subagent_cfg?.adaptive ?? true
  notifyDraft.sound = s.notify_cfg?.sound ?? true
  notifyDraft.system = s.notify_cfg?.system ?? true
  maskingDraft.enabled = s.masking_cfg?.enabled ?? true
  maskingDraft.enhanced_rules = s.masking_cfg?.enhanced_rules ?? false
  maskingDraft.patternsText = (s.masking_cfg?.custom_patterns ?? []).join('\n')
  syncMirrorDraft()
  mirrorSpeeds.value = s.mirror_speeds?.results ?? {}
  mirrorTestedAt.value = s.mirror_speeds?.tested_at ?? ''
  draft.provider = s.llm.provider
  // 清洗历史 bug 写入的模型名：下拉标注「（977K）」曾被当成 id 存进配置
  draft.model = s.llm.model.replace(/（\d+K）$/, '')
  draft.base_url = s.llm.base_url
  draft.api_key = ''  // 同上：后端只回掩码，明文密钥不再下发
  apiKeyMasked.value = s.llm.api_key_masked ?? ''
  apiKeyConfigured.value = s.llm.key_configured ?? false
  apiKeyTouched.value = false
  draft.temperature = s.llm.temperature
  draft.max_tokens = s.llm.max_tokens
  draft.max_context_tokens = s.llm.max_context_tokens
  draft.enable_thinking = s.llm.enable_thinking
  draft.timeout_seconds = s.llm.timeout_seconds
  // 审查 Agent 配置回填（api_key 只回掩码；模型未配置时字段留空）
  reviewDraft.enabled = s.review_cfg?.enabled ?? true
  reviewDraft.provider = (s.review_cfg?.provider as 'openai' | 'anthropic') ?? 'openai'
  reviewDraft.model = s.review_cfg?.model ?? ''
  reviewDraft.base_url = s.review_cfg?.base_url ?? ''
  reviewDraft.api_key = ''
  reviewKeyMasked.value = s.review_cfg?.api_key_masked ?? ''
  reviewKeyConfigured.value = s.review_cfg?.model_configured ?? false
  reviewKeyTouched.value = false
  reviewDraft.max_turns = s.review_cfg?.max_turns ?? 10
  reviewDraft.max_review_rounds = s.review_cfg?.max_review_rounds ?? 2
  reviewDraft.allow_test_commands = s.review_cfg?.allow_test_commands ?? true
  reviewDraft.squash_merge = s.review_cfg?.squash_merge ?? true
  draft.interval = s.scheduler_cfg.interval_seconds
  draft.parallel = s.scheduler_cfg.max_parallel_agents
  draft.port = s.web_cfg.port
  draft.retention = s.logging?.retention_days ?? 7
  draft.max_total_mb = s.logging?.max_total_mb ?? 200
  draft.runlog_enabled = s.runlog?.enabled ?? true
  draft.runlog_retention = s.runlog?.retention_days ?? 30
  draft.runlog_max_mb = s.runlog?.max_total_mb ?? 500
  draft.runlog_dir = s.runlog?.dir ?? ''
  draft.budget_enabled = s.budget_cfg?.enabled ?? false
  draft.budget_task_tokens = s.budget_cfg?.max_tokens_per_task ?? 0
  draft.budget_day_tokens = s.budget_cfg?.max_tokens_per_project_per_day ?? 0
  draft.budget_task_cost = s.budget_cfg?.max_cost_per_task_cny ?? 0
  draft.res_enabled = s.resilience_cfg?.enabled ?? true
  draft.res_consec = s.resilience_cfg?.max_consecutive_failures ?? 3
  draft.res_window = s.resilience_cfg?.window_minutes ?? 120
  draft.res_rate_pct = Math.round((s.resilience_cfg?.max_failure_rate ?? 0.6) * 100)
  draft.res_samples = s.resilience_cfg?.min_samples ?? 5
  draft.res_auto = s.resilience_cfg?.auto_resume ?? false
  draft.egress_mode = (s.egress_cfg?.mode as 'allowlist' | 'off') ?? 'allowlist'
  draft.egress_extra = [...(s.egress_cfg?.extra_hosts ?? [])]
  draft.egress_audit = s.egress_cfg?.audit ?? true
  syncProfilesDraft(s.agent_profiles)
})

// ---- 全局 Agent 角色预设池草稿 ----
const profilesDraft = ref<Record<string, any>>({})
const profilesTouchedKeys = reactive<Record<string, boolean>>({})
const profilesSaving = shallowRef(false)
const editingProfileId = shallowRef<string | null>(null)
const profileTestingId = shallowRef<string | null>(null)

const roleSelectOptions = [
  { label: t('项目经理 (PM)'), value: 'pm' },
  { label: t('后端开发工程师'), value: 'backend' },
  { label: t('前端开发工程师'), value: 'frontend' },
  { label: t('测试工程师 (QA)'), value: 'qa' },
  { label: t('自定义专职角色'), value: 'custom' },
]

function getRoleIcon(role: string): string {
  const map: Record<string, string> = {
    pm: 'manage_accounts', backend: 'terminal', frontend: 'palette',
    qa: 'science', custom: 'extension',
  }
  return map[role] || 'smart_toy'
}

function getRoleTemplate(role: string): string {
  if (store.state?.role_prompt_templates?.[role]) {
    return store.state.role_prompt_templates[role]
  }
  const builtIn = store.state?.built_in_roles?.[role]
  if (builtIn?.default_prompt_template) {
    return builtIn.default_prompt_template
  }
  return ''
}

function loadRolePromptTemplate(prof: any): void {
  const tpl = getRoleTemplate(prof.role)
  if (!tpl) {
    message()?.(t('未找到该角色的内置模版'), 'warning')
    return
  }
  prof.extra_prompt = tpl
  message()?.(tFmt('已为「{p0}」载入内置职责规范模版', { p0: prof.name || prof.role }), 'success')
}

function syncProfilesDraft(profs: Record<string, any> | undefined): void {
  const defaults: Record<string, any> = {
    pm_default: {
      id: 'pm_default', name: t('项目经理 PM'), role: 'pm',
      description: t('负责需求分析、子任务拆分与前后端依赖编排'),
      max_turns: 20, produces_code: false, allow_test_scripts: false,
      extra_prompt: getRoleTemplate('pm'),
      llm: { provider: '', model: '', base_url: '', api_key: '', api_key_masked: '', api_key_configured: false },
    },
    backend_default: {
      id: 'backend_default', name: t('后端开发'), role: 'backend',
      description: t('负责后端服务架构、接口路由与业务逻辑实现'),
      max_turns: 40, produces_code: true, allow_test_scripts: false,
      extra_prompt: getRoleTemplate('backend'),
      llm: { provider: '', model: '', base_url: '', api_key: '', api_key_masked: '', api_key_configured: false },
    },
    frontend_default: {
      id: 'frontend_default', name: t('前端开发'), role: 'frontend',
      description: t('负责前端页面组件、状态管理与接口联调'),
      max_turns: 40, produces_code: true, allow_test_scripts: false,
      extra_prompt: getRoleTemplate('frontend'),
      llm: { provider: '', model: '', base_url: '', api_key: '', api_key_masked: '', api_key_configured: false },
    },
    qa_default: {
      id: 'qa_default', name: t('测试工程师'), role: 'qa',
      description: t('负责前后端联调测试、新建 Bug 任务与回归验收'),
      max_turns: 35, produces_code: false, allow_test_scripts: false,
      extra_prompt: getRoleTemplate('qa'),
      llm: { provider: '', model: '', base_url: '', api_key: '', api_key_masked: '', api_key_configured: false },
    },
  }
  const source = (profs && Object.keys(profs).length > 0) ? profs : defaults
  const nextDraft: Record<string, any> = {}
  for (const [k, v] of Object.entries(source)) {
    const vLlm = v.llm || {}
    nextDraft[k] = {
      id: k,
      name: v.name || k,
      role: v.role || (k in defaults ? defaults[k].role : 'custom'),
      description: v.description || '',
      max_turns: v.max_turns ?? null,
      produces_code: v.produces_code ?? null,
      allow_test_scripts: !!v.allow_test_scripts,
      extra_prompt: v.extra_prompt || '',
      llm: {
        provider: vLlm.provider || '',
        model: vLlm.model || '',
        base_url: vLlm.base_url || '',
        api_key: '',
        api_key_masked: vLlm.api_key_masked || '',
        api_key_configured: !!vLlm.api_key_configured,
      },
    }
  }
  profilesDraft.value = nextDraft
  if (!editingProfileId.value && Object.keys(nextDraft).length > 0) {
    editingProfileId.value = Object.keys(nextDraft)[0]
  }
}

function addProfile(): void {
  const base = 'custom_role'
  let id = base
  let n = 2
  while (profilesDraft.value[id]) {
    id = `${base}_${n}`
    n++
  }
  profilesDraft.value[id] = {
    id,
    name: t('新建专职 Agent'),
    role: 'custom',
    description: t('专职自定义能力 Agent'),
    max_turns: 40,
    produces_code: true,
    allow_test_scripts: false,
    extra_prompt: getRoleTemplate('custom'),
    llm: { provider: '', model: '', base_url: '', api_key: '', api_key_masked: '', api_key_configured: false },
  }
  editingProfileId.value = id
}

function removeProfile(id: string): void {
  delete profilesDraft.value[id]
  delete profilesTouchedKeys[id]
  if (editingProfileId.value === id) {
    editingProfileId.value = Object.keys(profilesDraft.value)[0] || null
  }
}

const profileFetchingId = shallowRef<string | null>(null)

async function fetchProfileModelList(pid: string): Promise<void> {
  const prof = profilesDraft.value[pid]
  if (!prof) return
  const eff = {
    provider: prof.llm.provider || draft.provider || store.state?.llm.provider || 'openai',
    base_url: (prof.llm.base_url || '').trim() || draft.base_url.trim() || store.state?.llm.base_url || '',
    api_key: (prof.llm.api_key || '').trim() || draft.api_key.trim(),
    model: (prof.llm.model || '').trim() || draft.model.trim() || store.state?.llm.model || '',
  }
  profileFetchingId.value = pid
  try {
    const r = await api.models(eff)
    for (const m of r.models) {
      if (m.id && !modelSpecs.value.some(existing => existing.id === m.id)) {
        modelSpecs.value.push(m)
      }
    }
    message()?.(tFmt('[{p0}] 已获取 {p1} 个模型，可在下拉框中选择', { p0: prof.name, p1: r.models.length }), 'success')
  } catch (e: any) {
    message()?.(tFmt('[{p0}] 获取模型列表失败：{p1}', { p0: prof.name, p1: e.message }), 'error')
  } finally {
    profileFetchingId.value = null
  }
}

async function testProfileModel(pid: string): Promise<void> {
  const prof = profilesDraft.value[pid]
  if (!prof) return
  const eff = {
    provider: prof.llm.provider || store.state?.llm.provider || 'openai',
    base_url: prof.llm.base_url || store.state?.llm.base_url || '',
    api_key: prof.llm.api_key || '',
    model: prof.llm.model || store.state?.llm.model || '',
  }
  if (!eff.model) {
    message()?.(t('该预设未指定模型名（将继承全局模型）'), 'warning')
  }
  profileTestingId.value = pid
  try {
    const res = await api.testModel(eff)
    message()?.(`[${prof.name}] ${res.message || t('连接成功')}`, 'success')
  } catch (e: any) {
    message()?.(tFmt('[{p0}] 连接测试失败：{p1}', { p0: prof.name, p1: e.message }), 'error')
  } finally {
    profileTestingId.value = null
  }
}

async function saveProfiles(): Promise<void> {
  profilesSaving.value = true
  try {
    const payload: Record<string, any> = {}
    for (const [pid, p] of Object.entries(profilesDraft.value)) {
      const pData: any = {
        name: p.name.trim() || pid,
        role: p.role,
        description: p.description.trim(),
        max_turns: p.max_turns ? Number(p.max_turns) : null,
        produces_code: p.produces_code,
        allow_test_scripts: !!p.allow_test_scripts,
        extra_prompt: p.extra_prompt.trim(),
      }
      if (p.llm && (p.llm.model.trim() || p.llm.provider || p.llm.base_url.trim() || profilesTouchedKeys[pid])) {
        const mObj: any = {}
        if (p.llm.provider) mObj.provider = p.llm.provider
        if (p.llm.model.trim()) mObj.model = p.llm.model.trim()
        if (p.llm.base_url.trim()) mObj.base_url = p.llm.base_url.trim()
        if (profilesTouchedKeys[pid]) mObj.api_key = p.llm.api_key || ''
        pData.llm = mObj
      }
      payload[pid] = pData
    }
    await api.config({ agent_profiles: payload })
    message()?.(t('Agent 角色预设池已保存并生效'))
    for (const k of Object.keys(profilesTouchedKeys)) delete profilesTouchedKeys[k]
  } catch (e: any) {
    message()?.(e.message, 'error')
  } finally {
    profilesSaving.value = false
  }
}

async function saveLlm(): Promise<void> {
  try {
    const llm: Record<string, unknown> = {
      provider: draft.provider, model: draft.model, base_url: draft.base_url,
      temperature: draft.temperature,
      max_tokens: draft.max_tokens, max_context_tokens: draft.max_context_tokens,
      enable_thinking: draft.enable_thinking,
      timeout_seconds: draft.timeout_seconds,
    }
    // 三态：未触碰不带键（后端保留已存密钥）；触碰过按输入上报，空串 = 清除
    if (apiKeyTouched.value) llm.api_key = draft.api_key.trim()
    await api.config({ llm })
    message()?.(t('已保存并生效'))
    draft.api_key = ''
    apiKeyTouched.value = false
  } catch (e: any) { message()?.(e.message, 'error') }
  void import('../store').then(m => m.refresh(true))
}

async function saveNotify(): Promise<void> {
  notifySaving.value = true
  try {
    await api.config({ notify: { sound: notifyDraft.sound, system: notifyDraft.system } })
    message()?.(t('已保存并生效（下个任务结束即按新开关提醒）'))
  } catch (e: any) { message()?.(e.message, 'error') }
  finally { notifySaving.value = false }
  void import('../store').then(m => m.refresh(true))
}

// ---- 数据脱敏保存：正则按行拆分、去空行；后端校验（≤20 条/可编译）失败 400 透传 ----
async function saveMasking(): Promise<void> {
  maskingSaving.value = true
  try {
    const custom_patterns = maskingDraft.patternsText
      .split('\n').map(l => l.trim()).filter(Boolean)
    await api.config({ masking: {
      enabled: maskingDraft.enabled,
      enhanced_rules: maskingDraft.enhanced_rules,
      custom_patterns,
    } })
    message()?.(t('已保存并生效（进行中任务的已脱敏内容保持原映射）'))
  } catch (e: any) { message()?.(e.message, 'error') }
  finally { maskingSaving.value = false }
  void import('../store').then(m => m.refresh(true))
}

// ---- 审查 Agent 保存：模型三态 + 未配置模型时清除绑定（后端 null=审查未启用） ----
async function saveReview(): Promise<void> {
  reviewSaving.value = true
  try {
    const hasModel = reviewDraft.model.trim().length > 0
    if (hasModel && effTaskModel().value && reviewDraft.model.trim().toLowerCase() === effTaskModel().value.trim().toLowerCase()) {
      message()?.(t('审查模型不能与任务模型相同（') + effTaskModel().value + t('）——请换一个独立模型'), 'error')
      return
    }
    const llm = hasModel ? {
      provider: reviewDraft.provider,
      base_url: reviewDraft.base_url.trim(),
      model: reviewDraft.model.trim(),
      ...(reviewKeyTouched.value ? { api_key: reviewDraft.api_key.trim() } : {}),
    } : null
    await api.config({
      review: {
        enabled: reviewDraft.enabled,
        llm,
        max_turns: reviewDraft.max_turns,
        max_review_rounds: reviewDraft.max_review_rounds,
        allow_test_commands: reviewDraft.allow_test_commands,
        squash_merge: reviewDraft.squash_merge,
      },
    })
    if (!hasModel) message()?.(t('已保存：审查未启用（未配置审查模型）'))
    else message()?.(t('已保存：审查门已对全部项目默认生效（项目级可单独关闭）'))
    reviewDraft.api_key = ''
    reviewKeyTouched.value = false
  } catch (e: any) { message()?.(e.message, 'error') }
  finally { reviewSaving.value = false }
  void import('../store').then(m => m.refresh(true))
}

async function fetchReviewModelList(): Promise<void> {
  reviewFetching.value = true
  try {
    const r = await api.models({
      provider: reviewDraft.provider,
      base_url: reviewDraft.base_url.trim(),
      api_key: reviewDraft.api_key.trim(),
      model: reviewDraft.model.trim(),
    })
    reviewModelOptions.value = r.models
    message()?.(tFmt('已获取 {p0} 个模型', { p0: r.models.length }))
  } catch (e: any) { message()?.(e.message, 'error') }
  finally { reviewFetching.value = false }
}

/** 当前生效任务模型（全局 llm 配置）：审查模型互斥校验基准 */
function effTaskModel(): { value: string } {
  return { value: store.state?.llm.model || '' }
}

async function saveSched(): Promise<void> {
  try {
    const web: Record<string, unknown> = { port: draft.port }
    // 登录密码三态：默认不带键（保持原值）；点过「清除」带空串（关闭鉴权）；
    // 输入过新密码则带上（后端保存后注销全部旧会话）
    if (authPwdClear.value) web.auth_password = ''
    else if (authPwdTouched.value) web.auth_password = authPwd.value
    await api.config({
      scheduler: { interval_seconds: draft.interval, max_parallel_agents: draft.parallel },
      web,
      logging: { retention_days: draft.retention, max_total_mb: draft.max_total_mb },
    })
    if (authPwdClear.value) message()?.(t('已保存：看板登录鉴权已关闭'), 'success')
    else if (authPwdTouched.value) message()?.(t('已保存：登录密码已更新，旧会话已失效，请重新登录'), 'success')
    else message()?.(t('已保存（端口下次启动生效；日志归档清理立即按新天数执行）'))
    authPwd.value = ''
    authPwdTouched.value = false
    authPwdClear.value = false
  } catch (e: any) { message()?.(e.message, 'error') }
  void import('../store').then(m => m.refresh(true))
}

// ---- 运行台账（V1-R1）：保存即重建台账单例（开关/保留期热生效，无需重启） ----
const runlogSaving = shallowRef(false)
async function saveRunlog(): Promise<void> {
  if (runlogBusy.value) {
    message()?.(t('运行台账正在迁移，请等待完成后再修改'), 'warning')
    return
  }
  runlogSaving.value = true
  try {
    await api.config({ runlog: {
      enabled: draft.runlog_enabled,
      retention_days: draft.runlog_retention,
      max_total_mb: draft.runlog_max_mb,
      dir: draft.runlog_dir.trim(),
    } })
    message()?.(t('运行台账设置已保存并立即生效'))
  } catch (e: any) { message()?.(e.message, 'error') }
  finally { runlogSaving.value = false }
  void import('../store').then(m => m.refresh(true))
}

function pickRunlogDir(): void {
  openDirBrowser(draft.runlog_dir || '', (p) => { draft.runlog_dir = p }, 'dir')
}

// ---- 预算硬闸：保存后立即生效 ----
const budgetSaving = shallowRef(false)
async function saveBudget(): Promise<void> {
  budgetSaving.value = true
  try {
    await api.config({ budget: {
      enabled: draft.budget_enabled,
      max_tokens_per_task: draft.budget_task_tokens,
      max_tokens_per_project_per_day: draft.budget_day_tokens,
      max_cost_per_task_cny: draft.budget_task_cost,
    } })
    message()?.(t('预算设置已保存并立即生效'))
  } catch (e: any) { message()?.(e.message, 'error') }
  finally { budgetSaving.value = false }
  void import('../store').then(m => m.refresh(true))
}

// ---- 熔断停机（V1-R4）：保存后立即生效；触发后需人工恢复 ----
const resilienceSaving = shallowRef(false)
async function saveResilience(): Promise<void> {
  resilienceSaving.value = true
  try {
    await api.config({ resilience: {
      enabled: draft.res_enabled,
      max_consecutive_failures: draft.res_consec,
      window_minutes: draft.res_window,
      max_failure_rate: (draft.res_rate_pct || 0) / 100,
      min_samples: draft.res_samples,
      auto_resume: draft.res_auto,
    } })
    message()?.(t('熔断设置已保存并立即生效'))
  } catch (e: any) { message()?.(e.message, 'error') }
  finally { resilienceSaving.value = false }
  void import('../store').then(m => m.refresh(true))
}

// ---- 出网白名单（V1-R6）：保存后立即生效；off 仅用于排障 ----
const egressSaving = shallowRef(false)
async function saveEgress(): Promise<void> {
  egressSaving.value = true
  try {
    await api.config({ egress: {
      mode: draft.egress_mode,
      extra_hosts: draft.egress_extra,
      audit: draft.egress_audit,
    } })
    message()?.(t('出网白名单已保存并立即生效'))
  } catch (e: any) { message()?.(e.message, 'error') }
  finally { egressSaving.value = false }
  void import('../store').then(m => m.refresh(true))
}
const egressStatus = computed(() => store.state?.egress_status)

const pausedProjects = computed(() =>
  (store.state?.projects || []).filter(p => p.health?.paused))

async function resumeProject(name: string): Promise<void> {
  try {
    await api.resumeProject(name)
    message()?.(tFmt('项目 {p0} 已恢复', { p0: name }), 'success')
  } catch (e: any) { message()?.(e.message, 'error') }
  void import('../store').then(m => m.refresh(true))
}

// ---- 写操作审计（V1-R5）：只读最近记录 ----
const auditEntries = ref<AuditEntryT[]>([])
const auditLoading = shallowRef(false)
const auditAction = shallowRef('')
const auditActor = shallowRef('')

async function loadAudit(): Promise<void> {
  auditLoading.value = true
  try {
    const r = await api.audit({
      limit: 300, action: auditAction.value.trim(), actor: auditActor.value.trim(),
    })
    auditEntries.value = r.entries || []
  } catch (e: any) { message()?.(e.message, 'error') }
  finally { auditLoading.value = false }
}

watch(() => settingsSection.value, (s) => {
  if (s === 'audit') void loadAudit()
}, { immediate: true })

// ---- 目录迁移：迁移中锁定台账设置与回放，每秒轮询进度 ----
const mig = computed(() => store.state?.runlog_migration)
const runlogBusy = computed(() => mig.value?.state === 'running')
const migPercent = computed(() => Math.min(100, Math.max(0, mig.value?.percent || 0)))
let migTimer: ReturnType<typeof setInterval> | null = null

function fmtMB(n?: number): string {
  return `${((n || 0) / 1048576).toFixed(1)} MB`
}

async function pollMigration(): Promise<void> {
  try {
    const r = await api.runlogMigration()
    if (store.state) store.state.runlog_migration = r.migration
  } catch { /* 轮询失败静默，下一轮再试 */ }
}

watch(() => mig.value?.state, (s, prev) => {
  if (s === 'running') {
    if (!migTimer) migTimer = setInterval(pollMigration, 1000)
  } else if (migTimer) {
    clearInterval(migTimer); migTimer = null
  }
  if (s === 'done' && prev === 'running') {
    message()?.(t('运行台账目录迁移完成'), 'success')
    draft.runlog_dir = store.state?.runlog?.dir ?? ''
    void import('../store').then(m => m.refresh(true))
  }
  if (s === 'failed' && prev === 'running') {
    message()?.(t('运行台账目录迁移失败，请查看错误后重试或改回原目录'), 'error')
  }
}, { immediate: true })

async function retryRunlogMigration(): Promise<void> {
  try {
    const r = await api.retryRunlogMigration()
    if (store.state) store.state.runlog_migration = r.migration
    message()?.(t('已重新开始迁移'))
  } catch (e: any) { message()?.(e.message, 'error') }
}

onUnmounted(() => { if (migTimer) { clearInterval(migTimer); migTimer = null } })

// ---- 看板登录密码（web.auth_password）：留空保持不变 / 输入即改 / 清除即关鉴权 ----
const authPwd = shallowRef('')
const authPwdTouched = shallowRef(false)
const authPwdClear = shallowRef(false)
function onPwdInput(v: string): void {
  authPwd.value = v
  authPwdTouched.value = v.length > 0
  if (v) authPwdClear.value = false  // 重新输入视为放弃「清除」
}
function clearAuthPwd(): void {
  authPwd.value = ''
  authPwdTouched.value = false
  authPwdClear.value = true
}

async function toggleAutostart(enabled: boolean): Promise<void> {
  try {
    await api.autostart(enabled)
    message()?.(t('开机自启动已') + (enabled ? t('开启') : t('关闭')))
  } catch (e: any) { message()?.(e.message, 'error') }
  void import('../store').then(m => m.refresh(true))
}

async function saveTools(): Promise<void> {
  toolsSaving.value = true
  try {
    const defaultOff = new Set(store.state?.default_off_tools ?? [
      ...browserTools, ...devServerTools, 'http_request',
    ])
    // 常规工具未列出=启用（仅上报 false）；调试类工具未列出=关闭（仅上报 true）
    const toolsPayload: Record<string, boolean> = {}
    for (const [k, v] of Object.entries(toolsDraft)) {
      if (defaultOff.has(k)) {
        if (v) toolsPayload[k] = true
      } else {
        if (!v) toolsPayload[k] = false
      }
    }
    // 命令白名单同理：只上报禁用的命令
    const cmdsOff = Object.fromEntries(
      Object.entries(commandsDraft).filter(([, v]) => !v))
    // 环境变量：多行 KEY=VALUE → 字典。兼容 YAML 冒号写法（README/配置示例就是
    // 冒号形式，复制粘贴进来不能静默丢）；无法解析的行收集起来明确告知，不吞
    const envVars: Record<string, string> = {}
    const skipped: string[] = []
    for (const line of envDraft.value.split('\n')) {
      const s = line.trim()
      if (!s || s.startsWith('#')) continue
      const eq = s.indexOf('=')
      const co = s.indexOf(':')
      const cut = eq > 0 ? eq : co > 0 ? co : -1
      if (cut <= 0) { skipped.push(s); continue }
      const k = s.slice(0, cut).trim()
      const v = s.slice(cut + 1).trim()
      if (!k || !v) { skipped.push(s); continue }
      envVars[k] = v
    }
    if (skipped.length) {
      message()?.(tFmt('以下行无法解析为 KEY=VALUE，已跳过：{p0}', { p0: skipped.join('；') }), 'warning')
    }
    await api.config({
      tools: toolsPayload,
      commands: cmdsOff,
      env: { vars: envVars, path_autofix: pathAutofix.value },
      permissions: { mode: permDraft.mode, timeout_minutes: permDraft.timeout_minutes },
      git: { commit_and_merge: gitCommitAndMergeDraft.value },
      subagent: {
        max_parallel: subagentDraft.max_parallel,
        adaptive: subagentDraft.adaptive,
      },
      local_debug: {
        browser: localDebugDraft.browser,
        browser_executable: localDebugDraft.browser_executable.trim(),
        headless: localDebugDraft.headless,
        allowed_hosts: localDebugDraft.hosts_text.split(/[\n,;]+/).map(s => s.trim()).filter(Boolean),
        navigation_timeout_seconds: localDebugDraft.navigation_timeout_seconds,
        service_ready_timeout_seconds: localDebugDraft.service_ready_timeout_seconds,
        max_services: localDebugDraft.max_services,
        artifacts_retention_days: localDebugDraft.artifacts_retention_days,
      },
    })
    message()?.(t('工具 / 命令白名单 / 访问权限已保存并生效'))
  } catch (e: any) { message()?.(e.message, 'error') }
  finally { toolsSaving.value = false }
  void import('../store').then(m => m.refresh(true))
}

// ---- 镜像源管理与测速选源 ----

const mirrorCategories = computed(() => store.state?.mirror_categories ?? [])
const catOptions = computed(() =>
  mirrorCategories.value.map(c => ({ label: c.label, value: c.key })))

/** 某类别的行 = 服务端内置清单 + 本地草稿新增的自定义源（与内置重复的不再显示） */
function mirrorRows(cat: string): MirrorRowT[] {
  const builtin = store.state?.mirrors_catalog?.[cat] ?? []
  const builtinUrls = new Set(builtin.map(r => r.url))
  const customs: MirrorRowT[] = (mirrorCustom.value[cat] ?? [])
    .filter(u => !builtinUrls.has(u))
    .map(u => ({ url: u, label: t('用户添加'), builtin: false, enabled: true, test_url: u }))
  return [...builtin, ...customs]
}

function mirrorEnabled(url: string): boolean {
  return !mirrorDisabled.value.includes(url)
}

function toggleMirror(url: string, on: boolean): void {
  mirrorDisabled.value = on
    ? mirrorDisabled.value.filter(u => u !== url)
    : [...new Set([...mirrorDisabled.value, url])]
}

function removeCustom(cat: string, url: string): void {
  mirrorCustom.value = {
    ...mirrorCustom.value,
    [cat]: (mirrorCustom.value[cat] ?? []).filter(u => u !== url),
  }
  mirrorDisabled.value = mirrorDisabled.value.filter(u => u !== url)
}

function addCustom(): void {
  const url = addUrl.value.trim()
  if (!/^https?:\/\/\S+$/.test(url)) {
    message()?.(t('请填写 http(s):// 开头、可直接访问的镜像 URL'), 'error')
    return
  }
  const list = mirrorCustom.value[addCat.value] ?? []
  if (list.includes(url)) { message()?.('该源已添加过', 'warning'); return }
  mirrorCustom.value = { ...mirrorCustom.value, [addCat.value]: [...list, url] }
  addUrl.value = ''
  message()?.(t('已添加，点「保存镜像源配置」后生效（Agent 测速时会一并探测）'))
}

/** 延迟徽标：≤800ms 绿 / ≤2500ms 黄 / 更慢或不可达红 / 未测灰 */
function latencyTag(url: string): { type: 'success' | 'warning' | 'error' | 'default'; text: string } {
  if (!(url in mirrorSpeeds.value)) return { type: 'default', text: '未测' }
  const ms = mirrorSpeeds.value[url]
  if (ms == null) return { type: 'error', text: '不可达' }
  if (ms <= 800) return { type: 'success', text: `${Math.round(ms)}ms` }
  if (ms <= 2500) return { type: 'warning', text: `${Math.round(ms)}ms` }
  return { type: 'error', text: `${Math.round(ms)}ms 慢` }
}

const mirrorsDirty = computed(() => {
  const cfg = store.state?.mirrors_cfg
  if (!cfg) return false
  const norm = (c: Record<string, string[]>, d: string[]) =>
    JSON.stringify({ c, d: [...d].sort() })
  return norm(mirrorCustom.value, mirrorDisabled.value)
      !== norm(cfg.custom ?? {}, cfg.disabled ?? [])
})

function syncMirrorDraft(): void {
  const s = store.state
  if (!s) return
  mirrorCustom.value = JSON.parse(JSON.stringify(s.mirrors_cfg?.custom ?? {}))
  mirrorDisabled.value = [...(s.mirrors_cfg?.disabled ?? [])]
}

async function saveMirrors(): Promise<void> {
  mirrorsSaving.value = true
  try {
    const custom = Object.fromEntries(
      Object.entries(mirrorCustom.value).filter(([, v]) => v.length))
    await api.config({ mirrors: { custom, disabled: mirrorDisabled.value } })
    message()?.(t('镜像源配置已保存并生效'))
  } catch (e: any) { message()?.(e.message, 'error') }
  finally { mirrorsSaving.value = false }
  const m = await import('../store')
  await m.refresh(true)
  syncMirrorDraft()  // 以后端清洗结果为准（去重/校验后的落盘值）
}

async function testMirrors(category?: string): Promise<void> {
  if (category) mirrorTestingCat.value = category
  else mirrorTesting.value = true
  try {
    const r = await api.mirrorTest(category)
    mirrorSpeeds.value = r.speeds?.results ?? {}
    mirrorTestedAt.value = r.speeds?.tested_at ?? ''
    message()?.(r.message || t('测速完成'), 'success')
  } catch (e: any) { message()?.(e.message, 'error') }
  finally { mirrorTesting.value = false; mirrorTestingCat.value = null }
}

async function applyMirrors(): Promise<void> {
  mirrorApplying.value = true
  try {
    const r = await api.mirrorApply()
    message()?.(r.message || t('已应用最快源'), 'success')
    const m = await import('../store')
    await m.refresh(true)
    // env.vars 被后端覆盖：同步刷新环境变量草稿，
    // 防止之后点「保存工具与权限」用旧文本框内容覆盖掉刚应用的最快源
    const s = store.state
    if (s) envDraft.value = Object.entries(s.env_cfg ?? {})
      .map(([k, v]) => `${k}=${v}`).join('\n')
  } catch (e: any) { message()?.(e.message, 'error') }
  finally { mirrorApplying.value = false }
}

// 测速结果跟随轮询刷新（Agent 端 speedtest_mirrors 自测后看板即时可见；
// 只覆盖展示数据，不碰 custom/disabled 草稿——编辑中不被冲掉）
watch(() => store.state?.mirror_speeds, (sp) => {
  if (!sp) return
  mirrorSpeeds.value = sp.results ?? {}
  mirrorTestedAt.value = sp.tested_at ?? ''
})

// ---- 软件更新：保存更新源 / 手动检查 / 手动导入本地安装包 ----
const updSaving = shallowRef(false)
async function saveUpdate(): Promise<void> {
  updSaving.value = true
  try {
    const update: Record<string, unknown> = {
      repo: updDraft.repo.trim(), auto_check: updDraft.auto_check,
    }
    // 三态：未触碰不带键（后端保留已存令牌）；触碰过按输入上报，空串 = 清除
    if (updTokenTouched.value) update.token = updDraft.token.trim()
    await api.config({ update })
    message()?.(t('更新配置已保存'))
    updDraft.token = ''
    updTokenTouched.value = false
  } catch (e: any) { message()?.(e.message, 'error') }
  finally { updSaving.value = false }
}

const updChecking = shallowRef(false)
async function checkUpdateNow(): Promise<void> {
  updChecking.value = true
  try {
    const r = await api.updateCheck()
    if (r.phase === 'available') message()?.(tFmt('发现新版本 v{p0}，可点顶部「更新到 {p1}」安装', { p0: r.latest, p1: r.latest }), 'success')
    else message()?.(tFmt('已是最新版本（v{p0}）', { p0: r.current }), 'success')
  } catch (e: any) { message()?.(e.message, 'error') }
  finally { updChecking.value = false }
}

// ---- 手动导入本地安装包：选 zip → 版本对照（同/异版本均允许） → 零 UAC 静默安装 ----
const localPkg = shallowRef('')
const pkgInstalling = shallowRef(false)
function pickPackage(): void {
  openDirBrowser('', (p) => { localPkg.value = p }, 'file')
}
const pkgName = computed(() => localPkg.value.split(/[\\/]/).pop() || '')
const pkgVersion = computed(() => {
  const m = pkgName.value.match(/(\d+\.\d+(?:\.\d+){0,2})/)
  return m ? m[1] : ''
})
const pkgTag = computed(() => {
  const cur = store.state?.version || ''
  if (!pkgVersion.value)
    return { type: 'warning' as const, text: '版本未知（文件名未识别到版本号，仍可安装）' }
  const c = cmpVersion(pkgVersion.value, cur)
  if (c === 0) return { type: 'default' as const, text: `同版本 v${pkgVersion.value}（覆盖重装）` }
  return c > 0
    ? { type: 'success' as const, text: `升级 v${cur} → v${pkgVersion.value}` }
    : { type: 'warning' as const, text: `降级 v${cur} → v${pkgVersion.value}` }
})
function installPackage(): void {
  if (!localPkg.value) return
  const target = pkgVersion.value
  window.$dialog?.warning({
    title: target ? tFmt('安装更新包 v{p0}', { p0: target }) : t('安装更新包（版本未知）'),
    content: `目标文件：${localPkg.value}\n\n注意：若正在执行任务会被中断。安装在后台静默进行`
      + '（全程无 UAC 弹窗，装回程序当前目录），完成后应用自动重启，本页面自动刷新。'
      + t('升级过程中出错会写安装日志并自动打开日志文档；全程无错误则不打开日志。'),
    positiveText: t('安装更新包'),
    negativeText: t('取消'),
    onPositiveClick() {
      pkgInstalling.value = true
      api.updateInstall(localPkg.value)
        .then(() => {
          // 挂目标版本（未知按当前版本核对）；遮罩立即出现，应用回归后自动刷新
          beginRestartWait(target || store.state?.version || '')
          message()?.(t('正在静默升级，应用将短暂离线并自动重启'), 'success')
        })
        .catch((e: any) => { message()?.(e.message, 'error') })
        .finally(() => { pkgInstalling.value = false })
    },
  })
}


// ---- 模型列表与规格自动填充 ----
const fetching = shallowRef(false)
const testing = shallowRef(false)
const modelSpecs = ref<ModelInfo[]>([])
// label 必须与 value 一致：naive 选中后按 label 回填输入框，带标注的 label
// 会被当成模型名存进配置（曾导致「step-5-preview（977K）」404）
const modelOptions = computed(() =>
  modelSpecs.value.map(m => ({ label: m.id, value: m.id })))

// 全局预设池各角色可用模型选项（仅动态聚合实际配置与在线拉取的真实模型，不硬编码任何预设）
const profileModelOptions = computed(() => {
  const set = new Set<string>()
  if (draft.model?.trim()) set.add(draft.model.trim())
  if (store.state?.llm?.model) set.add(store.state.llm.model)
  for (const m of modelSpecs.value) {
    if (m.id) set.add(m.id)
  }
  if (reviewDraft.model?.trim()) set.add(reviewDraft.model.trim())
  for (const m of reviewModelOptions.value) {
    if (m.id) set.add(m.id)
  }
  for (const p of Object.values(profilesDraft.value || {})) {
    if (p.llm?.model?.trim()) set.add(p.llm.model.trim())
  }
  return Array.from(set).map(id => ({ label: id, value: id }))
})
// 选中模型的规格摘要（厂商风格单位显示，如「上下文 1M · 最大输出 32K」）
const selectedSpec = computed(() =>
  modelSpecs.value.find(m => m.id.toLowerCase() === draft.model.trim().toLowerCase()))

async function fetchModels(): Promise<void> {
  fetching.value = true
  try {
    const r = await api.models({
      provider: draft.provider, base_url: draft.base_url.trim(), api_key: draft.api_key.trim(),
    })
    modelSpecs.value = r.models
    message()?.(tFmt('已获取 {p0} 个模型：在「模型」框下拉选择，规格自动填充', { p0: r.models.length }))
  } catch (e: any) { message()?.(e.message, 'error') }
  finally { fetching.value = false }
}

// 测试模型连通性：优先用表单草稿值（未保存也能测），缺省字段由后端回退已存配置
async function testModel(): Promise<void> {
  testing.value = true
  try {
    const r = await api.testModel({
      provider: draft.provider, base_url: draft.base_url.trim(),
      api_key: draft.api_key.trim(), model: draft.model.trim(),
    })
    message()?.(r.message || t('连接成功'), 'success')
  } catch (e: any) { message()?.(e.message, 'error') }
  finally { testing.value = false }
}

function onModelPick(v: string | null): void {
  draft.model = (v || '').trim()
  if (!draft.model) return
  const m = modelSpecs.value.find(x => x.id.toLowerCase() === draft.model.toLowerCase())
  if (!m) return
  if (m.context_window) draft.max_context_tokens = m.context_window
  if (m.max_output_tokens) draft.max_tokens = m.max_output_tokens
  if (m.context_window || m.max_output_tokens)
    message()?.(t('已按该模型的规格自动填充上下文窗口与最大输出（可手改）'))
}

const providerOptions = [
  { label: t('openai（兼容协议）'), value: 'openai' },
  { label: 'anthropic', value: 'anthropic' },
]
</script>

<template>
  <div class="settings" v-if="store.state">
    <div class="layout">
      <!-- 移动端：分类下拉 + 子项胶囊（窄屏隐藏左侧手风琴，省出纵向空间） -->
      <div class="mobile-menu">
        <NSelect v-model:value="mobileGroup" :options="groupOptions" size="small" />
        <div class="mchips">
          <button v-for="it in mobileGroupItems" :key="it.key" type="button"
                  class="mchip" :class="{ active: settingsSection === it.key }"
                  @click="selectSection(it.key)">{{ it.label }}</button>
        </div>
      </div>
      <!-- 左：功能分区手风琴（单开；key 与 settingsSection 联动支持跨页定位） -->
      <aside class="menu">
        <div v-for="g in SETTING_GROUPS" :key="g.key" class="mgroup">
          <button type="button" class="mgroup-head" :class="{ open: openGroups[g.key] }"
                  @click="toggleGroup(g.key)">
            <i class="ms">{{ g.icon }}</i>
            <span class="mgroup-label">{{ g.label }}</span>
            <i class="ms mchev">{{ openGroups[g.key] ? 'expand_less' : 'expand_more' }}</i>
          </button>
          <div v-show="openGroups[g.key]" class="mchildren">
            <button v-for="it in g.items" :key="it.key" type="button"
                    class="mchild" :class="{ active: settingsSection === it.key }"
                    :title="it.desc" @click="selectSection(it.key)">
              <i class="ms">{{ it.icon }}</i><span>{{ it.label }}</span>
            </button>
          </div>
        </div>
      </aside>
      <!-- 右：当前分区内容（v-if 懒加载，未选中不挂载不轮询开销） -->
      <section class="content">
        <div v-if="settingsSection === 'llm'">
          <NCard size="small" class="scard">
            <template #header><i class="ms">smart_toy</i> {{ t('大模型接入') }}</template>
      <div class="frow">
        <div class="f1"><label>{{ t('协议 provider') }}</label>
          <NSelect v-model:value="draft.provider" :options="providerOptions" /></div>
        <div class="f2"><label>{{ t('模型 model（下拉选择或输入过滤，支持自定义 id）') }}</label>
          <NSelect v-model:value="draft.model" :options="modelOptions" filterable tag
                   :placeholder="t('deepseek-chat 或获取列表后选择')" @update:value="onModelPick" /></div>
        <div class="f0"><label>&nbsp;</label>
          <div class="btnrow">
            <NButton :loading="fetching" @click="fetchModels">{{ t('获取列表') }}</NButton>
            <NButton :loading="testing" :title="t('发送一条极短对话，验证网络/密钥/模型名是否可用')" @click="testModel">{{ t('测试连接') }}</NButton>
          </div></div>
      </div>
      <div class="modelspec" v-if="selectedSpec && (selectedSpec.context_window || selectedSpec.max_output_tokens)">
        该模型规格：上下文 {{ fmtTokens(selectedSpec.context_window) || '未知' }} ·
        最大输出 {{ fmtTokens(selectedSpec.max_output_tokens) || '未知' }}（已自动填充到下方，可手改）
      </div>
      <div class="frow">
        <div class="f2"><label>{{ t('API 地址 base_url') }}</label><NInput v-model:value="draft.base_url" :placeholder="draft.provider === 'anthropic' ? 'https://api.anthropic.com（自动补 /v1）' : 'https://api.deepseek.com/v1'" /></div>
        <div class="f14"><label>API Key</label>
          <NInput v-model:value="draft.api_key" type="password" show-password-on="click"
                  :placeholder="apiKeyConfigured
                    ? `已配置：${apiKeyMasked}（留空保持不变）` : 'sk-...'"
                  @update:value="apiKeyTouched = true" /></div>
      </div>
      <div class="hint" v-if="draft.provider === 'anthropic'">
        Anthropic 协议：直接填官方/兼容服务公布的根地址即可，程序自动补 /v1（已含 /v1 则不重复）；多数兼容服务不提供 /models 列表端点，拉取 404 时直接手填模型名即可。
      </div>
      <div class="frow">
        <div class="f1"><label>{{ t('采样温度 temperature') }}</label><NInputNumber v-model:value="draft.temperature" :step="0.1" :min="0" :max="2" /></div>
        <div class="f1"><label>{{ t('最大输出 max_tokens（拉取过模型列表时，上限跟随模型上报值）') }}</label><NInputNumber v-model:value="draft.max_tokens" :step="256" :min="256" :max="selectedSpec?.max_output_tokens || 200000" /></div>
      </div>
      <div class="frow">
        <div class="f1"><label>{{ t('上下文窗口 max_context_tokens（上限同样跟随模型上报值）') }}</label><NInputNumber v-model:value="draft.max_context_tokens" :step="1024" :min="4096" :max="selectedSpec?.context_window || 2000000" /></div>
        <div class="f1"><label>{{ t('请求超时（秒，-1 = 不超时，挂起时可用看板「强制停止」打断）') }}</label><NInputNumber v-model:value="draft.timeout_seconds" :min="-1" :max="3600" /></div>
      </div>
      <label class="checkline" style="margin-bottom: 10px">
        <NSwitch v-model:value="draft.enable_thinking" />
        请求侧开启模型思考
        <span class="hint">{{ t('默认关闭：DeepSeek-R1 等推理模型会自动返回思考内容，无需开启。anthropic 需此项才会返回 thinking 块（预留一半 max_tokens 作思考预算）；OpenAI 兼容服务按 Qwen（enable_thinking）/ GLM（thinking）约定传参，不支持的服务请勿开启。') }}</span>
      </label>
      <div class="sfoot">
        <NButton type="primary" @click="saveLlm">{{ t('保存模型配置') }}</NButton>
      </div>
          </NCard>
        </div>
        <div v-if="settingsSection === 'profiles'">
          <NCard size="small" class="scard">
            <template #header><i class="ms">group</i> {{ t('多模型项目小组 (Team Profiles)') }}</template>
            <div class="team-banner" style="background: rgba(91, 140, 255, 0.08); border: 1px solid rgba(91, 140, 255, 0.25); border-radius: 8px; padding: 10px 14px; margin-bottom: 14px; font-size: 12.5px; line-height: 1.6; color: var(--text);">
              <div style="font-weight: 600; color: #5b8cff; margin-bottom: 4px;"><i class="ms sm">lightbulb</i> {{ t('多模型 Agent 项目小组模式 (Team) 配置说明') }}</div>
              <div>
                系统采用<b>「全局角色预设池 + 项目小队自由进驻」</b>的双层架构：
                <ul style="margin: 4px 0 0 16px; padding: 0;">
                  <li><b>{{ t('全局层（此处）') }}</b>{{ t('：统一配置') }} <b>{{ t('项目经理 (PM)') }}</b>、<b>{{ t('后端工程师') }}</b>、<b>{{ t('前端工程师') }}</b>、<b>{{ t('测试工程师 (QA)') }}</b> {{ t('各自绑定的专属大模型（如 DeepSeek-R1、Claude 3.7、GPT-4o）与独立 API 密钥，一次配置后全系统共享；') }}</li>
                  <li><b>{{ t('项目层进驻') }}</b>{{ t('：每个项目可独立决定是否启用小组模式。在「项目」页或「看板」卡片点击') }}<b>{{ t('「设置」→「多模型 Agent 项目小组模式 (Team)」') }}</b>{{ t('即可一键勾选启用小队进驻并自由调整成员名单。') }}</li>
                </ul>
              </div>
            </div>
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; gap:8px; flex-wrap:wrap;">
              <span class="hint">{{ t('配置不同专业职能角色的专属大模型与能力边界，进驻任意项目时可自由选用或局部微调。') }}</span>
              <NButton size="small" type="primary" secondary @click="addProfile">{{ t('+ 新建角色预设') }}</NButton>
            </div>

            <div class="profiles-container">
              <div v-for="(prof, pid) in profilesDraft" :key="pid" class="group-card profile-card" :class="{ 'active-card': editingProfileId === pid }">
                <div class="group-header" style="cursor: pointer" @click="editingProfileId = editingProfileId === pid ? null : pid">
                  <div class="group-title-wrap" style="display:flex; align-items:center; gap:6px;">
                    <span class="group-icon"><i class="ms">{{ getRoleIcon(prof.role) }}</i></span>
                    <span class="group-title">{{ prof.name }}</span>
                    <NTag size="tiny" round type="info">{{ prof.role.toUpperCase() }}</NTag>
                    <NTag size="tiny" round :bordered="false" style="font-family:Consolas, monospace; font-size:10.5px; background:rgba(128,128,160,0.12); color:var(--dim);">ID: {{ pid }}</NTag>
                    <span class="hint" v-if="prof.llm?.model" style="display:inline-flex; align-items:center;">· 模型: {{ prof.llm.model }}</span>
                    <span class="hint" v-else style="display:inline-flex; align-items:center;">{{ t('· 继承全局模型') }}</span>
                  </div>
                  <div class="group-ops" @click.stop>
                    <NButton size="tiny" secondary :loading="profileFetchingId === pid" @click="fetchProfileModelList(pid)">{{ t('获取模型列表') }}</NButton>
                    <NButton size="tiny" secondary :loading="profileTestingId === pid" @click="testProfileModel(pid)">{{ t('测试连接') }}</NButton>
                    <NButton size="tiny" quaternary type="error" @click="removeProfile(pid)">{{ t('删除') }}</NButton>
                    <span style="font-size: 12px; color: var(--dim)"><i class="ms sm">{{ editingProfileId === pid ? 'expand_less' : 'edit' }}</i> {{ editingProfileId === pid ? '收起' : '编辑' }}</span>
                  </div>
                </div>

                <div v-if="editingProfileId === pid" style="padding: 12px; border-top: 1px solid var(--line); background: var(--card);">
                  <div class="frow">
                    <div class="f1">
                      <label>{{ t('预设名称') }}</label>
                      <NInput v-model:value="prof.name" :placeholder="t('如 资深后端开发')" />
                    </div>
                    <div class="f1">
                      <label>{{ t('职能角色') }}</label>
                      <NSelect v-model:value="prof.role" :options="roleSelectOptions" />
                    </div>
                    <div class="f14">
                      <label>{{ t('职责简介（将展示在团队名册）') }}</label>
                      <NInput v-model:value="prof.description" :placeholder="t('负责接口与业务逻辑实现')" />
                    </div>
                  </div>

                  <div class="frow" style="margin-top: 8px">
                    <div class="f1">
                      <label>{{ t('协议 provider（留空继承全局）') }}</label>
                      <NSelect v-model:value="prof.llm.provider" :options="[
                        { label: '继承全局', value: '' },
                        { label: 'openai（兼容协议）', value: 'openai' },
                        { label: 'anthropic', value: 'anthropic' },
                      ]" />
                    </div>
                    <div class="f1">
                      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                        <label style="margin:0">{{ t('模型名 model（留空继承全局）') }}</label>
                        <NButton size="tiny" quaternary type="primary" :loading="profileFetchingId === pid" @click="fetchProfileModelList(pid)" style="font-size:11px; padding:0 4px">{{ t('获取模型列表') }}</NButton>
                      </div>
                      <NSelect
                        v-model:value="prof.llm.model"
                        :options="profileModelOptions"
                        filterable
                        tag
                        clearable
                        :placeholder="'留空继承全局（当前 ' + (draft.model || store.state?.llm?.model || '-') + '）'"
                        @update:value="(v: string | null) => { prof.llm.model = v || '' }"
                      />
                    </div>
                    <div class="f14">
                      <label>{{ t('接口 base_url（留空继承全局）') }}</label>
                      <NInput v-model:value="prof.llm.base_url" placeholder="https://api.deepseek.com/v1" />
                    </div>
                  </div>

                  <div class="frow" style="margin-top: 8px">
                    <div class="f2">
                      <label>{{ t('API Key（留空继承全局或保持已配置密钥）') }}</label>
                      <NInput v-model:value="prof.llm.api_key" type="password" show-password-on="click"
                              :placeholder="prof.llm.api_key_configured ? `已配置（${prof.llm.api_key_masked}），留空保持` : 'sk-...'"
                              @update:value="profilesTouchedKeys[pid] = true" />
                    </div>
                    <div class="f1">
                      <label>{{ t('单任务轮数上限（留空用默认）') }}</label>
                      <NInputNumber v-model:value="prof.max_turns" :min="-1" :max="200" :placeholder="t('默认')" />
                    </div>
                  </div>

                  <div v-if="prof.role === 'qa'" style="margin-top: 6px;">
                    <label class="checkline">
                      <NSwitch size="small" v-model:value="prof.allow_test_scripts" />
                      <span>{{ t('允许测试工程师编写/补充自动化测试脚本（开启后允许 QA 新增测试目录脚本并纳入提交）') }}</span>
                    </label>
                  </div>

                  <div style="margin-top: 8px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                      <label style="font-size: 12px; color: var(--dim); margin: 0;">{{ t('角色专属补充提示词（注入该角色系统提示词）') }}</label>
                      <NButton size="tiny" quaternary type="primary" @click="loadRolePromptTemplate(prof)" style="font-size: 11.5px; padding: 0 4px;"><i class="ms sm">description</i> {{ t('载入内置角色模版（类似 AGENT.md）') }}</NButton>
                    </div>
                    <NInput v-model:value="prof.extra_prompt" type="textarea" :rows="4" :autosize="{ minRows: 3, maxRows: 14 }"
                            :placeholder="t('如：工作流与协作规范、测试要求、编码风格与交付验收标准...')" />
                  </div>
                </div>
              </div>
            </div>

            <div class="sfoot" style="margin-top: 14px;">
              <NButton type="primary" :loading="profilesSaving" @click="saveProfiles">{{ t('保存角色预设池') }}</NButton>
            </div>
          </NCard>
        </div>
        <div v-if="settingsSection === 'sched'">
          <NCard size="small" class="scard">
            <template #header><i class="ms">schedule</i> {{ t('调度与看板') }}</template>
      <div class="frow">
        <div class="f1"><label>{{ t('扫描间隔（秒，30~86400）') }}</label><NInputNumber v-model:value="draft.interval" :min="30" :max="86400" /></div>
        <div class="f1"><label>{{ t('并行 Agent 数（1~16）') }}</label><NInputNumber v-model:value="draft.parallel" :min="1" :max="16" /></div>
        <div class="f1"><label>{{ t('看板端口（下次启动生效）') }}</label><NInputNumber v-model:value="draft.port" :min="1" :max="65535" /></div>
      </div>
      <div class="frow">
        <div class="f1"><label>{{ t('日志保留（天，0 为关闭自动归档清理，保存后立即执行）') }}</label>
          <NInputNumber v-model:value="draft.retention" :min="0" :max="365" /></div>
        <div class="f1"><label>{{ t('日志总占用上限（MB，超出从最旧删起，0 为不限制）') }}</label>
          <NInputNumber v-model:value="draft.max_total_mb" :min="0" :max="100000" /></div>
      </div>
      <div class="frow">
        <div class="f1"><label>{{ t('看板登录密码（非空时访问页面/API 均需先登录，保存后立即生效）') }}</label>
          <NInput :value="authPwd" type="password" show-password-on="click"
                  :placeholder="store.state.web_cfg.auth_enabled
                    ? '已设置，留空保持不变' : '未设置（免登录；外网部署务必设置）'"
                  @update:value="onPwdInput" /></div>
        <div class="f0 pwd-btns">
          <NButton v-if="store.state.web_cfg.auth_enabled" size="small" quaternary
                   type="error" :disabled="authPwdClear" @click="clearAuthPwd">{{ t('清除密码（关闭鉴权）') }}</NButton>
        </div>
      </div>
      <div class="hint pwd-hint" v-if="authPwdClear">
        将在保存后关闭登录鉴权（任何能访问该端口的人都可操作看板）；在上方重新输入密码可取消。
      </div>
      <div class="hint pwd-hint" v-else-if="authPwdTouched">
        将在保存后启用/更新登录密码，所有已登录会话立即失效，需用新密码重新登录。
      </div>
      <div class="sfoot"><NButton type="primary" @click="saveSched">{{ t('保存调度配置') }}</NButton></div>
          </NCard>
        </div>
        <div v-if="settingsSection === 'notify'">
          <NCard size="small" class="scard">
            <template #header><i class="ms">notifications</i> {{ t('提醒设置') }}</template>
      <div class="frow">
        <div class="f1"><label class="checkline">
          <NSwitch v-model:value="notifyDraft.sound" />
          提示音
          <span class="hint">{{ t('完成/受阻时播程序内置提示音（完成「叮咚」，受阻警示音；随程序分发，不跟随系统声音方案），音频缺失时回退 Windows 系统音。') }}</span>
        </label></div>
        <div class="f1"><label class="checkline">
          <NSwitch v-model:value="notifyDraft.system" />
          系统通知
          <span class="hint">{{ t('完成/受阻时弹托盘气泡，看板没开也能收到；--no-tray 启动时无渠道。') }}</span>
        </label></div>
      </div>
      <div class="sfoot"><NButton type="primary" :loading="notifySaving" @click="saveNotify">{{ t('保存提醒设置') }}</NButton></div>
          </NCard>
        </div>
        <div v-if="settingsSection === 'masking'">
          <NCard size="small" class="scard">
            <template #header><i class="ms">security</i> {{ t('数据脱敏') }}</template>
      <label class="checkline">
        <NSwitch v-model:value="maskingDraft.enabled" />
        启用脱敏
        <span class="hint">{{ t('发送模型厂商前在本地掩除密码/密钥等敏感信息（高置信规则：sk-/ghp_/glpat-/AKIA/AIza/xox-/JWT/PEM 私钥块/Bearer/URL token 参数/连接串密码段）。处理只在内存中进行，不写磁盘、不进日志；模型写回的占位符在落盘前自动还原原文，任务结束即清除映射。') }}</span>
      </label>
      <label class="checkline">
        <NSwitch v-model:value="maskingDraft.enhanced_rules" />
        键值对增强规则
        <span class="hint">{{ t('掩除 api_key=xxx、"password": "xxx" 等键值对的值（键保留）。覆盖面广但误伤面也大——文档示例、测试夹具里的假密钥同样会被替换，模型将读到占位符而非原文，默认关闭，确认工作区无此类内容再开启。') }}</span>
      </label>
      <div class="frow">
        <div class="f1">
          <label>{{ t('自定义脱敏正则（每行一条，整段匹配替换；≤20 条、每条 ≤500 字符）') }}</label>
          <NInput v-model:value="maskingDraft.patternsText" type="textarea"
                  :autosize="{ minRows: 3, maxRows: 8 }"
                  :placeholder="t('例：PROJECT_SECRET_[A-Za-z0-9]{16,}')" />
        </div>
      </div>
      <div class="sfoot"><NButton type="primary" :loading="maskingSaving" @click="saveMasking">{{ t('保存脱敏设置') }}</NButton></div>
          </NCard>
        </div>
        <div v-if="settingsSection === 'runlog'">
          <NCard size="small" class="scard">
            <template #header><i class="ms">receipt_long</i> {{ t('运行台账（RunLog）') }}</template>
            <div v-if="runlogBusy || mig?.state === 'failed'" class="migbox">
              <NProgress type="line" :percentage="migPercent" :processing="runlogBusy"
                         :status="mig?.state === 'failed' ? 'error' : 'default'" />
              <div class="hint" style="margin-top:6px">
                <template v-if="runlogBusy">
                  正在迁移 {{ mig?.done_runs || 0 }}/{{ mig?.total_runs || 0 }} 个 run ·
                  {{ fmtMB(mig?.done_bytes) }}/{{ fmtMB(mig?.total_bytes) }}
                  <template v-if="mig?.current"> · 当前 {{ mig.current }}</template>
                </template>
                <template v-else>
                  迁移失败：{{ mig?.error || '未知错误' }}
                  <NButton size="tiny" type="warning" style="margin-left:8px"
                           @click="retryRunlogMigration">{{ t('重试迁移') }}</NButton>
                </template>
              </div>
            </div>
            <label class="checkline">
              <NSwitch v-model:value="draft.runlog_enabled" :disabled="runlogBusy" />
              启用运行台账
              <span class="hint">{{ t('每个任务一轮执行生成一个 run 目录：事件流 + diff + 测试输出 + 审查结论 + 用量。用于看板「任务详情 → 运行记录」回放；关闭后不再记录，不影响任务执行。') }}</span>
            </label>
            <div class="frow">
              <div class="f1"><label>{{ t('保留天数（0 为不按时间清理）') }}</label>
                <NInputNumber v-model:value="draft.runlog_retention" :min="0" :max="3650"
                              :disabled="runlogBusy" /></div>
              <div class="f1"><label>{{ t('总占用上限（MB，超出从最旧 run 删起，0 为不限制）') }}</label>
                <NInputNumber v-model:value="draft.runlog_max_mb" :min="0" :max="100000"
                              :disabled="runlogBusy" /></div>
            </div>
            <div class="frow">
              <div class="f1"><label>{{ t('台账目录（留空 = 配置文件同级 runs/；改目录会自动迁移历史记录）') }}</label>
                <div class="pathline">
                  <NInput v-model:value="draft.runlog_dir" :disabled="runlogBusy"
                          :placeholder="t('留空使用默认目录；也可输入绝对路径')" />
                  <NButton :disabled="runlogBusy" @click="pickRunlogDir">{{ t('浏览…') }}</NButton>
                </div>
              </div>
            </div>
            <div class="hint">
              默认不写入受管仓库；自定义目录建议放本机独立磁盘。切换目录后后台异步迁移，
              迁移期间台账设置与回放按钮会锁定并显示进度。
            </div>
            <div class="sfoot"><NButton type="primary" :loading="runlogSaving"
                                        :disabled="runlogBusy" @click="saveRunlog">{{ t('保存运行台账设置') }}</NButton></div>
          </NCard>
        </div>
        <div v-if="settingsSection === 'budget'">
          <NCard size="small" class="scard">
            <template #header><i class="ms">savings</i> {{ t('预算与成本') }}</template>
            <label class="checkline">
              <NSwitch v-model:value="draft.budget_enabled" />
              启用预算硬闸
              <span class="hint">{{ t('超限立即停止本次执行（任务标记为受阻，原因 budget_exceeded），保留 worktree 成果；项目当日用尽后暂停领取新任务。默认关闭。') }}</span>
            </label>
            <div class="frow">
              <div class="f1"><label>{{ t('单任务 token 上限（prompt + completion，0 = 不限制）') }}</label>
                <NInputNumber v-model:value="draft.budget_task_tokens" :min="0" :max="1000000000" /></div>
              <div class="f1"><label>{{ t('项目当日 token 上限（本地自然日，0 = 不限制）') }}</label>
                <NInputNumber v-model:value="draft.budget_day_tokens" :min="0" :max="10000000000" /></div>
              <div class="f1"><label>{{ t('单任务成本上限（元，需在 config.yaml 的 budget.prices 配单价）') }}</label>
                <NInputNumber v-model:value="draft.budget_task_cost" :min="0" :max="1000000000" /></div>
            </div>
            <div class="hint">
              成本只对配置了单价的模型计算（`budget.prices: {模型名: {prompt: 元/百万, completion: 元/百万}}`，单价表当前在 config.yaml 维护）。
              预算超限不是失败：任务停在受阻，调高预算后重新入队即可续跑。
            </div>
            <label style="margin-top:6px">{{ t('各项目今日用量') }}</label>
            <div class="budget-today">
              <div v-for="(v, k) in (store.state.budget_today || {})" :key="k" class="budget-row">
                <span class="budget-name">{{ k }}</span>
                <span>{{ fmtTokens(v.total_tokens || 0) }} tokens</span>
                <span>¥{{ (v.cost_cny || 0).toFixed(4) }}</span>
              </div>
              <div v-if="!Object.keys(store.state.budget_today || {}).length" class="hint">{{ t('今日暂无用量记录') }}</div>
            </div>
            <div class="sfoot"><NButton type="primary" :loading="budgetSaving" @click="saveBudget">{{ t('保存预算设置') }}</NButton></div>
          </NCard>
        </div>
        <div v-if="settingsSection === 'resilience'">
          <NCard size="small" class="scard">
            <template #header><i class="ms">health_and_safety</i> {{ t('熔断与恢复') }}</template>
            <label class="checkline">
              <NSwitch v-model:value="draft.res_enabled" />
              启用熔断停机
              <span class="hint">{{ t('连续失败或窗口失败率超阈值时，项目暂停领取新任务；正在跑的任务允许收尾。默认需要人工恢复，避免自动重启掩盖系统性故障。') }}</span>
            </label>
            <div class="frow">
              <div class="f1"><label>{{ t('连续失败阈值（0 = 不按此规则）') }}</label>
                <NInputNumber v-model:value="draft.res_consec" :min="0" :max="100" /></div>
              <div class="f1"><label>{{ t('失败率统计窗口（分钟）') }}</label>
                <NInputNumber v-model:value="draft.res_window" :min="1" :max="1440" /></div>
            </div>
            <div class="frow">
              <div class="f1"><label>{{ t('窗口失败率阈值（%，0 = 不按此规则）') }}</label>
                <NInputNumber v-model:value="draft.res_rate_pct" :min="0" :max="100" /></div>
              <div class="f1"><label>{{ t('窗口最少样本数') }}</label>
                <NInputNumber v-model:value="draft.res_samples" :min="1" :max="1000" /></div>
            </div>
            <label class="checkline">
              <NSwitch v-model:value="draft.res_auto" />
              窗口结束后自动恢复
              <span class="hint">{{ t('默认关闭：熔断后必须人工点「恢复」。开启后暂停满一个窗口自动恢复一次（清空旧失败记录）。') }}</span>
            </label>
            <label style="margin-top:6px">{{ t('当前暂停的项目') }}</label>
            <div class="budget-today">
              <div v-for="p in pausedProjects" :key="p.name" class="budget-row">
                <span class="budget-name">{{ p.name }} · {{ p.health?.pause_reason || '已暂停' }}</span>
                <NButton size="tiny" type="warning" @click="resumeProject(p.name)">{{ t('恢复') }}</NButton>
              </div>
              <div v-if="!pausedProjects.length" class="hint">{{ t('当前没有暂停的项目') }}</div>
            </div>
            <div class="sfoot"><NButton type="primary" :loading="resilienceSaving" @click="saveResilience">{{ t('保存熔断设置') }}</NButton></div>
          </NCard>
        </div>
        <div v-if="settingsSection === 'audit'">
          <NCard size="small" class="scard">
            <template #header><i class="ms">history</i> {{ t('审计日志') }}</template>
            <div class="frow">
              <div class="f1"><label>{{ t('动作过滤（如 task / project / auth）') }}</label>
                <NInput v-model:value="auditAction" :placeholder="t('留空 = 全部')" /></div>
              <div class="f1"><label>{{ t('操作者过滤（user / api_key / local）') }}</label>
                <NInput v-model:value="auditActor" :placeholder="t('留空 = 全部')" /></div>
              <div class="f1" style="justify-content:flex-end">
                <NButton type="primary" secondary :loading="auditLoading" @click="loadAudit">{{ t('刷新') }}</NButton>
              </div>
            </div>
            <div class="audit-list">
              <div v-for="(e, i) in auditEntries" :key="i" class="audit-row">
                <span class="audit-time">{{ (e.ts || '').slice(5, 19).replace('T', ' ') }}</span>
                <span class="audit-action">{{ e.action }}</span>
                <span class="audit-actor">{{ e.actor }}<template v-if="e.ip"> · {{ e.ip }}</template></span>
                <span class="audit-target">{{ e.target }}</span>
                <NTag size="tiny" :bordered="false"
                      :type="e.result === 'ok' ? 'success' : 'error'">{{ e.result }}</NTag>
              </div>
              <div v-if="!auditLoading && !auditEntries.length" class="hint">{{ t('暂无审计记录') }}</div>
            </div>
            <div class="hint">
              只展示最近记录；完整日志在数据目录 `logs/audit.jsonl`。参数只记摘要，不记密码、API Key 或任务正文。
            </div>
          </NCard>
        </div>
        <div v-if="settingsSection === 'review'">
          <NCard size="small" class="scard">
            <template #header><i class="ms">rate_review</i> {{ t('任务审查（Review Agent）') }}</template>
      <label class="checkline">
        <NSwitch v-model:value="reviewDraft.enabled" />
        启用审查（配置审查模型后即对全部项目默认生效；项目可单独关闭）
      </label>
      <template v-if="reviewDraft.enabled">
        <div class="frow" style="margin-top:8px">
          <div class="f1"><label>{{ t('协议 provider') }}</label>
            <NSelect v-model:value="reviewDraft.provider" :options="[
              { label: 'openai（兼容协议）', value: 'openai' },
              { label: 'anthropic', value: 'anthropic' },
            ]" /></div>
          <div class="f2"><label>{{ t('审查模型 model（必须与任务模型不同）') }}</label>
            <NSelect v-model:value="reviewDraft.model" :options="reviewModelOptions.map(m => ({ label: m.id, value: m.id }))"
                     filterable tag clearable :placeholder="t('如 deepseek-chat / glm-4-flash（可配便宜小模型）')"
                     @update:value="(v: string | null) => { reviewDraft.model = v || '' }" /></div>
        </div>
        <div class="frow">
          <div class="f1"><label>{{ t('接口 base_url') }}</label>
            <NInput v-model:value="reviewDraft.base_url" placeholder="https://api.deepseek.com/v1" /></div>
          <div class="f1"><label>{{ t('密钥 api_key') }}</label>
            <NInput v-model:value="reviewDraft.api_key" type="password" show-password-on="click"
                    :placeholder="reviewKeyConfigured && reviewKeyMasked ? `已配置（${reviewKeyMasked}），留空保持原值` : '审查模型服务密钥'"
                    @update:value="reviewKeyTouched = true" /></div>
        </div>
        <div class="frow">
          <div class="f1"><label>{{ t('单次审查轮数上限（max_turns）') }}</label>
            <NInputNumber v-model:value="reviewDraft.max_turns" :min="-1" :max="100" :placeholder="t('-1 为不限')" />
            <div class="hint" style="margin-top:2px">{{ t('单次审查工具交互轮次上限（-1 为不限，默认 10）') }}</div></div>
          <div class="f1"><label>{{ t('返工审查轮数上限（全局默认，-1 为不上限）') }}</label>
            <NInputNumber v-model:value="reviewDraft.max_review_rounds" :min="-1" :max="10" :placeholder="t('-1 为不限')" />
            <div class="hint" style="margin-top:2px">{{ t('不通过累计达到上限转受阻（-1 表示不上限持续返工）') }}</div></div>
        </div>
        <label class="checkline stack">
          <NSwitch v-model:value="reviewDraft.allow_test_commands" />
          审查可执行测试命令
          <span class="hint">{{ t('审查 agent 可用 run_command 实测验证（受项目命令白名单与访问权限审批约束）；关闭=纯只读审查。') }}</span>
        </label>
        <label class="checkline stack">
          <NSwitch v-model:value="reviewDraft.squash_merge" />
          返工通过后收敛提交
          <span class="hint">{{ t('返工任务的多个 WIP 提交在通过审查后 merge --squash 收敛为单提交；关闭=保留 --no-ff 合并。') }}</span>
        </label>
        <div class="btnrow" style="margin-top:6px">
          <NButton size="small" :loading="reviewFetching" @click="fetchReviewModelList">{{ t('获取模型列表') }}</NButton>
          <span class="hint">{{ t('审查与任务使用不同模型交叉验证，防止「自审自批」；后端保存时强制校验互斥。') }}</span>
        </div>
      </template>
      <div class="sfoot"><NButton type="primary" :loading="reviewSaving" @click="saveReview">{{ t('保存审查配置') }}</NButton></div>
          </NCard>
        </div>
        <div v-if="settingsSection === 'tools'">
          <NCard size="small" class="scard">
            <template #header><i class="ms">handyman</i> {{ t('Agent 工具与访问权限') }}</template>
            <!-- 一级：四大核心分类 Tab -->
            <div style="margin-bottom: 14px;">
              <NTabs v-model:value="toolsTab" type="segment" size="small" class="tools-tabs">
                <NTabPane name="tools">
                  <template #tab>
                    <i class="ms">handyman</i> Agent 工具能力
                    <NTag size="tiny" round :type="toolsEnabledCount === toolsTotalCount ? 'success' : 'default'" style="margin-left: 6px;">
                      {{ toolsEnabledCount }}/{{ toolsTotalCount }}
                    </NTag>
                  </template>
                </NTabPane>
                <NTabPane name="commands">
                  <template #tab>
                    <i class="ms">terminal</i> 终端命令白名单
                    <NTag size="tiny" round :type="cmdsEnabledCount === cmdsTotalCount ? 'success' : 'warning'" style="margin-left: 6px;">
                      {{ cmdsEnabledCount }}/{{ cmdsTotalCount }}
                    </NTag>
                  </template>
                </NTabPane>
                <NTabPane name="debug">
                  <template #tab>
                    <i class="ms">language</i> 本地调试与浏览器
                    <NTag size="tiny" round :type="debugActiveCount > 0 ? 'info' : 'default'" style="margin-left: 6px;">
                      {{ debugActiveCount > 0 ? `${debugActiveCount} 项启用` : '默认关闭' }}
                    </NTag>
                  </template>
                </NTabPane>
                <NTabPane name="perms"><template #tab><i class="ms">lock</i> {{ t('权限模式与环境') }}</template></NTabPane>
              </NTabs>
            </div>

            <!-- TAB 1: Agent 工具能力 (Core & Extended Tools) -->
            <div v-show="toolsTab === 'tools'" class="tab-content">
              <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; gap:12px; flex-wrap:wrap;">
                <span class="hint">{{ t('控制 Agent 在任务中可调用的内置核心工具；关闭的工具不会下发给大模型提示词。') }}</span>
                <NInput v-model:value="toolSearch" size="small" :placeholder="t('搜索工具名称或描述...')" clearable style="width: 240px;" />
              </div>

              <div class="tool-groups-container">
                <!-- 子代理并发预算：厂商不公开并发上限，靠 429 反馈自适应逼近 -->
                <div class="group-card">
                  <div class="group-header">
                    <div class="group-title-wrap">
                      <span class="group-icon"><i class="ms sm">bolt</i></span>
                      <span class="group-title">{{ t('子代理并发预算') }}</span>
                      <span class="group-desc">{{ t('限制真正打给厂商的在途请求数（与「单次最多派 4 个子代理」的熔断值无关）；撞 429 自动对半降并发，成功后逐步爬回。') }}</span>
                    </div>
                    <div class="group-ops">
                      <NTag size="small" round :type="subagentLiveLimit === null ? 'default' : 'info'">
                        {{ subagentLiveLimit === null ? '尚未建立配额池' : `当前实际并发 ${subagentLiveLimit}` }}
                      </NTag>
                    </div>
                  </div>
                  <div style="display:flex; gap:22px; flex-wrap:wrap; align-items:flex-end; padding-top: 2px;">
                    <div class="f1" style="margin:0;">
                      <label>{{ t('最大并发请求数（1~16，同一模型+密钥共享）') }}</label>
                      <NInputNumber v-model:value="subagentDraft.max_parallel" :min="1" :max="16" size="small" style="width: 120px;" />
                    </div>
                    <div class="f1" style="margin:0;">
                      <label>{{ t('按 429 / Retry-After 自适应降速（厂商不返回限流头时也有效）') }}</label>
                      <NSwitch size="small" v-model:value="subagentDraft.adaptive" />
                    </div>
                  </div>
                </div>

                <div v-for="cat in filteredToolCategories" :key="cat.key" class="group-card">
                  <div class="group-header">
                    <div class="group-title-wrap">
                      <span class="group-icon"><i class="ms">{{ miIcon(cat.icon) || cat.icon }}</i></span>
                      <span class="group-title">{{ cat.label }}</span>
                      <span class="group-desc">{{ cat.description }}</span>
                    </div>
                    <div class="group-ops">
                      <NTag size="small" round :type="getGroupToolsCount(cat.tools).enabled === getGroupToolsCount(cat.tools).total ? 'success' : 'warning'">
                        已启用 {{ getGroupToolsCount(cat.tools).enabled }} / {{ getGroupToolsCount(cat.tools).total }}
                      </NTag>
                      <NButton size="tiny" quaternary @click="toggleToolsGroup(cat.tools, true)">{{ t('全选') }}</NButton>
                      <NButton size="tiny" quaternary @click="toggleToolsGroup(cat.tools, false)">{{ t('全关') }}</NButton>
                    </div>
                  </div>
                  <div class="group-items-grid">
                    <div v-for="t in getFilteredTools(cat.tools)" :key="t.name" class="tool-card-item">
                      <div class="tool-card-left">
                        <NSwitch v-if="t.name === 'finish'" size="small" :value="true" disabled />
                        <NSwitch v-else size="small" v-model:value="toolsDraft[t.name]" />
                        <span class="tool-name">{{ t.name }}</span>
                        <NTag size="tiny" :bordered="false" :type="getToolRiskTag(t.name).type" class="risk-tag">
                          {{ getToolRiskTag(t.name).label }}
                        </NTag>
                      </div>
                      <div class="tool-card-right">
                        <span class="tool-desc">{{ t.description }}</span>
                      </div>
                    </div>
                  </div>
                </div>

                <!-- 本地调试工具引导卡片 -->
                <div class="group-card guide-card" style="background: rgba(0, 128, 255, 0.04); border: 1px dashed rgba(0, 128, 255, 0.3);">
                  <div class="group-header" style="border-bottom: none; padding: 10px 14px;">
                    <div class="group-title-wrap">
                      <span class="group-icon"><i class="ms sm">explore</i></span>
                      <span class="group-title">{{ t('本地开发服务与浏览器自动化扩展') }}</span>
                      <span class="group-desc">{{ t('包含 dev_server_* 服务管理（3工具）、http_request 接口调试（1工具）与 Chromium 浏览器自动化（10工具）') }}</span>
                    </div>
                    <div class="group-ops">
                      <NButton size="small" type="primary" secondary @click="toolsTab = 'debug'">
                        进入本地调试配置 ({{ debugActiveCount }} 项启用) <i class="ms sm">arrow_forward</i>
                      </NButton>
                    </div>
                  </div>
                </div>
              </div>
              <div class="hint tool-foot" style="margin-top: 10px;">
                * finish 为任务结束的协议工具，始终可用；各工具的「变更·需审批」与「高危·需审批」属性受「权限模式与环境」配置约束。
              </div>
            </div>

            <!-- TAB 2: 终端命令白名单 (Terminal Commands Whitelist) -->
            <div v-show="toolsTab === 'commands'" class="tab-content">
              <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; gap:12px; flex-wrap:wrap;">
                <span class="hint">
                  按语言和技术生态细分管理默认放行的命令。关闭的命令从默认白名单移除（Agent 调 run_command 会被直接拦截）。
                </span>
                <div class="cmd-actions">
                  <NButton size="tiny" secondary @click="toggleAllCommands(true)">{{ t('全部放行') }}</NButton>
                  <NButton size="tiny" secondary @click="toggleAllCommands(false)">{{ t('全部禁用') }}</NButton>
                  <NInput v-model:value="cmdSearch" size="small" class="cmd-search"
                          :placeholder="t('过滤命令（如 pytest、npm）...')" clearable />
                </div>
              </div>

              <div class="cmd-groups-container">
                <div v-for="cat in filteredCommandCategories" :key="cat.key" class="group-card">
                  <div class="group-header">
                    <div class="group-title-wrap">
                      <span class="group-icon"><i class="ms">{{ miIcon(cat.icon) || cat.icon }}</i></span>
                      <span class="group-title">{{ cat.label }}</span>
                      <span class="group-desc">{{ cat.description }}</span>
                    </div>
                    <div class="group-ops">
                      <NTag size="small" round :type="getGroupCommandsCount(cat.commands).enabled === getGroupCommandsCount(cat.commands).total ? 'success' : 'warning'">
                        已放行 {{ getGroupCommandsCount(cat.commands).enabled }} / {{ getGroupCommandsCount(cat.commands).total }}
                      </NTag>
                      <NButton size="tiny" quaternary @click="toggleCommandsGroup(cat.commands, true)">{{ t('全选') }}</NButton>
                      <NButton size="tiny" quaternary @click="toggleCommandsGroup(cat.commands, false)">{{ t('全关') }}</NButton>
                    </div>
                  </div>
                  <div class="cmd-chips-grid">
                    <label v-for="c in getFilteredCommands(cat.commands)" :key="c" class="cmd-chip-card" :class="{ 'disabled-chip': commandsDraft[c] === false }">
                      <NSwitch size="small" v-model:value="commandsDraft[c]" />
                      <span class="cmd-chip-name">{{ c }}</span>
                    </label>
                  </div>
                </div>
              </div>
              <div class="hint tool-foot" style="margin-top: 10px;">
                项目设置对话框中的「命令白名单」仍可输入指定命令列表整表替换全局默认白名单。
              </div>
            </div>

            <!-- TAB 3: 本地调试与浏览器 (Local Debug & Browser) -->
            <div v-show="toolsTab === 'debug'" class="tab-content">
              <div class="dbg-switches-box">
                <div class="group-card" style="margin-bottom: 12px;">
                  <div class="group-header">
                    <div class="group-title-wrap">
                      <span class="group-icon"><i class="ms sm">rocket_launch</i></span>
                      <span class="group-title">{{ t('后台开发服务管理 (dev_server)') }}</span>
                      <span class="group-desc">{{ t('支持任务中后台常驻启动 dev server（自动端口与就绪等待），任务结束自动清理') }}</span>
                    </div>
                    <div class="group-ops">
                      <label class="checkline" style="gap:6px">
                        <span style="font-size:12px; font-weight:600">{{ t('服务组主开关：') }}</span>
                        <NSwitch size="small" v-model:value="devServerGroupEnabled" />
                      </label>
                    </div>
                  </div>
                  <div class="group-items-grid">
                    <div v-for="name in devServerTools" :key="name" class="tool-card-item">
                      <div class="tool-card-left">
                        <NSwitch size="small" v-model:value="toolsDraft[name]" />
                        <span class="tool-name">{{ name }}</span>
                        <NTag size="tiny" :bordered="false" :type="getToolRiskTag(name).type" class="risk-tag">
                          {{ getToolRiskTag(name).label }}
                        </NTag>
                      </div>
                      <div class="tool-card-right">
                        <span class="tool-desc">{{ getToolDesc(name) }}</span>
                      </div>
                    </div>
                  </div>
                </div>

                <div class="group-card" style="margin-bottom: 12px;">
                  <div class="group-header">
                    <div class="group-title-wrap">
                      <span class="group-icon"><i class="ms sm">language</i></span>
                      <span class="group-title">{{ t('本地 HTTP API 调试 (http_request)') }}</span>
                      <span class="group-desc">{{ t('支持对本地或内网开发服务发起 HTTP 请求，调试 REST/GraphQL 接口（受下方主机白名单约束）') }}</span>
                    </div>
                    <div class="group-ops">
                      <NSwitch size="small" v-model:value="toolsDraft['http_request']" />
                    </div>
                  </div>
                  <div class="tool-card-item" style="border: none;">
                    <div class="tool-card-left">
                      <span class="tool-name">http_request</span>
                      <NTag size="tiny" :bordered="false" :type="getToolRiskTag('http_request').type" class="risk-tag">
                        {{ getToolRiskTag('http_request').label }}
                      </NTag>
                    </div>
                    <div class="tool-card-right">
                      <span class="tool-desc">{{ getToolDesc('http_request') }}</span>
                    </div>
                  </div>
                </div>

                <div class="group-card" style="margin-bottom: 12px;">
                  <div class="group-header">
                    <div class="group-title-wrap">
                      <span class="group-icon"><i class="ms sm">explore</i></span>
                      <span class="group-title">{{ t('本地 Chromium 浏览器调试 (browser)') }}</span>
                      <span class="group-desc">{{ t('驱动本地 Chrome/Edge 浏览器，支持访问页面、点击输入、截图与监听控制台/网络日志') }}</span>
                    </div>
                    <div class="group-ops">
                      <label class="checkline" style="gap:6px">
                        <span style="font-size:12px; font-weight:600">{{ t('浏览器组主开关：') }}</span>
                        <NSwitch size="small" v-model:value="browserGroupEnabled" />
                      </label>
                    </div>
                  </div>
                  <div class="group-items-grid">
                    <div v-for="name in browserTools" :key="name" class="tool-card-item">
                      <div class="tool-card-left">
                        <NSwitch size="small" v-model:value="toolsDraft[name]" />
                        <span class="tool-name">{{ name }}</span>
                        <NTag size="tiny" :bordered="false" :type="getToolRiskTag(name).type" class="risk-tag">
                          {{ getToolRiskTag(name).label }}
                        </NTag>
                      </div>
                      <div class="tool-card-right">
                        <span class="tool-desc">{{ getToolDesc(name) }}</span>
                      </div>
                    </div>
                  </div>
                </div>

                <!-- 浏览器运行时与网络安全配置卡片 -->
                <div class="group-card">
                  <div class="group-header">
                    <div class="group-title-wrap">
                      <span class="group-icon"><i class="ms">settings</i></span>
                      <span class="group-title">{{ t('调试环境运行参数与安全限制') }}</span>
                      <span class="group-desc">{{ t('配置浏览器内核路径、窗口显示模式及内网穿透防护主机白名单') }}</span>
                    </div>
                  </div>
                  <div style="padding: 12px;">
                    <div class="frow">
                      <div class="f1"><label>{{ t('浏览器引擎') }}</label>
                        <NSelect v-model:value="localDebugDraft.browser" :options="[
                          { label: 'auto（优先 Chrome，无则回退 Edge）', value: 'auto' },
                          { label: 'Google Chrome', value: 'chrome' },
                          { label: 'Microsoft Edge', value: 'edge' },
                        ]" /></div>
                      <div class="f2"><label>{{ t('自定义浏览器路径（留空则自动探测系统安装）') }}</label>
                        <NInput v-model:value="localDebugDraft.browser_executable" placeholder="C:\...\chrome.exe" /></div>
                    </div>
                    <div class="frow" style="margin-top:8px;align-items:center">
                      <label class="cmd-chip">
                        <NSwitch size="small" v-model:value="localDebugDraft.headless" />
                        <span class="cmd-name">{{ t('无头模式运行（关闭时在桌面弹出浏览器窗口，可直观围观操作）') }}</span>
                      </label>
                      <NButton size="small" :loading="testingBrowser" @click="onTestBrowser">{{ t('测试浏览器探测') }}</NButton>
                    </div>
                    <div class="frow" style="margin-top:10px">
                      <div class="f2">
                        <label>{{ t('允许访问的主机白名单（每行一个，防内网穿透；默认仅限回环）') }}</label>
                        <NInput v-model:value="localDebugDraft.hosts_text" type="textarea" :rows="3"
                                placeholder="localhost&#10;127.0.0.1&#10;::1" />
                      </div>
                      <div class="f1">
                        <label>{{ t('导航超时 / 服务就绪超时（秒）') }}</label>
                        <div style="display:flex;gap:8px">
                          <NInputNumber v-model:value="localDebugDraft.navigation_timeout_seconds" :min="5" :max="300" :placeholder="t('导航')" />
                          <NInputNumber v-model:value="localDebugDraft.service_ready_timeout_seconds" :min="5" :max="600" :placeholder="t('就绪')" />
                        </div>
                      </div>
                    </div>
                    <div class="hint tool-foot" style="margin-top:8px; margin-bottom:0">
                      浏览器顶层导航与 http_request 仅放行上述白名单主机；云元数据等敏感网段强制拦截；项目级设置可额外追加局域网测试机。
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <!-- TAB 4: 权限模式与环境 (Permissions & Environment) -->
            <div v-show="toolsTab === 'perms'" class="tab-content">
              <div class="group-card" style="margin-bottom: 12px; padding: 12px;">
                <div style="font-size: 13px; font-weight: 600; margin-bottom: 8px; color: var(--text);">
                  <i class="ms sm">security</i> 敏感操作访问权限模式
                </div>
                <div class="frow">
                  <div class="f2"><label>{{ t('访问权限模式（敏感操作是否挂起等待看板批准）') }}</label>
                    <NSelect v-model:value="permDraft.mode" :options="permOptions" /></div>
                  <div class="f1"><label>{{ t('审批超时（分钟，1~1440，无人响应自动拒绝）') }}</label>
                    <NInputNumber v-model:value="permDraft.timeout_minutes" :min="1" :max="1440" /></div>
                </div>
                <div class="hint pwd-hint" style="margin-top: 6px;">
                  询问 = 任务挂起，顶栏出现「待审批」入口，批准/拒绝后继续；项目对话框可按项目覆盖模式。
                </div>
              </div>

              <div class="group-card" style="margin-bottom: 12px; padding: 12px;">
                <label class="checkline stack" style="margin: 0; padding: 0; border: none; background: none;">
                  <div style="display:flex;align-items:center;gap:8px">
                    <NSwitch v-model:value="gitCommitAndMergeDraft" />
                    <span style="font-weight: 600; font-size: 13px; color: var(--text);"><i class="ms sm">account_tree</i> {{ t('任务完成后自动提交并合并代码（Git 自动提交与合并）') }}</span>
                  </div>
                  <span class="hint" style="margin-top: 6px;">{{ t('默认关闭：Agent 直接在项目工作区修改代码，不自动产生 Git 提交与主分支合并，方便人工检查与自主提交；开启后在独立 worktree 分支完成开发并自动提交合并。') }}</span>
                </label>
              </div>

              <div class="group-card" style="padding: 12px;">
                <div style="font-size: 13px; font-weight: 600; margin-bottom: 8px; color: var(--text);">
                  <i class="ms sm">terminal</i> Agent 子进程环境变量与 PATH 引导
                </div>
                <label style="font-size: 12px; color: var(--dim); margin-bottom: 4px; display: block;">{{ t('自定义环境变量（代理 / 国内镜像，每行 KEY=VALUE）') }}</label>
                <NInput v-model:value="envDraft" type="textarea" :rows="3"
                        placeholder="每行一个 KEY=VALUE（也接受 KEY: VALUE），例如：&#10;GOPROXY=https://goproxy.cn,direct&#10;HTTP_PROXY=http://127.0.0.1:7890" />
                <div class="hint tool-foot" style="margin-top: 6px;">
                  注入 run_command 子进程：国内网络拉依赖失败时配 GOPROXY、npm registry（HTTP_PROXY）、PIP_INDEX_URL 即可；不允许覆盖 PATH/SYSTEMROOT 等系统变量。
                </div>
                <label class="cmd-chip" style="margin-top: 6px;" :title="t('写入前备份旧值到日志，只增不删，可手动改回')">
                  <NSwitch size="small" v-model:value="pathAutofix" />
                  <span class="cmd-name">{{ t('任务启动时自动把缺失的工具目录写入用户 PATH') }}</span>
                </label>
                <div class="hint tool-foot" style="margin-top: 6px;">
                  启动任务时扫描 node/npm/go/java 等常用命令：不在 PATH 的自动注入 Agent 会话并写进提示词；开启后还会把缺失目录持久写入用户 PATH。
                </div>
              </div>
            </div>

            <!-- 统一底栏保存按钮 -->
            <div class="sfoot" style="margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--line);">
              <NButton type="primary" :loading="toolsSaving" @click="saveTools">{{ t('保存工具与权限') }}</NButton>
            </div>
          </NCard>
        </div>
        <div v-if="settingsSection === 'mirrors'">
          <NCard size="small" class="scard">
            <template #header><i class="ms">sync_alt</i> {{ t('镜像源管理与测速选源') }}</template>
      <div class="mirror-ops">
        <NButton size="small" :loading="mirrorTesting" @click="testMirrors()">{{ t('测速全部源') }}</NButton>
        <NButton size="small" type="primary" :loading="mirrorApplying" @click="applyMirrors">
          应用最快源到环境变量</NButton>
        <span class="hint" v-if="mirrorTestedAt">上次测速：{{ mirrorTestedAtText }}</span>
        <span class="hint" v-else>{{ t('尚未测速（Agent 会按内置默认顺序下载）') }}</span>
      </div>
      <div class="hint tool-foot">
        测速 = HTTP 直连取首包（绕过系统代理，正是「无代理时」的真实速度）；结果存
        mirror_speeds.json，Agent 的 env-setup 技能会自动带上「最快源优先」清单。
        「应用最快源」把代理类（GOPROXY / pip index / npm registry）的冠军写进上方环境变量。
      </div>

      <div v-for="cat in mirrorCategories" :key="cat.key" class="mcat">
        <div class="mcat-head">
          <span class="mcat-name">{{ cat.label }}</span>
          <NTag size="tiny" :bordered="false" :type="cat.kind === 'proxy' ? 'info' : 'default'">
            <template v-if="cat.kind === 'proxy'"><i class="ms sm">arrow_forward</i> 可应用 {{ cat.env_var }}</template>
            <template v-else>{{ t('下载源') }}</template></NTag>
          <NButton size="tiny" quaternary :loading="mirrorTestingCat === cat.key"
                   :disabled="mirrorTesting" @click="testMirrors(cat.key)">{{ t('只测这类') }}</NButton>
        </div>
        <div v-for="row in mirrorRows(cat.key)" :key="row.url" class="mrow">
          <NSwitch size="small" :value="mirrorEnabled(row.url)"
                   @update:value="(v: boolean) => toggleMirror(row.url, v)" />
          <span class="murl">{{ row.url }}</span>
          <NTag v-if="row.builtin" size="tiny" :bordered="false">内置·{{ row.label }}</NTag>
          <NTag v-else size="tiny" :bordered="false" type="success">{{ t('自定义') }}</NTag>
          <NTag size="tiny" :bordered="false" :type="latencyTag(row.url).type">
            {{ latencyTag(row.url).text }}</NTag>
          <NButton v-if="!row.builtin" size="tiny" quaternary type="error"
                   @click="removeCustom(cat.key, row.url)">{{ t('删除') }}</NButton>
        </div>
      </div>

      <div class="madd">
        <NSelect v-model:value="addCat" size="small" :options="catOptions" class="madd-cat" />
        <NInput v-model:value="addUrl" size="small" class="madd-url"
                :placeholder="t('要添加的镜像 URL（http(s)://，需可直接访问；测速时会一并探测）')"
                @keyup.enter="addCustom" />
        <NButton size="small" @click="addCustom">{{ t('添加镜像源') }}</NButton>
      </div>
      <div class="hint tool-foot">
        添加/禁用需点下方「保存镜像源配置」生效；禁用的源不参与测速、也不会推荐给 Agent；
        自定义源若是内网镜像通常最快，测速后自动成为首选。
      </div>

      <div class="sfoot">
        <NButton type="primary" :loading="mirrorsSaving" :disabled="!mirrorsDirty"
                 @click="saveMirrors">{{ t('保存镜像源配置') }}</NButton>
      </div>
          </NCard>
        </div>
        <div v-if="settingsSection === 'egress'">
          <NCard size="small" class="scard">
            <template #header><i class="ms">lan</i> {{ t('出网白名单') }}</template>
      <div class="frow">
        <div class="f2"><label>{{ t('模式') }}</label>
          <NSelect v-model:value="draft.egress_mode" :options="[
            { label: 'allowlist — 只允许白名单主机（默认）', value: 'allowlist' },
            { label: 'off — 全部放行（仅排障用）', value: 'off' },
          ]" />
        </div>
        <div class="f1"><label>{{ t('拒绝时写审计') }}</label>
          <div style="display:flex;align-items:center;gap:8px">
            <NSwitch v-model:value="draft.egress_audit" />
            <span class="hint">logs/audit.jsonl · action=egress.deny</span>
          </div>
        </div>
      </div>
      <div class="frow">
        <div class="f1"><label>{{ t('额外放行主机（extra_hosts）') }}</label>
          <NDynamicTags v-model:value="draft.egress_extra" :max="30"
                        :placeholder="t('api.example.com（可带 :端口；*.example.com 含子域）')" />
          <div class="hint tool-foot">
            模型 / Issue / 镜像 / 更新源主机按各自配置自动放行，无需在此重复；
            网页抓取要访问的文档站需在此添加。回环地址始终放行；Agent 子进程
            （npm / pip / git）的出网不在应用层拦截范围。
          </div>
        </div>
      </div>
      <div class="hint tool-foot">
        已推导白名单（{{ (egressStatus?.derived_hosts || []).length }} 个）：
        {{ (egressStatus?.derived_hosts || []).join('、') || '（无）' }}
      </div>
      <div v-if="egressStatus" class="hint tool-foot">
        运行计数：放行 {{ egressStatus.allowed }} 次 / 拒绝 {{ egressStatus.denied }} 次
        <template v-if="egressStatus.last_denied.length">
          ；最近拒绝：{{ egressStatus.last_denied.map(d => `${d.host}（${d.purpose}，${d.ts}）`).join('；') }}
        </template>
        <template v-if="egressStatus.runtime_hosts.length">
          ；运行时放行（更新包 CDN）：{{ egressStatus.runtime_hosts.map(h => h.host).join('、') }}
        </template>
      </div>
      <div class="sfoot">
        <NButton type="primary" :loading="egressSaving" @click="saveEgress">{{ t('保存出网白名单') }}</NButton>
      </div>
          </NCard>
        </div>
        <div v-if="settingsSection === 'app'">
          <NCard size="small" class="scard">
            <template #header><i class="ms">settings</i> {{ t('应用') }}</template>
      <label class="checkline">
        <NSwitch :value="store.state.app_autostart.enabled" @update:value="toggleAutostart" />
        开机自动启动（写入当前用户注册表）
      </label>
      <label class="checkline" style="margin-top:10px">
        <NSwitch v-model:value="updDraft.auto_check" />
        启动时自动检查更新（Gitee Release 源）
      </label>
      <div class="frow">
        <div class="f2"><label>{{ t('更新源仓库 URL（Gitee，Release 附件放安装包）') }}</label>
          <NInput v-model:value="updDraft.repo" placeholder="https://gitee.com/xxx/orcsy" /></div>
        <div class="f1"><label>{{ t('访问 token（私有仓库用）') }}</label>
          <NInput v-model:value="updDraft.token" type="password" show-password-on="click"
                  :placeholder="updTokenConfigured
                    ? `已配置：${updTokenMasked}（留空保持不变）` : '可选'"
                  @update:value="updTokenTouched = true" /></div>
      </div>
      <div class="sfoot">
        <NButton :loading="updSaving" @click="saveUpdate">{{ t('保存更新配置') }}</NButton>
        <NButton :loading="updChecking" @click="checkUpdateNow">{{ t('检查更新') }}</NButton>
      </div>
      <div class="pkg-block">
        <div class="pkg-row">
          <div class="f2">
            <label>{{ t('手动导入本地安装包（orcsy-update-*.zip；同版本覆盖、升/降级均可）') }}</label>
            <NInput :value="localPkg" readonly :placeholder="t('尚未选择更新包文件')" />
          </div>
          <div class="f0 pkg-btns">
            <NButton @click="pickPackage">{{ t('选择安装包…') }}</NButton>
            <NButton type="primary" :loading="pkgInstalling" :disabled="!localPkg"
                     @click="installPackage">{{ t('安装更新包') }}</NButton>
          </div>
        </div>
        <div v-if="localPkg" class="pkg-meta">
          <NTag size="small" :bordered="false" :type="pkgTag.type">{{ pkgTag.text }}</NTag>
          <span class="hint">{{ pkgName }} · 安装全程静默无 UAC，完成后页面自动刷新；
            出错自动打开安装日志，无错误则不打开</span>
        </div>
      </div>
          </NCard>
        </div>
      </section>
    </div>
  </div>
</template>
<style scoped>
.settings { padding: 0 20px; max-width: 1180px; display: flex; flex-direction: column; gap: 14px; }
@media (max-width: 768px) { .settings { padding: 0 24px; } }
/* 工具区四大分类段式 Tab：4 个长标签+计数徽标在窄屏放不下——
   允许胶囊换行成两行两列，避免第三个/第四个标签被截断 */
.tools-tabs :deep(.n-tabs-rail) { flex-wrap: wrap; }
.tools-tabs :deep(.n-tabs-tab-wrapper) { min-width: 46%; }
.tools-tabs :deep(.n-tabs-tab) { justify-content: center; width: 100%; }
/* 左手风琴分区 + 右内容区：窄屏自动竖排 */
.mobile-menu { display: none; }
.layout { display: grid; grid-template-columns: 240px minmax(0, 1fr); gap: 14px; align-items: start; }
@media (max-width: 860px) {
  .layout { grid-template-columns: 1fr; }
  .menu { display: none; }
  /* 工具/命令分组：标题说明占满整行，操作区换到下一行，避免标签与开关
     挤在右列把说明压成竖条 */
  .group-header { align-items: flex-start; }
  .group-title-wrap { flex: 1 0 100%; }
  .group-ops { width: 100%; justify-content: flex-start; flex-wrap: wrap; }
  /* 四个工具分类 Tab 横排放不下带计数的长标签：窄屏一项一行 */
  .tools-tabs :deep(.n-tabs-tab-wrapper) { min-width: 100%; }
  .tools-tabs :deep(.n-tabs-tab) { justify-content: flex-start; white-space: normal; }
  .tools-tabs :deep(.n-tabs-tab__label) { white-space: normal; }
  /* 命令白名单操作行：按钮与过滤框换行，输入框占满整行 */
  .cmd-actions { width: 100%; flex-wrap: wrap; }
  .cmd-actions .cmd-search { width: 100%; }
  .mobile-menu { display: flex; flex-direction: column; gap: 8px;
    border: 1px solid var(--line); border-radius: 10px; padding: 10px;
    background: var(--card); }
  .mchips { display: flex; gap: 6px; overflow-x: auto; padding-bottom: 2px; }
  .mchip { flex: none; border: 1px solid var(--line); border-radius: 999px;
    padding: 6px 12px; background: none; color: var(--dim); font: inherit;
    font-size: 12.5px; white-space: nowrap; cursor: pointer; }
  .mchip.active { border-color: var(--acc, #5b8cff);
    color: var(--acc, #5b8cff); background: rgba(91, 140, 255, 0.12); }
}
.menu { position: sticky; top: 14px; border: 1px solid var(--line);
  border-radius: 10px; padding: 6px; background: var(--card); }
.mgroup + .mgroup { border-top: 1px solid var(--line); }
.mgroup-head { width: 100%; display: flex; align-items: center; gap: 8px;
  padding: 10px 8px; border: 0; border-radius: 6px; background: none;
  color: var(--text); font: inherit; font-size: 13.5px; font-weight: 600;
  text-align: left; cursor: pointer; }
.mgroup-head:hover { background: rgba(128, 140, 180, 0.12); }
.mgroup-head.open { color: var(--acc, #5b8cff); }
.mgroup-head .ms { font-size: 18px; }
.mgroup-label { flex: 1; }
.mchev { color: var(--faint); font-size: 18px; }
.mchildren { display: flex; flex-direction: column; gap: 2px;
  padding: 0 4px 6px 20px; }
.mchild { display: flex; align-items: center; gap: 7px; width: 100%;
  padding: 7px 8px; border: 0; border-radius: 6px; background: none;
  color: var(--dim); font: inherit; font-size: 12.8px; text-align: left;
  cursor: pointer; }
.mchild:hover { background: rgba(128, 140, 180, 0.12); color: var(--text); }
.mchild.active { background: rgba(91, 140, 255, 0.14);
  color: var(--acc, #5b8cff); font-weight: 600; }
.mchild .ms { font-size: 16px; }
.content { min-width: 0; }
.content .scard { margin-bottom: 0; }
/* 卡片标题过长（如 Team Profiles）时换行，窄屏不撑破卡片 */
.content .scard :deep(.n-card-header__main) {
  flex-wrap: wrap; line-height: 1.35; word-break: break-word;
}
/* 窄屏防网格/弹性子项被长标题或输入框撑破 */
.layout, .layout > *, .mobile-menu, .mchips,
.content > div, .content .scard, .content .group-card,
.frow > div, .duo > div { min-width: 0; }
.modelspec { font-size: 12px; color: var(--dim); margin: -4px 0 8px; }
.hint { font-size: 12px; color: var(--faint); font-weight: 400; }
.frow { display: flex; gap: 10px; margin-bottom: 10px; }
.frow > div { display: flex; flex-direction: column; gap: 4px; }
.pathline { display: flex; gap: 8px; align-items: center; }
.migbox { border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px;
  margin-bottom: 10px; background: rgba(91, 140, 255, 0.06); }
.budget-today { border: 1px solid var(--line); border-radius: 8px; padding: 6px 10px;
  max-height: 220px; overflow: auto; margin-bottom: 8px; }
.budget-row { display: flex; gap: 14px; justify-content: space-between;
  font-size: 12.5px; padding: 4px 2px; border-bottom: 1px dashed var(--line); }
.budget-row:last-child { border-bottom: none; }
.budget-name { flex: 1; color: var(--text); overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.audit-list { border: 1px solid var(--line); border-radius: 8px; padding: 4px 10px;
  max-height: 460px; overflow: auto; }
.audit-row { display: flex; gap: 10px; align-items: center; font-size: 12px;
  padding: 5px 2px; border-bottom: 1px dashed var(--line); }
.audit-row:last-child { border-bottom: none; }
.audit-time { color: var(--faint); font-family: Consolas, monospace; flex: none; }
.audit-action { color: #6f9dff; font-family: Consolas, monospace; min-width: 130px; }
.audit-actor { color: var(--dim); min-width: 150px; }
.audit-target { flex: 1; color: var(--text); overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.frow label, .checkline { font-size: 12.5px; color: var(--dim); }
.f1 { flex: 1; } .f14 { flex: 1.4; } .f2 { flex: 2; } .f0 { flex: none; }
.btnrow { display: flex; gap: 8px; }
.sfoot { display: flex; justify-content: flex-end; }
.pkg-block { margin-top: 12px; border-top: 1px solid var(--line); padding-top: 12px; }
.pkg-row { display: flex; gap: 10px; align-items: flex-end; }
.pkg-row > div { display: flex; flex-direction: column; gap: 4px; }
.pkg-btns { flex-direction: row !important; gap: 8px; padding-bottom: 1px; }
.pkg-meta { display: flex; gap: 8px; align-items: center; margin-top: 8px; flex-wrap: wrap; }
.pwd-btns { align-self: flex-end; padding-bottom: 1px; }
.pwd-hint { margin: -4px 0 8px; }
.tools-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(420px, 1fr));
  gap: 6px 18px; margin-bottom: 8px; }
.subhead { display: block; font-size: 13px; font-weight: 600; color: var(--text);
  margin: 14px 0 8px; padding-top: 12px; border-top: 1px solid var(--line); }
/* 命令白名单：横向流式胶囊（开关+命令名），放不下自动换行 */
.cmds-flow { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-bottom: 8px; }
.cmd-chip { display: inline-flex; align-items: center; gap: 6px;
  border: 1px solid var(--line); border-radius: 999px; padding: 3px 12px 3px 8px;
  background: var(--card); }
.cmd-name { font-family: Consolas, monospace; font-size: 12.5px; color: var(--text); }
.tool-line { align-items: baseline; }
.tool-name { font-family: Consolas, monospace; font-size: 12.5px; color: var(--text);
  flex: none; min-width: 92px; }
.tool-desc { flex: 1; min-width: 0; }
.tool-foot { margin-bottom: 10px; }
@media (max-width: 900px) { .tools-grid { grid-template-columns: 1fr; } }
.checkline { display: flex; gap: 10px; align-items: center; }
/* 纵排开关行（如审查配置的两个开关）：hint 折行后行间不贴死 */
.stack { margin-top: 12px; }
.stack .hint { flex: 1; min-width: 0; }
.prow { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; margin-bottom: 8px; }
.prow-head { display: flex; gap: 6px; align-items: center; }
.prow-meta { font-size: 12px; color: var(--dim); margin: 4px 0; word-break: break-all; }
.ops-row { display: flex; gap: 6px; }
.empty { color: var(--faint); font-size: 13px; text-align: center; padding: 18px 0; }
/* 镜像源管理：操作行 + 类别分组 + 源行（开关/URL/来源/延迟徽标） */
.mirror-ops { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }
.mcat { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; margin-bottom: 8px; }
.mcat-head { display: flex; gap: 8px; align-items: center; margin-bottom: 4px; }
.mcat-name { font-size: 13px; font-weight: 600; color: var(--text); }
.mrow { display: flex; gap: 8px; align-items: center; padding: 2px 0; }
.murl { font-family: Consolas, monospace; font-size: 12px; color: var(--dim);
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.madd { display: flex; gap: 8px; align-items: center; margin: 4px 0 8px; }
.madd-cat { width: 190px; flex: none; }
.madd-url { flex: 1; }

/* ---- Agent 工具与命令白名单三级分组样式 ---- */
.tab-content { padding-top: 4px; }
.group-card {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--card);
  margin-bottom: 12px;
  overflow: hidden;
}
/* 卡片内容行（头部以外的直接子元素）与卡片边缘留出间距：
   头部自带 12px 内边距，正文行此前 0 内边距会贴死卡片左右边框 */
.group-card > div:not(.group-header) { padding: 4px 12px 10px; }
.group-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 12px;
  background: rgba(128, 128, 128, 0.05);
  border-bottom: 1px solid var(--line);
  gap: 10px;
  flex-wrap: wrap;
}
.group-title-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;
  flex-wrap: wrap;
}
.group-icon {
  font-size: 15px;
}
.group-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}
.group-desc {
  font-size: 12px;
  color: var(--dim);
}
.group-ops {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
.group-items-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(460px, 1fr));
  gap: 1px;
  background: var(--line);
}
.tool-card-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  background: var(--card);
}
.tool-card-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.tool-card-right {
  flex: 1;
  min-width: 0;
  text-align: right;
}
/* 窄屏：工具行改上下排列，描述占满整行（否则 460px 网格 + 右侧窄列会把
   文字挤成一列字，行高被拉得很长） */
@media (max-width: 860px) {
  .group-items-grid { grid-template-columns: 1fr; }
  .tool-card-item { flex-direction: column; align-items: stretch; gap: 5px; }
  .tool-card-left { flex-wrap: wrap; }
  .tool-card-right { text-align: left; }
  .tool-card-right .tool-desc { display: block; line-height: 1.55; }
}
.risk-tag {
  font-size: 11px;
  padding: 0 4px;
}
.cmd-chips-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 10px 12px;
}
.cmd-actions { display: flex; gap: 8px; align-items: center; }
.cmd-search { width: 220px; }
.cmd-chip-card {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 3px 10px;
  background: var(--bg);
  cursor: pointer;
  transition: all 0.2s ease;
}
.cmd-chip-card:hover {
  border-color: rgba(0, 128, 255, 0.4);
}
.cmd-chip-card.disabled-chip {
  opacity: 0.6;
  background: rgba(128, 128, 128, 0.05);
}
.cmd-chip-name {
  font-family: Consolas, monospace;
  font-size: 12px;
  color: var(--text);
}
@media (max-width: 900px) {
  .group-items-grid {
    grid-template-columns: 1fr;
  }
}
</style>
