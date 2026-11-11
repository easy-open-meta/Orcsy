<script setup lang="ts">
import { NCollapse, NCollapseItem, NButton, NCard, NInput, NInputNumber, NSelect, NSwitch, NTag, NTabs, NTabPane } from 'naive-ui'
import { computed, reactive, ref, shallowRef, watch } from 'vue'
import { api, type MirrorRowT, type ModelInfo } from '../api'
import { beginRestartWait, openDirBrowser, settingsSection, store } from '../store'
import { cmpVersion, fmtTokens } from '../utils'

const message = () => window.$message

// ---- 功能分区手风琴：左侧分区菜单，右侧只渲染当前分区（懒加载，未点开不挂载） ----
// key 与 settingsSection 联动（跨视图跳转定位，如看板「设置」按钮直达对应分区）
const SECTIONS = [
  { key: 'llm', label: '🤖 大模型接入' },
  { key: 'profiles', label: '👥 多模型项目小组 (Team)' },
  { key: 'sched', label: '⏱ 调度与看板' },
  { key: 'notify', label: '🔔 提醒设置' },
  { key: 'review', label: '🔍 任务审查（Review Agent）' },
  { key: 'tools', label: '🛠 Agent 工具与访问权限' },
  { key: 'mirrors', label: '🪞 镜像源管理与测速选源' },
  { key: 'app', label: '⚙️ 应用' },
] as const
const activeSection = computed(() => settingsSection.value)
function onSectionChange(v: string | number | (string | number)[] | null): void {
  // NCollapse 单开形态回调数组/单值/null（再点已展开项收起）；取第一个有效 key。
  // 收起（null）保持当前分区：右侧必须始终有内容可看
  const k = Array.isArray(v) ? v[0] : v
  if (k) settingsSection.value = String(k)
}
const sectionLabel = computed(() =>
  SECTIONS.find(s => s.key === settingsSection.value)?.label ?? '')

// ---- 草稿模式：首份数据到后回填一次，之后轮询不覆盖未保存的输入 ----
const draft = reactive({
  provider: 'openai' as 'openai' | 'anthropic',
  model: '', base_url: '', api_key: '',
  temperature: 0.2, max_tokens: 8192, max_context_tokens: 131072,
  enable_thinking: false, timeout_seconds: 300,
  interval: 300, parallel: 1, port: 5116, retention: 7, max_total_mb: 200,
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
    label: '文件操作与编辑',
    icon: '📁',
    description: '读取、创建、编辑、重命名及删除工作区代码和文本文件',
    tools: ['read_file', 'write_file', 'edit_file', 'multi_edit', 'delete_file', 'move_file', 'list_dir'],
  },
  {
    key: 'code_search',
    label: '代码搜索与定位',
    icon: '🔍',
    description: '按通配符模式查找文件以及跨文件正则搜索内容',
    tools: ['find_files', 'grep'],
  },
  {
    key: 'command_exec',
    label: '终端命令执行',
    icon: '💻',
    description: '在项目根目录执行白名单内的构建、测试、检查与系统命令',
    tools: ['run_command'],
  },
  {
    key: 'task_collab',
    label: '任务管理与协议',
    icon: '⚡',
    description: '拆解多阶段子任务并在任务完成或受阻时给出结构化答复',
    tools: ['create_tasks', 'finish'],
  },
  {
    key: 'subagent',
    label: '子代理并发协同',
    icon: '👥',
    description: '派生只读子代理并行探索多个模块/目录，单轮拿到汇总结论',
    tools: ['delegate_explore'],
  },
  {
    key: 'external_net',
    label: '网络与外部知识',
    icon: '🌐',
    description: '查阅专业技能库文档、抓取公网页面正文与镜像源延迟测速',
    tools: ['read_skill', 'web_fetch', 'speedtest_mirrors'],
  },
]

const defaultCommandCategories = [
  {
    key: 'python',
    label: 'Python 生态',
    icon: '🐍',
    description: 'Python 解释器、包管理与测试/Lint 工具',
    commands: ['python*', 'py', 'pip*', 'pytest', 'uv', 'uvx', 'poetry', 'tox', 'ruff', 'mypy', 'black', 'flake8'],
  },
  {
    key: 'node',
    label: 'Node / 前端',
    icon: '📦',
    description: 'Node 运行时、包管理器与前端构建/测试工具',
    commands: ['node', 'npm', 'npx', 'pnpm', 'yarn', 'bun', 'tsc', 'eslint', 'prettier', 'vitest', 'jest'],
  },
  {
    key: 'compiled',
    label: 'Rust / Go / .NET / Java',
    icon: '⚙️',
    description: '系统级与强类型后端编译型语言工具链',
    commands: ['cargo', 'go', 'dotnet', 'gradle*', 'mvn', 'java', 'javac'],
  },
  {
    key: 'cpp',
    label: 'C / C++ 构建',
    icon: '🔨',
    description: 'C/C++ 编译器、构建工具与测试套件',
    commands: ['make', 'cmake', 'ctest', 'ninja', 'gcc', 'g++', 'clang', 'clang++', 'msbuild'],
  },
  {
    key: 'vcs',
    label: '版本控制',
    icon: '🌿',
    description: 'Git 代码仓库与分支版本管理',
    commands: ['git'],
  },
  {
    key: 'package_managers',
    label: '包管理器',
    icon: '📥',
    description: '系统级包管理器（缺工具链时按需环境引导，受审批约束）',
    commands: ['winget*', 'choco', 'scoop', 'brew', 'apt*', 'apt-get', 'dnf', 'nvm'],
  },
  {
    key: 'system_shell',
    label: '系统 Shell / 实用工具',
    icon: '💻',
    description: '系统 Shell、网络下载与归档解压',
    commands: ['powershell', 'pwsh', 'curl', 'tar', 'setx'],
  },
  {
    key: 'windows_probe',
    label: 'Windows 探测与排障',
    icon: '🪟',
    description: 'Windows 环境排障、进程查看与文件探测（只读/信息性）',
    commands: ['where', 'cmd', 'dir', 'type', 'echo', 'set', 'tasklist', 'systeminfo', 'findstr', 'more', 'ver', 'hostname', 'whoami'],
  },
  {
    key: 'posix_probe',
    label: 'POSIX 只读探测与统计',
    icon: '🐧',
    description: '跨平台/Unix 常用只读与文本统计命令（只读无害）',
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
      message()?.(res.message || res.error || '测试失败', 'error')
    }
  } catch (e: any) {
    message()?.(e.message, 'error')
  } finally {
    testingBrowser.value = false
  }
}
// ---- 访问权限草稿（全局默认；项目可在对话框里逐个覆盖） ----
const permOptions = [
  { label: '变更前询问（发生变更时询问：写/改/删/移文件、执行命令都挂起等批准）', value: 'ask_on_change' },
  { label: '自主模式（有风险时询问：仅 delete_file / move_file / run_command）', value: 'autonomous' },
  { label: '完全访问（减少确认次数：一律不询问，与历史行为一致）', value: 'full_access' },
]
const permDraft = reactive({ mode: 'full_access' as string, timeout_minutes: 30 })
// ---- Git 提交与合并开关（默认关闭：改动直接保留在工作区） ----
const gitCommitAndMergeDraft = shallowRef(false)
// ---- 任务结束提醒草稿（完成/受阻时提示音 + 系统通知；cancelled 不提醒） ----
const notifyDraft = reactive({ sound: true, system: true })
const notifySaving = shallowRef(false)
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
  notifyDraft.sound = s.notify_cfg?.sound ?? true
  notifyDraft.system = s.notify_cfg?.system ?? true
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
  syncProfilesDraft(s.agent_profiles)
})

// ---- 全局 Agent 角色预设池草稿 ----
const profilesDraft = ref<Record<string, any>>({})
const profilesTouchedKeys = reactive<Record<string, boolean>>({})
const profilesSaving = shallowRef(false)
const editingProfileId = shallowRef<string | null>(null)
const profileTestingId = shallowRef<string | null>(null)

const roleSelectOptions = [
  { label: '👑 项目经理 (PM)', value: 'pm' },
  { label: '⚙️ 后端开发工程师', value: 'backend' },
  { label: '🎨 前端开发工程师', value: 'frontend' },
  { label: '🧪 测试工程师 (QA)', value: 'qa' },
  { label: '🧩 自定义专职角色', value: 'custom' },
]

function getRoleIcon(role: string): string {
  const map: Record<string, string> = { pm: '👑', backend: '⚙️', frontend: '🎨', qa: '🧪', custom: '🧩' }
  return map[role] || '🤖'
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
    message()?.('未找到该角色的内置模版', 'warning')
    return
  }
  prof.extra_prompt = tpl
  message()?.(`已为「${prof.name || prof.role}」载入内置职责规范模版`, 'success')
}

function syncProfilesDraft(profs: Record<string, any> | undefined): void {
  const defaults: Record<string, any> = {
    pm_default: {
      id: 'pm_default', name: '项目经理 PM', role: 'pm',
      description: '负责需求分析、子任务拆分与前后端依赖编排',
      max_turns: 20, produces_code: false, allow_test_scripts: false,
      extra_prompt: getRoleTemplate('pm'),
      llm: { provider: '', model: '', base_url: '', api_key: '', api_key_masked: '', api_key_configured: false },
    },
    backend_default: {
      id: 'backend_default', name: '后端开发', role: 'backend',
      description: '负责后端服务架构、接口路由与业务逻辑实现',
      max_turns: 40, produces_code: true, allow_test_scripts: false,
      extra_prompt: getRoleTemplate('backend'),
      llm: { provider: '', model: '', base_url: '', api_key: '', api_key_masked: '', api_key_configured: false },
    },
    frontend_default: {
      id: 'frontend_default', name: '前端开发', role: 'frontend',
      description: '负责前端页面组件、状态管理与接口联调',
      max_turns: 40, produces_code: true, allow_test_scripts: false,
      extra_prompt: getRoleTemplate('frontend'),
      llm: { provider: '', model: '', base_url: '', api_key: '', api_key_masked: '', api_key_configured: false },
    },
    qa_default: {
      id: 'qa_default', name: '测试工程师', role: 'qa',
      description: '负责前后端联调测试、新建 Bug 任务与回归验收',
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
    name: '新建专职 Agent',
    role: 'custom',
    description: '专职自定义能力 Agent',
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
    message()?.(`[${prof.name}] 已获取 ${r.models.length} 个模型，可在下拉框中选择`, 'success')
  } catch (e: any) {
    message()?.(`[${prof.name}] 获取模型列表失败：${e.message}`, 'error')
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
    message()?.('该预设未指定模型名（将继承全局模型）', 'warning')
  }
  profileTestingId.value = pid
  try {
    const res = await api.testModel(eff)
    message()?.(`[${prof.name}] ${res.message || '连接成功'}`, 'success')
  } catch (e: any) {
    message()?.(`[${prof.name}] 连接测试失败：${e.message}`, 'error')
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
    message()?.('Agent 角色预设池已保存并生效')
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
    message()?.('已保存并生效')
    draft.api_key = ''
    apiKeyTouched.value = false
  } catch (e: any) { message()?.(e.message, 'error') }
  void import('../store').then(m => m.refresh(true))
}

async function saveNotify(): Promise<void> {
  notifySaving.value = true
  try {
    await api.config({ notify: { sound: notifyDraft.sound, system: notifyDraft.system } })
    message()?.('已保存并生效（下个任务结束即按新开关提醒）')
  } catch (e: any) { message()?.(e.message, 'error') }
  finally { notifySaving.value = false }
  void import('../store').then(m => m.refresh(true))
}

// ---- 审查 Agent 保存：模型三态 + 未配置模型时清除绑定（后端 null=审查未启用） ----
async function saveReview(): Promise<void> {
  reviewSaving.value = true
  try {
    const hasModel = reviewDraft.model.trim().length > 0
    if (hasModel && effTaskModel().value && reviewDraft.model.trim().toLowerCase() === effTaskModel().value.trim().toLowerCase()) {
      message()?.('审查模型不能与任务模型相同（' + effTaskModel().value + '）——请换一个独立模型', 'error')
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
    if (!hasModel) message()?.('已保存：审查未启用（未配置审查模型）')
    else message()?.('已保存：审查门已对全部项目默认生效（项目级可单独关闭）')
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
    message()?.(`已获取 ${r.models.length} 个模型`)
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
    if (authPwdClear.value) message()?.('已保存：看板登录鉴权已关闭', 'success')
    else if (authPwdTouched.value) message()?.('已保存：登录密码已更新，旧会话已失效，请重新登录', 'success')
    else message()?.('已保存（端口下次启动生效；日志归档清理立即按新天数执行）')
    authPwd.value = ''
    authPwdTouched.value = false
    authPwdClear.value = false
  } catch (e: any) { message()?.(e.message, 'error') }
  void import('../store').then(m => m.refresh(true))
}

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
    message()?.('开机自启动已' + (enabled ? '开启' : '关闭'))
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
      message()?.(`以下行无法解析为 KEY=VALUE，已跳过：${skipped.join('；')}`, 'warning')
    }
    await api.config({
      tools: toolsPayload,
      commands: cmdsOff,
      env: { vars: envVars, path_autofix: pathAutofix.value },
      permissions: { mode: permDraft.mode, timeout_minutes: permDraft.timeout_minutes },
      git: { commit_and_merge: gitCommitAndMergeDraft.value },
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
    message()?.('工具 / 命令白名单 / 访问权限已保存并生效')
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
    .map(u => ({ url: u, label: '用户添加', builtin: false, enabled: true, test_url: u }))
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
    message()?.('请填写 http(s):// 开头、可直接访问的镜像 URL', 'error')
    return
  }
  const list = mirrorCustom.value[addCat.value] ?? []
  if (list.includes(url)) { message()?.('该源已添加过', 'warning'); return }
  mirrorCustom.value = { ...mirrorCustom.value, [addCat.value]: [...list, url] }
  addUrl.value = ''
  message()?.('已添加，点「保存镜像源配置」后生效（Agent 测速时会一并探测）')
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
    message()?.('镜像源配置已保存并生效')
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
    message()?.(r.message || '测速完成', 'success')
  } catch (e: any) { message()?.(e.message, 'error') }
  finally { mirrorTesting.value = false; mirrorTestingCat.value = null }
}

async function applyMirrors(): Promise<void> {
  mirrorApplying.value = true
  try {
    const r = await api.mirrorApply()
    message()?.(r.message || '已应用最快源', 'success')
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
    message()?.('更新配置已保存')
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
    if (r.phase === 'available') message()?.(`发现新版本 v${r.latest}，可点顶部「更新到 ${r.latest}」安装`, 'success')
    else message()?.(`已是最新版本（v${r.current}）`, 'success')
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
    title: target ? `安装更新包 v${target}` : '安装更新包（版本未知）',
    content: `目标文件：${localPkg.value}\n\n注意：若正在执行任务会被中断。安装在后台静默进行`
      + '（全程无 UAC 弹窗，装回程序当前目录），完成后应用自动重启，本页面自动刷新。'
      + '升级过程中出错会写安装日志并自动打开日志文档；全程无错误则不打开日志。',
    positiveText: '安装更新包',
    negativeText: '取消',
    onPositiveClick() {
      pkgInstalling.value = true
      api.updateInstall(localPkg.value)
        .then(() => {
          // 挂目标版本（未知按当前版本核对）；遮罩立即出现，应用回归后自动刷新
          beginRestartWait(target || store.state?.version || '')
          message()?.('正在静默升级，应用将短暂离线并自动重启', 'success')
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
    message()?.(`已获取 ${r.models.length} 个模型：在「模型」框下拉选择，规格自动填充`)
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
    message()?.(r.message || '连接成功', 'success')
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
    message()?.('已按该模型的规格自动填充上下文窗口与最大输出（可手改）')
}

const providerOptions = [
  { label: 'openai（兼容协议）', value: 'openai' },
  { label: 'anthropic', value: 'anthropic' },
]
</script>

<template>
  <div class="settings" v-if="store.state">
    <div class="layout">
      <!-- 左：功能分区手风琴（单开；key 与 settingsSection 联动支持跨页定位） -->
      <aside class="menu">
        <NCollapse :expanded-names="activeSection" :accordion="true" @update:expanded-names="onSectionChange">
          <NCollapseItem name="llm" :title="'🤖 大模型接入'">
            <div class="menu-desc" v-if="activeSection === 'llm'">OpenAI 兼容协议覆盖 DeepSeek / GLM / Kimi / 通义 / Ollama 等；保存后立即生效。</div>
          </NCollapseItem>
          <NCollapseItem name="profiles" :title="'👥 多模型项目小组 (Team)'">
            <div class="menu-desc" v-if="activeSection === 'profiles'">配置 PM / 后端 / 前端 / QA 角色专属大模型与 API Key 预设池，供各项目 Team 引用。</div>
          </NCollapseItem>
          <NCollapseItem name="sched" :title="'⏱ 调度与看板'">
            <div class="menu-desc" v-if="activeSection === 'sched'">扫描间隔、并行数保存后立即生效；看板端口下次启动生效。</div>
          </NCollapseItem>
          <NCollapseItem name="notify" :title="'🔔 提醒设置'">
            <div class="menu-desc" v-if="activeSection === 'notify'">任务完成/受阻时提醒；手动停止不打扰。保存后立即生效。</div>
          </NCollapseItem>
          <NCollapseItem name="review" :title="'🔍 任务审查（Review Agent）'">
            <div class="menu-desc" v-if="activeSection === 'review'">独立模型合并前审查任务成果；未配置审查模型时审查未启用。</div>
          </NCollapseItem>
          <NCollapseItem name="tools" :title="'🛠 Agent 工具与访问权限'">
            <div class="menu-desc" v-if="activeSection === 'tools'">全局工具开关、命令白名单与访问权限模式；项目未覆盖时按这里生效。</div>
          </NCollapseItem>
          <NCollapseItem name="mirrors" :title="'🪞 镜像源管理与测速选源'">
            <div class="menu-desc" v-if="activeSection === 'mirrors'">Agent 装环境/拉依赖前测速，哪个快用哪个。</div>
          </NCollapseItem>
          <NCollapseItem name="app" :title="'⚙️ 应用'">
            <div class="menu-desc" v-if="activeSection === 'app'">更新源、自更新检查、看板登录密码与开机自启动。</div>
          </NCollapseItem>
        </NCollapse>
      </aside>
      <!-- 右：当前分区内容（v-if 懒加载，未选中不挂载不轮询开销） -->
      <section class="content">
        <div v-if="settingsSection === 'llm'">
          <NCard title="🤖 大模型接入" size="small" class="scard">
      <div class="frow">
        <div class="f1"><label>协议 provider</label>
          <NSelect v-model:value="draft.provider" :options="providerOptions" /></div>
        <div class="f2"><label>模型 model（下拉选择或输入过滤，支持自定义 id）</label>
          <NSelect v-model:value="draft.model" :options="modelOptions" filterable tag
                   placeholder="deepseek-chat 或获取列表后选择" @update:value="onModelPick" /></div>
        <div class="f0"><label>&nbsp;</label>
          <div class="btnrow">
            <NButton :loading="fetching" @click="fetchModels">获取列表</NButton>
            <NButton :loading="testing" title="发送一条极短对话，验证网络/密钥/模型名是否可用" @click="testModel">测试连接</NButton>
          </div></div>
      </div>
      <div class="modelspec" v-if="selectedSpec && (selectedSpec.context_window || selectedSpec.max_output_tokens)">
        该模型规格：上下文 {{ fmtTokens(selectedSpec.context_window) || '未知' }} ·
        最大输出 {{ fmtTokens(selectedSpec.max_output_tokens) || '未知' }}（已自动填充到下方，可手改）
      </div>
      <div class="frow">
        <div class="f2"><label>API 地址 base_url</label><NInput v-model:value="draft.base_url" :placeholder="draft.provider === 'anthropic' ? 'https://api.anthropic.com（自动补 /v1）' : 'https://api.deepseek.com/v1'" /></div>
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
        <div class="f1"><label>采样温度 temperature</label><NInputNumber v-model:value="draft.temperature" :step="0.1" :min="0" :max="2" /></div>
        <div class="f1"><label>最大输出 max_tokens（拉取过模型列表时，上限跟随模型上报值）</label><NInputNumber v-model:value="draft.max_tokens" :step="256" :min="256" :max="selectedSpec?.max_output_tokens || 200000" /></div>
      </div>
      <div class="frow">
        <div class="f1"><label>上下文窗口 max_context_tokens（上限同样跟随模型上报值）</label><NInputNumber v-model:value="draft.max_context_tokens" :step="1024" :min="4096" :max="selectedSpec?.context_window || 2000000" /></div>
        <div class="f1"><label>请求超时（秒，-1 = 不超时，挂起时可用看板「强制停止」打断）</label><NInputNumber v-model:value="draft.timeout_seconds" :min="-1" :max="3600" /></div>
      </div>
      <label class="checkline" style="margin-bottom: 10px">
        <NSwitch v-model:value="draft.enable_thinking" />
        请求侧开启模型思考
        <span class="hint">默认关闭：DeepSeek-R1 等推理模型会自动返回思考内容，无需开启。anthropic 需此项才会返回 thinking 块（预留一半 max_tokens 作思考预算）；OpenAI 兼容服务按 Qwen（enable_thinking）/ GLM（thinking）约定传参，不支持的服务请勿开启。</span>
      </label>
      <div class="sfoot">
        <NButton type="primary" @click="saveLlm">保存模型配置</NButton>
      </div>
          </NCard>
        </div>
        <div v-if="settingsSection === 'profiles'">
          <NCard title="👥 多模型 Agent 项目小组与角色预设池 (Team Profiles)" size="small" class="scard">
            <div class="team-banner" style="background: rgba(91, 140, 255, 0.08); border: 1px solid rgba(91, 140, 255, 0.25); border-radius: 8px; padding: 10px 14px; margin-bottom: 14px; font-size: 12.5px; line-height: 1.6; color: var(--text);">
              <div style="font-weight: 600; color: #5b8cff; margin-bottom: 4px;">💡 多模型 Agent 项目小组模式 (Team) 配置说明</div>
              <div>
                系统采用<b>「全局角色预设池 + 项目小队自由进驻」</b>的双层架构：
                <ul style="margin: 4px 0 0 16px; padding: 0;">
                  <li><b>全局层（此处）</b>：统一配置 <b>项目经理 (PM)</b>、<b>后端工程师</b>、<b>前端工程师</b>、<b>测试工程师 (QA)</b> 各自绑定的专属大模型（如 DeepSeek-R1、Claude 3.7、GPT-4o）与独立 API 密钥，一次配置后全系统共享；</li>
                  <li><b>项目层进驻</b>：每个项目可独立决定是否启用小组模式。在「项目」页或「看板」卡片点击<b>「设置」→「👥 多模型 Agent 项目小组模式 (Team)」</b>即可一键勾选启用小队进驻并自由调整成员名单。</li>
                </ul>
              </div>
            </div>
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; gap:8px; flex-wrap:wrap;">
              <span class="hint">配置不同专业职能角色的专属大模型与能力边界，进驻任意项目时可自由选用或局部微调。</span>
              <NButton size="small" type="primary" secondary @click="addProfile">+ 新建角色预设</NButton>
            </div>

            <div class="profiles-container">
              <div v-for="(prof, pid) in profilesDraft" :key="pid" class="group-card profile-card" :class="{ 'active-card': editingProfileId === pid }">
                <div class="group-header" style="cursor: pointer" @click="editingProfileId = editingProfileId === pid ? null : pid">
                  <div class="group-title-wrap" style="display:flex; align-items:center; gap:6px;">
                    <span class="group-icon">{{ getRoleIcon(prof.role) }}</span>
                    <span class="group-title">{{ prof.name }}</span>
                    <NTag size="tiny" round type="info">{{ prof.role.toUpperCase() }}</NTag>
                    <NTag size="tiny" round :bordered="false" style="font-family:Consolas, monospace; font-size:10.5px; background:rgba(128,128,160,0.12); color:var(--dim);">ID: {{ pid }}</NTag>
                    <span class="hint" v-if="prof.llm?.model" style="display:inline-flex; align-items:center;">· 模型: {{ prof.llm.model }}</span>
                    <span class="hint" v-else style="display:inline-flex; align-items:center;">· 继承全局模型</span>
                  </div>
                  <div class="group-ops" @click.stop>
                    <NButton size="tiny" secondary :loading="profileFetchingId === pid" @click="fetchProfileModelList(pid)">获取模型列表</NButton>
                    <NButton size="tiny" secondary :loading="profileTestingId === pid" @click="testProfileModel(pid)">测试连接</NButton>
                    <NButton size="tiny" quaternary type="error" @click="removeProfile(pid)">删除</NButton>
                    <span style="font-size: 12px; color: var(--dim)">{{ editingProfileId === pid ? '▲ 收起' : '▼ 编辑' }}</span>
                  </div>
                </div>

                <div v-if="editingProfileId === pid" style="padding: 12px; border-top: 1px solid var(--line); background: var(--card);">
                  <div class="frow">
                    <div class="f1">
                      <label>预设名称</label>
                      <NInput v-model:value="prof.name" placeholder="如 资深后端开发" />
                    </div>
                    <div class="f1">
                      <label>职能角色</label>
                      <NSelect v-model:value="prof.role" :options="roleSelectOptions" />
                    </div>
                    <div class="f14">
                      <label>职责简介（将展示在团队名册）</label>
                      <NInput v-model:value="prof.description" placeholder="负责接口与业务逻辑实现" />
                    </div>
                  </div>

                  <div class="frow" style="margin-top: 8px">
                    <div class="f1">
                      <label>协议 provider（留空继承全局）</label>
                      <NSelect v-model:value="prof.llm.provider" :options="[
                        { label: '继承全局', value: '' },
                        { label: 'openai（兼容协议）', value: 'openai' },
                        { label: 'anthropic', value: 'anthropic' },
                      ]" />
                    </div>
                    <div class="f1">
                      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                        <label style="margin:0">模型名 model（留空继承全局）</label>
                        <NButton size="tiny" quaternary type="primary" :loading="profileFetchingId === pid" @click="fetchProfileModelList(pid)" style="font-size:11px; padding:0 4px">获取模型列表</NButton>
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
                      <label>接口 base_url（留空继承全局）</label>
                      <NInput v-model:value="prof.llm.base_url" placeholder="https://api.deepseek.com/v1" />
                    </div>
                  </div>

                  <div class="frow" style="margin-top: 8px">
                    <div class="f2">
                      <label>API Key（留空继承全局或保持已配置密钥）</label>
                      <NInput v-model:value="prof.llm.api_key" type="password" show-password-on="click"
                              :placeholder="prof.llm.api_key_configured ? `已配置（${prof.llm.api_key_masked}），留空保持` : 'sk-...'"
                              @update:value="profilesTouchedKeys[pid] = true" />
                    </div>
                    <div class="f1">
                      <label>单任务轮数上限（留空用默认）</label>
                      <NInputNumber v-model:value="prof.max_turns" :min="-1" :max="200" placeholder="默认" />
                    </div>
                  </div>

                  <div v-if="prof.role === 'qa'" style="margin-top: 6px;">
                    <label class="checkline">
                      <NSwitch size="small" v-model:value="prof.allow_test_scripts" />
                      <span>允许测试工程师编写/补充自动化测试脚本（开启后允许 QA 新增测试目录脚本并纳入提交）</span>
                    </label>
                  </div>

                  <div style="margin-top: 8px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                      <label style="font-size: 12px; color: var(--dim); margin: 0;">角色专属补充提示词（注入该角色系统提示词）</label>
                      <NButton size="tiny" quaternary type="primary" @click="loadRolePromptTemplate(prof)" style="font-size: 11.5px; padding: 0 4px;">📋 载入内置角色模版（类似 AGENT.md）</NButton>
                    </div>
                    <NInput v-model:value="prof.extra_prompt" type="textarea" :rows="4" :autosize="{ minRows: 3, maxRows: 14 }"
                            placeholder="如：工作流与协作规范、测试要求、编码风格与交付验收标准..." />
                  </div>
                </div>
              </div>
            </div>

            <div class="sfoot" style="margin-top: 14px;">
              <NButton type="primary" :loading="profilesSaving" @click="saveProfiles">保存角色预设池</NButton>
            </div>
          </NCard>
        </div>
        <div v-if="settingsSection === 'sched'">
          <NCard title="⏱ 调度与看板" size="small" class="scard">
      <div class="frow">
        <div class="f1"><label>扫描间隔（秒，30~86400）</label><NInputNumber v-model:value="draft.interval" :min="30" :max="86400" /></div>
        <div class="f1"><label>并行 Agent 数（1~16）</label><NInputNumber v-model:value="draft.parallel" :min="1" :max="16" /></div>
        <div class="f1"><label>看板端口（下次启动生效）</label><NInputNumber v-model:value="draft.port" :min="1" :max="65535" /></div>
      </div>
      <div class="frow">
        <div class="f1"><label>日志保留（天，0 为关闭自动归档清理，保存后立即执行）</label>
          <NInputNumber v-model:value="draft.retention" :min="0" :max="365" /></div>
        <div class="f1"><label>日志总占用上限（MB，超出从最旧删起，0 为不限制）</label>
          <NInputNumber v-model:value="draft.max_total_mb" :min="0" :max="100000" /></div>
      </div>
      <div class="frow">
        <div class="f1"><label>看板登录密码（非空时访问页面/API 均需先登录，保存后立即生效）</label>
          <NInput :value="authPwd" type="password" show-password-on="click"
                  :placeholder="store.state.web_cfg.auth_enabled
                    ? '已设置，留空保持不变' : '未设置（免登录；外网部署务必设置）'"
                  @update:value="onPwdInput" /></div>
        <div class="f0 pwd-btns">
          <NButton v-if="store.state.web_cfg.auth_enabled" size="small" quaternary
                   type="error" :disabled="authPwdClear" @click="clearAuthPwd">清除密码（关闭鉴权）</NButton>
        </div>
      </div>
      <div class="hint pwd-hint" v-if="authPwdClear">
        将在保存后关闭登录鉴权（任何能访问该端口的人都可操作看板）；在上方重新输入密码可取消。
      </div>
      <div class="hint pwd-hint" v-else-if="authPwdTouched">
        将在保存后启用/更新登录密码，所有已登录会话立即失效，需用新密码重新登录。
      </div>
      <div class="sfoot"><NButton type="primary" @click="saveSched">保存调度配置</NButton></div>
          </NCard>
        </div>
        <div v-if="settingsSection === 'notify'">
          <NCard title="🔔 提醒设置" size="small" class="scard">
      <div class="frow">
        <div class="f1"><label class="checkline">
          <NSwitch v-model:value="notifyDraft.sound" />
          提示音
          <span class="hint">完成/受阻时播程序内置提示音（完成「叮咚」，受阻警示音；随程序分发，不跟随系统声音方案），音频缺失时回退 Windows 系统音。</span>
        </label></div>
        <div class="f1"><label class="checkline">
          <NSwitch v-model:value="notifyDraft.system" />
          系统通知
          <span class="hint">完成/受阻时弹托盘气泡，看板没开也能收到；--no-tray 启动时无渠道。</span>
        </label></div>
      </div>
      <div class="sfoot"><NButton type="primary" :loading="notifySaving" @click="saveNotify">保存提醒设置</NButton></div>
          </NCard>
        </div>
        <div v-if="settingsSection === 'review'">
          <NCard title="🔍 任务审查（Review Agent）" size="small" class="scard">
      <label class="checkline">
        <NSwitch v-model:value="reviewDraft.enabled" />
        启用审查（配置审查模型后即对全部项目默认生效；项目可单独关闭）
      </label>
      <template v-if="reviewDraft.enabled">
        <div class="frow" style="margin-top:8px">
          <div class="f1"><label>协议 provider</label>
            <NSelect v-model:value="reviewDraft.provider" :options="[
              { label: 'openai（兼容协议）', value: 'openai' },
              { label: 'anthropic', value: 'anthropic' },
            ]" /></div>
          <div class="f2"><label>审查模型 model（必须与任务模型不同）</label>
            <NSelect v-model:value="reviewDraft.model" :options="reviewModelOptions.map(m => ({ label: m.id, value: m.id }))"
                     filterable tag clearable placeholder="如 deepseek-chat / glm-4-flash（可配便宜小模型）"
                     @update:value="(v: string | null) => { reviewDraft.model = v || '' }" /></div>
        </div>
        <div class="frow">
          <div class="f1"><label>接口 base_url</label>
            <NInput v-model:value="reviewDraft.base_url" placeholder="https://api.deepseek.com/v1" /></div>
          <div class="f1"><label>密钥 api_key</label>
            <NInput v-model:value="reviewDraft.api_key" type="password" show-password-on="click"
                    :placeholder="reviewKeyConfigured && reviewKeyMasked ? `已配置（${reviewKeyMasked}），留空保持原值` : '审查模型服务密钥'"
                    @update:value="reviewKeyTouched = true" /></div>
        </div>
        <div class="frow">
          <div class="f1"><label>单次审查轮数上限（max_turns）</label>
            <NInputNumber v-model:value="reviewDraft.max_turns" :min="-1" :max="100" placeholder="-1 为不限" />
            <div class="hint" style="margin-top:2px">单次审查工具交互轮次上限（-1 为不限，默认 10）</div></div>
          <div class="f1"><label>返工审查轮数上限（全局默认，-1 为不上限）</label>
            <NInputNumber v-model:value="reviewDraft.max_review_rounds" :min="-1" :max="10" placeholder="-1 为不限" />
            <div class="hint" style="margin-top:2px">不通过累计达到上限转受阻（-1 表示不上限持续返工）</div></div>
        </div>
        <label class="checkline stack">
          <NSwitch v-model:value="reviewDraft.allow_test_commands" />
          审查可执行测试命令
          <span class="hint">审查 agent 可用 run_command 实测验证（受项目命令白名单与访问权限审批约束）；关闭=纯只读审查。</span>
        </label>
        <label class="checkline stack">
          <NSwitch v-model:value="reviewDraft.squash_merge" />
          返工通过后收敛提交
          <span class="hint">返工任务的多个 WIP 提交在通过审查后 merge --squash 收敛为单提交；关闭=保留 --no-ff 合并。</span>
        </label>
        <div class="btnrow" style="margin-top:6px">
          <NButton size="small" :loading="reviewFetching" @click="fetchReviewModelList">获取模型列表</NButton>
          <span class="hint">审查与任务使用不同模型交叉验证，防止「自审自批」；后端保存时强制校验互斥。</span>
        </div>
      </template>
      <div class="sfoot"><NButton type="primary" :loading="reviewSaving" @click="saveReview">保存审查配置</NButton></div>
          </NCard>
        </div>
        <div v-if="settingsSection === 'tools'">
          <NCard title="🛠 Agent 工具与访问权限" size="small" class="scard">
            <!-- 一级：四大核心分类 Tab -->
            <div style="margin-bottom: 14px;">
              <NTabs v-model:value="toolsTab" type="segment" size="small">
                <NTabPane name="tools">
                  <template #tab>
                    <span>🛠 Agent 工具能力</span>
                    <NTag size="tiny" round :type="toolsEnabledCount === toolsTotalCount ? 'success' : 'default'" style="margin-left: 6px;">
                      {{ toolsEnabledCount }}/{{ toolsTotalCount }}
                    </NTag>
                  </template>
                </NTabPane>
                <NTabPane name="commands">
                  <template #tab>
                    <span>💻 终端命令白名单</span>
                    <NTag size="tiny" round :type="cmdsEnabledCount === cmdsTotalCount ? 'success' : 'warning'" style="margin-left: 6px;">
                      {{ cmdsEnabledCount }}/{{ cmdsTotalCount }}
                    </NTag>
                  </template>
                </NTabPane>
                <NTabPane name="debug">
                  <template #tab>
                    <span>🌐 本地调试与浏览器</span>
                    <NTag size="tiny" round :type="debugActiveCount > 0 ? 'info' : 'default'" style="margin-left: 6px;">
                      {{ debugActiveCount > 0 ? `${debugActiveCount} 项启用` : '默认关闭' }}
                    </NTag>
                  </template>
                </NTabPane>
                <NTabPane name="perms" tab="🔒 权限模式与环境" />
              </NTabs>
            </div>

            <!-- TAB 1: Agent 工具能力 (Core & Extended Tools) -->
            <div v-show="toolsTab === 'tools'" class="tab-content">
              <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; gap:12px; flex-wrap:wrap;">
                <span class="hint">控制 Agent 在任务中可调用的内置核心工具；关闭的工具不会下发给大模型提示词。</span>
                <NInput v-model:value="toolSearch" size="small" placeholder="搜索工具名称或描述..." clearable style="width: 240px;" />
              </div>

              <div class="tool-groups-container">
                <div v-for="cat in filteredToolCategories" :key="cat.key" class="group-card">
                  <div class="group-header">
                    <div class="group-title-wrap">
                      <span class="group-icon">{{ cat.icon }}</span>
                      <span class="group-title">{{ cat.label }}</span>
                      <span class="group-desc">{{ cat.description }}</span>
                    </div>
                    <div class="group-ops">
                      <NTag size="small" round :type="getGroupToolsCount(cat.tools).enabled === getGroupToolsCount(cat.tools).total ? 'success' : 'warning'">
                        已启用 {{ getGroupToolsCount(cat.tools).enabled }} / {{ getGroupToolsCount(cat.tools).total }}
                      </NTag>
                      <NButton size="tiny" quaternary @click="toggleToolsGroup(cat.tools, true)">全选</NButton>
                      <NButton size="tiny" quaternary @click="toggleToolsGroup(cat.tools, false)">全关</NButton>
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
                      <span class="group-icon">🧭</span>
                      <span class="group-title">本地开发服务与浏览器自动化扩展</span>
                      <span class="group-desc">包含 dev_server_* 服务管理（3工具）、http_request 接口调试（1工具）与 Chromium 浏览器自动化（10工具）</span>
                    </div>
                    <div class="group-ops">
                      <NButton size="small" type="primary" secondary @click="toolsTab = 'debug'">
                        进入本地调试配置 ({{ debugActiveCount }} 项启用) →
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
                <div style="display:flex; gap:8px; align-items:center;">
                  <NButton size="tiny" secondary @click="toggleAllCommands(true)">全部放行</NButton>
                  <NButton size="tiny" secondary @click="toggleAllCommands(false)">全部禁用</NButton>
                  <NInput v-model:value="cmdSearch" size="small" placeholder="过滤命令（如 pytest、npm）..." clearable style="width: 220px;" />
                </div>
              </div>

              <div class="cmd-groups-container">
                <div v-for="cat in filteredCommandCategories" :key="cat.key" class="group-card">
                  <div class="group-header">
                    <div class="group-title-wrap">
                      <span class="group-icon">{{ cat.icon }}</span>
                      <span class="group-title">{{ cat.label }}</span>
                      <span class="group-desc">{{ cat.description }}</span>
                    </div>
                    <div class="group-ops">
                      <NTag size="small" round :type="getGroupCommandsCount(cat.commands).enabled === getGroupCommandsCount(cat.commands).total ? 'success' : 'warning'">
                        已放行 {{ getGroupCommandsCount(cat.commands).enabled }} / {{ getGroupCommandsCount(cat.commands).total }}
                      </NTag>
                      <NButton size="tiny" quaternary @click="toggleCommandsGroup(cat.commands, true)">全选</NButton>
                      <NButton size="tiny" quaternary @click="toggleCommandsGroup(cat.commands, false)">全关</NButton>
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
                      <span class="group-icon">🚀</span>
                      <span class="group-title">后台开发服务管理 (dev_server)</span>
                      <span class="group-desc">支持任务中后台常驻启动 dev server（自动端口与就绪等待），任务结束自动清理</span>
                    </div>
                    <div class="group-ops">
                      <label class="checkline" style="gap:6px">
                        <span style="font-size:12px; font-weight:600">服务组主开关：</span>
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
                      <span class="group-icon">🌐</span>
                      <span class="group-title">本地 HTTP API 调试 (http_request)</span>
                      <span class="group-desc">支持对本地或内网开发服务发起 HTTP 请求，调试 REST/GraphQL 接口（受下方主机白名单约束）</span>
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
                      <span class="group-icon">🧭</span>
                      <span class="group-title">本地 Chromium 浏览器调试 (browser)</span>
                      <span class="group-desc">驱动本地 Chrome/Edge 浏览器，支持访问页面、点击输入、截图与监听控制台/网络日志</span>
                    </div>
                    <div class="group-ops">
                      <label class="checkline" style="gap:6px">
                        <span style="font-size:12px; font-weight:600">浏览器组主开关：</span>
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
                      <span class="group-icon">⚙️</span>
                      <span class="group-title">调试环境运行参数与安全限制</span>
                      <span class="group-desc">配置浏览器内核路径、窗口显示模式及内网穿透防护主机白名单</span>
                    </div>
                  </div>
                  <div style="padding: 12px;">
                    <div class="frow">
                      <div class="f1"><label>浏览器引擎</label>
                        <NSelect v-model:value="localDebugDraft.browser" :options="[
                          { label: 'auto（优先 Chrome，无则回退 Edge）', value: 'auto' },
                          { label: 'Google Chrome', value: 'chrome' },
                          { label: 'Microsoft Edge', value: 'edge' },
                        ]" /></div>
                      <div class="f2"><label>自定义浏览器路径（留空则自动探测系统安装）</label>
                        <NInput v-model:value="localDebugDraft.browser_executable" placeholder="C:\...\chrome.exe" /></div>
                    </div>
                    <div class="frow" style="margin-top:8px;align-items:center">
                      <label class="cmd-chip">
                        <NSwitch size="small" v-model:value="localDebugDraft.headless" />
                        <span class="cmd-name">无头模式运行（关闭时在桌面弹出浏览器窗口，可直观围观操作）</span>
                      </label>
                      <NButton size="small" :loading="testingBrowser" @click="onTestBrowser">测试浏览器探测</NButton>
                    </div>
                    <div class="frow" style="margin-top:10px">
                      <div class="f2">
                        <label>允许访问的主机白名单（每行一个，防内网穿透；默认仅限回环）</label>
                        <NInput v-model:value="localDebugDraft.hosts_text" type="textarea" :rows="3"
                                placeholder="localhost&#10;127.0.0.1&#10;::1" />
                      </div>
                      <div class="f1">
                        <label>导航超时 / 服务就绪超时（秒）</label>
                        <div style="display:flex;gap:8px">
                          <NInputNumber v-model:value="localDebugDraft.navigation_timeout_seconds" :min="5" :max="300" placeholder="导航" />
                          <NInputNumber v-model:value="localDebugDraft.service_ready_timeout_seconds" :min="5" :max="600" placeholder="就绪" />
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
                  🛡 敏感操作访问权限模式
                </div>
                <div class="frow">
                  <div class="f2"><label>访问权限模式（敏感操作是否挂起等待看板批准）</label>
                    <NSelect v-model:value="permDraft.mode" :options="permOptions" /></div>
                  <div class="f1"><label>审批超时（分钟，1~1440，无人响应自动拒绝）</label>
                    <NInputNumber v-model:value="permDraft.timeout_minutes" :min="1" :max="1440" /></div>
                </div>
                <div class="hint pwd-hint" style="margin-top: 6px;">
                  询问 = 任务挂起，顶栏出现「🔔 待审批」入口，批准/拒绝后继续；项目对话框可按项目覆盖模式。
                </div>
              </div>

              <div class="group-card" style="margin-bottom: 12px; padding: 12px;">
                <label class="checkline stack" style="margin: 0; padding: 0; border: none; background: none;">
                  <div style="display:flex;align-items:center;gap:8px">
                    <NSwitch v-model:value="gitCommitAndMergeDraft" />
                    <span style="font-weight: 600; font-size: 13px; color: var(--text);">🌿 任务完成后自动提交并合并代码（Git 自动提交与合并）</span>
                  </div>
                  <span class="hint" style="margin-top: 6px;">默认关闭：Agent 直接在项目工作区修改代码，不自动产生 Git 提交与主分支合并，方便人工检查与自主提交；开启后在独立 worktree 分支完成开发并自动提交合并。</span>
                </label>
              </div>

              <div class="group-card" style="padding: 12px;">
                <div style="font-size: 13px; font-weight: 600; margin-bottom: 8px; color: var(--text);">
                  💻 Agent 子进程环境变量与 PATH 引导
                </div>
                <label style="font-size: 12px; color: var(--dim); margin-bottom: 4px; display: block;">自定义环境变量（代理 / 国内镜像，每行 KEY=VALUE）</label>
                <NInput v-model:value="envDraft" type="textarea" :rows="3"
                        placeholder="每行一个 KEY=VALUE（也接受 KEY: VALUE），例如：&#10;GOPROXY=https://goproxy.cn,direct&#10;HTTP_PROXY=http://127.0.0.1:7890" />
                <div class="hint tool-foot" style="margin-top: 6px;">
                  注入 run_command 子进程：国内网络拉依赖失败时配 GOPROXY、npm registry（HTTP_PROXY）、PIP_INDEX_URL 即可；不允许覆盖 PATH/SYSTEMROOT 等系统变量。
                </div>
                <label class="cmd-chip" style="margin-top: 6px;" title="写入前备份旧值到日志，只增不删，可手动改回">
                  <NSwitch size="small" v-model:value="pathAutofix" />
                  <span class="cmd-name">任务启动时自动把缺失的工具目录写入用户 PATH</span>
                </label>
                <div class="hint tool-foot" style="margin-top: 6px;">
                  启动任务时扫描 node/npm/go/java 等常用命令：不在 PATH 的自动注入 Agent 会话并写进提示词；开启后还会把缺失目录持久写入用户 PATH。
                </div>
              </div>
            </div>

            <!-- 统一底栏保存按钮 -->
            <div class="sfoot" style="margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--line);">
              <NButton type="primary" :loading="toolsSaving" @click="saveTools">保存工具与权限</NButton>
            </div>
          </NCard>
        </div>
        <div v-if="settingsSection === 'mirrors'">
          <NCard title="🪞 镜像源管理与测速选源" size="small" class="scard">
      <div class="mirror-ops">
        <NButton size="small" :loading="mirrorTesting" @click="testMirrors()">测速全部源</NButton>
        <NButton size="small" type="primary" :loading="mirrorApplying" @click="applyMirrors">
          应用最快源到环境变量</NButton>
        <span class="hint" v-if="mirrorTestedAt">上次测速：{{ mirrorTestedAtText }}</span>
        <span class="hint" v-else>尚未测速（Agent 会按内置默认顺序下载）</span>
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
            {{ cat.kind === 'proxy' ? `可应用 → ${cat.env_var}` : '下载源' }}</NTag>
          <NButton size="tiny" quaternary :loading="mirrorTestingCat === cat.key"
                   :disabled="mirrorTesting" @click="testMirrors(cat.key)">只测这类</NButton>
        </div>
        <div v-for="row in mirrorRows(cat.key)" :key="row.url" class="mrow">
          <NSwitch size="small" :value="mirrorEnabled(row.url)"
                   @update:value="(v: boolean) => toggleMirror(row.url, v)" />
          <span class="murl">{{ row.url }}</span>
          <NTag v-if="row.builtin" size="tiny" :bordered="false">内置·{{ row.label }}</NTag>
          <NTag v-else size="tiny" :bordered="false" type="success">自定义</NTag>
          <NTag size="tiny" :bordered="false" :type="latencyTag(row.url).type">
            {{ latencyTag(row.url).text }}</NTag>
          <NButton v-if="!row.builtin" size="tiny" quaternary type="error"
                   @click="removeCustom(cat.key, row.url)">删除</NButton>
        </div>
      </div>

      <div class="madd">
        <NSelect v-model:value="addCat" size="small" :options="catOptions" class="madd-cat" />
        <NInput v-model:value="addUrl" size="small" class="madd-url"
                placeholder="要添加的镜像 URL（http(s)://，需可直接访问；测速时会一并探测）"
                @keyup.enter="addCustom" />
        <NButton size="small" @click="addCustom">添加镜像源</NButton>
      </div>
      <div class="hint tool-foot">
        添加/禁用需点下方「保存镜像源配置」生效；禁用的源不参与测速、也不会推荐给 Agent；
        自定义源若是内网镜像通常最快，测速后自动成为首选。
      </div>

      <div class="sfoot">
        <NButton type="primary" :loading="mirrorsSaving" :disabled="!mirrorsDirty"
                 @click="saveMirrors">保存镜像源配置</NButton>
      </div>
          </NCard>
        </div>
        <div v-if="settingsSection === 'app'">
          <NCard title="⚙️ 应用" size="small" class="scard">
      <label class="checkline">
        <NSwitch :value="store.state.app_autostart.enabled" @update:value="toggleAutostart" />
        开机自动启动（写入当前用户注册表）
      </label>
      <label class="checkline" style="margin-top:10px">
        <NSwitch v-model:value="updDraft.auto_check" />
        启动时自动检查更新（Gitee Release 源）
      </label>
      <div class="frow">
        <div class="f2"><label>更新源仓库 URL（Gitee，Release 附件放安装包）</label>
          <NInput v-model:value="updDraft.repo" placeholder="https://gitee.com/xxx/orcsy" /></div>
        <div class="f1"><label>访问 token（私有仓库用）</label>
          <NInput v-model:value="updDraft.token" type="password" show-password-on="click"
                  :placeholder="updTokenConfigured
                    ? `已配置：${updTokenMasked}（留空保持不变）` : '可选'"
                  @update:value="updTokenTouched = true" /></div>
      </div>
      <div class="sfoot">
        <NButton :loading="updSaving" @click="saveUpdate">保存更新配置</NButton>
        <NButton :loading="updChecking" @click="checkUpdateNow">检查更新</NButton>
      </div>
      <div class="pkg-block">
        <div class="pkg-row">
          <div class="f2">
            <label>手动导入本地安装包（orcsy-update-*.zip；同版本覆盖、升/降级均可）</label>
            <NInput :value="localPkg" readonly placeholder="尚未选择更新包文件" />
          </div>
          <div class="f0 pkg-btns">
            <NButton @click="pickPackage">选择安装包…</NButton>
            <NButton type="primary" :loading="pkgInstalling" :disabled="!localPkg"
                     @click="installPackage">安装更新包</NButton>
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
/* 左手风琴分区 + 右内容区：窄屏自动竖排 */
.layout { display: grid; grid-template-columns: 240px minmax(0, 1fr); gap: 14px; align-items: start; }
@media (max-width: 860px) { .layout { grid-template-columns: 1fr; } }
.menu { position: sticky; top: 14px; }
.menu :deep(.n-collapse-item__header) { font-size: 13.5px; }
.menu :deep(.n-collapse-item__content-inner) { padding-top: 0; }
.menu-desc { font-size: 12px; color: var(--faint); cursor: pointer; }
.menu-desc:hover { color: var(--dim); }
.content { min-width: 0; }
.content .scard { margin-bottom: 0; }
.modelspec { font-size: 12px; color: var(--dim); margin: -4px 0 8px; }
.hint { font-size: 12px; color: var(--faint); font-weight: 400; }
.frow { display: flex; gap: 10px; margin-bottom: 10px; }
.frow > div { display: flex; flex-direction: column; gap: 4px; }
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
