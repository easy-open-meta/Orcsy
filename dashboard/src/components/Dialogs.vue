<script setup lang="ts">
import { NButton, NCheckbox, NInput, NInputNumber, NModal, NSelect, NSpin, NSwitch, NTag, useDialog, useMessage } from 'naive-ui'
import { computed, nextTick, onMounted, reactive, ref, shallowRef, watch } from 'vue'
import { api, ConflictError, type DirListing, type ModelInfo, type PermissionMode, type RunDetailT, type RunSummaryT, type TaskDepT, type TeamMemberT } from '../api'
import { isMobile, viewportHeight } from '../mobile'
import { miIcon } from '../mi'
import { beginRestartWait, navigateTo, openTaskView, refresh, ui, store } from '../store'
import { tNow as t, tFmt } from '../lang'
import ProjectFormSection from './project/ProjectFormSection.vue'
import ProviderHelp from './ProviderHelp.vue'

// 把 useMessage/useDialog 桥接到 window，供任意组件调用。
// bridge 兼容两种用法：message('文本') 默认 info；message.success('文本') 等——
// 本组件内统一用 message.xxx，window.$message 供其它组件直呼
const messageApi = useMessage()
const message = ((content: string, type: 'success' | 'warning' | 'error' | 'info' = 'info') => {
  (messageApi as any)[type]?.(content)
}) as any
Object.assign(message, messageApi)
;(window as any).$message = message
;(window as any).$dialog = useDialog() as any

// ---------- 项目对话框 ----------
interface TeamMemberDraft {
  id: string
  profile: string
  enabled: boolean
  name: string
  role: string
  description: string
  ll_model: string
  ll_protocol: string
  ll_base: string
  ll_key: string
  ll_key_masked: string
  allow_test_scripts: boolean
  extra_prompt: string
}

const pDraft = reactive({
  name: '', path: '', tasks_dir: 'tasks', guide_file: 'AGENT.md',
  max_tasks: 1, max_turns: 40, branch: '', cmds: '', enabled: true,
  stack: '', run: '', test: '', err: '',
  // 专属模型绑定：use_llm 关=跟随全局；开=部分覆盖（空字段仍继承全局）
  use_llm: false, ll_provider: '' as '' | 'openai' | 'anthropic',
  ll_protocol: '' as '' | 'native' | 'text',
  ll_base: '', ll_key: '', ll_model: '',
  // 远程仓库绑定：拉取 open Issues 入任务队列（单向）；token 与账号密码二选一；
  // repo_auto_pull 为自动拉取间隔（分钟，0=仅手动）
  use_repo: false, repo_url: '', repo_token: '', repo_user: '', repo_pass: '',
  repo_provider: '' as '' | 'github' | 'gitee' | 'gitlab' | 'gitea',
  repo_auto_pull: 0,
  // 项目级工具开关覆盖：use_tools 关=跟随全局；开=tools 里的逐项布尔全量覆盖
  use_tools: false, tools: {} as Record<string, boolean>,
  // 项目级访问权限覆盖：'' = 跟随全局 permissions.mode
  perm_mode: '' as '' | PermissionMode,
  // 审查 Agent：use_review 三态开关（false=跟随全局 review.enabled）；
  // 审查模型绑定部分覆盖全局 review.llm；rounds null=跟随全局（1~10 自定义）
  use_review: false, review_ll_provider: '' as '' | 'openai' | 'anthropic',
  review_ll_base: '', review_ll_key: '', review_ll_model: '',
  review_ll_protocol: '' as '' | 'native' | 'text',
  review_rounds: null as number | null,
  debug_hosts_text: '',
  use_cam: false,
  cam_enabled: false,
  premerge_test_enabled: false,
  premerge_test_command: '',
  git_author_name: '', git_author_email: '',
  team: {
    enabled: false,
    execution_mode: 'parallel_worktree' as 'parallel_worktree' | 'sequential',
    max_qa_rounds: 3,
    max_split_depth: 1,
    chain_within_cycle: true,
    max_chain_steps_per_cycle: 10,
    fallback_role: 'backend',
    members: [] as TeamMemberDraft[],
  },
})

const teamMemberKeyTouched = reactive<Record<string, boolean>>({})
const gitIniting = shallowRef(false)

// 选项标签随语言切换更新：tNow 为一次性取值，须包 computed 才响应 lang 变化
const roleSelectOptions = computed(() => [
  { label: t('项目经理 (PM)'), value: 'pm' },
  { label: t('后端开发工程师'), value: 'backend' },
  { label: t('前端开发工程师'), value: 'frontend' },
  { label: t('测试工程师 (QA)'), value: 'qa' },
  { label: t('运维/DevOps 工程师'), value: 'devops' },
  { label: t('UI 视觉审查专家'), value: 'ui_critic' },
  { label: t('安全合规审计工程师'), value: 'security' },
  { label: t('自定义专职角色'), value: 'custom' },
])

function getMemberRoleIcon(role: string): string {
  const map: Record<string, string> = {
    pm: 'manage_accounts', backend: 'terminal', frontend: 'palette',
    qa: 'science', devops: 'dns', ui_critic: 'visibility',
    security: 'gpp_maybe', custom: 'extension',
  }
  return map[role] || 'smart_toy'
}

const globalProfileOptions = computed(() => {
  const profs = store.state?.agent_profiles || {}
  return Object.values(profs).map(p => ({
    label: `${p.name} (${p.role.toUpperCase()})`,
    value: p.id,
  }))
})

function addTeamMember(): void {
  const base = 'custom'
  let id = base
  let n = 2
  const existing = new Set(pDraft.team.members.map(m => m.id))
  while (existing.has(id)) {
    id = `${base}_${n}`
    n++
  }
  pDraft.team.members.push({
    id, profile: '', enabled: true, name: '', role: 'custom',
    description: '', ll_model: '', ll_protocol: '', ll_base: '', ll_key: '', ll_key_masked: '',
    allow_test_scripts: false, extra_prompt: '',
  })
}

// 新建项目默认小组成员：内置角色逐个绑定同名预设（预设不存在则留空自定义配置）。
// 0.31.0 起角色池扩展了 devops / ui_critic / security——老版本升级后没有这些
// 预设也照常加成员，模型与工具走角色内置默认（可在成员卡里再改）
function defaultTeamMembers(): TeamMemberDraft[] {
  const roles: Array<{ id: string, profile: string, role: string }> = [
    { id: 'pm', profile: 'pm_default', role: 'pm' },
    { id: 'backend', profile: 'backend_default', role: 'backend' },
    { id: 'frontend', profile: 'frontend_default', role: 'frontend' },
    { id: 'qa', profile: 'qa_default', role: 'qa' },
    { id: 'devops', profile: 'devops_default', role: 'devops' },
    { id: 'ui_critic', profile: 'ui_critic_default', role: 'ui_critic' },
    { id: 'security', profile: 'security_default', role: 'security' },
  ]
  const profs = store.state?.agent_profiles || {}
  return roles.map(r => ({
    id: r.id, profile: r.profile in profs ? r.profile : '', enabled: true,
    name: '', role: r.role, description: '', ll_model: '', ll_key: '',
    ll_key_masked: '', allow_test_scripts: false, extra_prompt: '',
    ll_protocol: '', ll_base: '',
  }))
}

function removeTeamMember(idx: number): void {
  pDraft.team.members.splice(idx, 1)
}

async function onGitInitProject(): Promise<void> {
  const pName = pDraft.name.trim() || ui.projectDlg?.original || ''
  if (!pName) return
  gitIniting.value = true
  try {
    const res = await api.gitInitProject({ name: pName })
    message.success(res.message || t('已成功初始化 Git 仓库'))
    void refresh(true)
  } catch (e: any) {
    message.error(e.message)
  } finally {
    gitIniting.value = false
  }
}

const browserToolNames = [
  'browser_navigate', 'browser_snapshot', 'browser_click',
  'browser_type', 'browser_wait', 'browser_screenshot',
  'browser_console', 'browser_network', 'browser_eval',
  'browser_close',
]
const devServerToolNames = [
  'dev_server_start', 'dev_server_logs', 'dev_server_stop',
]

const pBrowserGroupEnabled = computed({
  get: () => browserToolNames.some(k => pDraft.tools[k]),
  set: (val: boolean) => {
    for (const k of browserToolNames) pDraft.tools[k] = val
  },
})

const pDevServerGroupEnabled = computed({
  get: () => devServerToolNames.some(k => pDraft.tools[k]),
  set: (val: boolean) => {
    for (const k of devServerToolNames) pDraft.tools[k] = val
  },
})

// 选项标签随语言切换更新（tNow 一次性取值，须包 computed）
const defaultProjectToolCategories = computed(() => [
  {
    key: 'file_ops',
    label: t('文件操作与编辑'),
    icon: 'folder',
    tools: ['read_file', 'write_file', 'edit_file', 'multi_edit', 'delete_file', 'move_file', 'list_dir'],
  },
  {
    key: 'code_search',
    label: t('代码搜索与定位'),
    icon: 'search',
    tools: ['find_files', 'grep'],
  },
  {
    key: 'command_exec',
    label: t('终端命令执行'),
    icon: 'terminal',
    tools: ['run_command'],
  },
  {
    key: 'task_collab',
    label: t('任务管理与协议'),
    icon: 'task_alt',
    tools: ['create_tasks', 'finish'],
  },
  {
    key: 'session_bus',
    label: t('批次共享状态'),
    icon: 'share',
    tools: ['session_share', 'session_read'],
  },
  {
    key: 'project_memory',
    label: t('项目架构记忆'),
    icon: 'account_tree',
    tools: ['update_architecture', 'update_project_notes'],
  },
  {
    key: 'external_net',
    label: t('网络与外部知识'),
    icon: 'language',
    tools: ['read_skill', 'web_fetch', 'speedtest_mirrors'],
  },
  {
    key: 'local_debug',
    label: t('本地开发服务与 API'),
    icon: 'rocket_launch',
    tools: ['dev_server_start', 'dev_server_logs', 'dev_server_stop', 'http_request'],
  },
  {
    key: 'browser_auto',
    label: t('Chromium 浏览器自动化'),
    icon: 'explore',
    tools: [
      'browser_navigate', 'browser_snapshot', 'browser_click',
      'browser_type', 'browser_wait', 'browser_screenshot',
      'browser_console', 'browser_network', 'browser_eval',
      'browser_close',
    ],
  },
])

const projectToolCategories = computed(() => {
  return store.state?.tool_categories ?? defaultProjectToolCategories.value
})

function pToggleToolsGroup(tools: string[], enable: boolean): void {
  for (const name of tools) {
    if (name === 'finish') continue
    pDraft.tools[name] = enable
  }
}

function pGetGroupToolsCount(tools: string[]): { enabled: number; total: number } {
  let enabled = 0
  for (const t of tools) {
    if (t === 'finish' || pDraft.tools[t]) enabled++
  }
  return { enabled, total: tools.length }
}

const pMutatingToolsSet = new Set([
  'write_file', 'edit_file', 'multi_edit', 'delete_file', 'move_file', 'run_command',
  'create_tasks', 'dev_server_start', 'browser_click', 'browser_type', 'browser_eval',
])
const pRiskyToolsSet = new Set(['delete_file', 'move_file', 'run_command', 'dev_server_start'])

function pGetToolRiskTag(name: string): { label: string; type: 'success' | 'warning' | 'error' | 'info' } {
  if (name === 'finish') return { label: '核心协议', type: 'info' }
  if (pRiskyToolsSet.has(name)) return { label: '高危', type: 'error' }
  if (pMutatingToolsSet.has(name)) return { label: '变更', type: 'warning' }
  return { label: '只读', type: 'success' }
}

function pGetToolDesc(name: string): string {
  const t = store.state?.tools_catalog?.find(x => x.name === name)
  return t ? t.description : ''
}

// 密钥类字段三态（H2）：看板只回显掩码，不回填明文。未触碰的字段保存时不带
// 对应的键（后端保持原值）；触碰过（含又清空）才上报，空串 = 显式清除
const llKeyTouched = shallowRef(false)
const repoTokenTouched = shallowRef(false)
const repoPassTouched = shallowRef(false)
const reviewKeyTouched = shallowRef(false)
// 正在编辑的项目（占位符展示已配置掩码用；新建时为空）
const editingProject = computed(() =>
  ui.projectDlg?.original
    ? store.state?.projects.find(x => x.name === ui.projectDlg?.original) ?? null
    : null)
const llKeyPlaceholder = computed(() => {
  const pl = editingProject.value?.llm
  if (pl?.api_key_configured) return `已配置：${pl.api_key_masked}（留空保持不变）`
  return store.state?.llm.key_configured ? '留空继承全局密钥' : 'sk-...'
})
const repoTokenPlaceholder = computed(() => {
  const r = editingProject.value?.repo
  if (r?.token_configured) return `已配置：${r.token_masked}（留空保持不变）`
  return 'ghp_... / gitee私人令牌 / glpat-... / gitea token'
})
const repoPassPlaceholder = computed(() => {
  const r = editingProject.value?.repo
  if (r?.password_configured) return `已配置：${r.password_masked}（留空保持不变）`
  return 'password'
})

// ---------- 访问权限（人工审批）公共文案 ----------
// 选项标签随语言切换更新（tNow 一次性取值，须包 computed）
const PERM_LABEL = computed<Record<string, string>>(() => ({
  ask_on_change: t('变更前询问'),
  autonomous: t('自主模式'),
  full_access: t('完全访问'),
}))
const permOptions = computed(() => [
  { label: tFmt('跟随全局（当前：{p0}）', { p0: PERM_LABEL.value[store.state?.permissions_cfg?.mode || 'full_access'] }), value: '' },
  { label: t('变更前询问（文件/命令变更都询问）'), value: 'ask_on_change' },
  { label: t('自主模式（仅高风险询问：删除/移动/执行命令）'), value: 'autonomous' },
  { label: t('完全访问（不询问，减少确认次数）'), value: 'full_access' },
])

// ---------- 人工审批面板 ----------
// 列表直接绑定 store.state.approvals：5s 轮询自动刷新，被处理/超时的条目自动消失
const approvalVisible = computed({
  get: () => ui.approvalDlg,
  set: (v: boolean) => { ui.approvalDlg = v },
})
const approvals = computed(() => store.state?.approvals ?? [])
const alwaysAllow = reactive<Record<string, boolean>>({})  // 审批 id → 「始终允许」勾选
const responding = shallowRef('')

function fmtWaited(s: number): string {
  if (s < 60) return `${s} 秒`
  const m = Math.floor(s / 60)
  return m < 60 ? `${m} 分 ${s % 60} 秒` : `${Math.floor(m / 60)} 小时 ${m % 60} 分`
}

async function respondApproval(id: string, approved: boolean): Promise<void> {
  responding.value = id
  try {
    await api.approvalRespond(id, approved, !!alwaysAllow[id])
    message(approved ? t('已批准，任务继续执行') : t('已拒绝，模型将调整方案或按受阻收尾'),
            approved ? 'success' : 'warning')
    delete alwaysAllow[id]
  } catch (e: any) { message(e.message, 'error') }
  finally { responding.value = '' }
  void refresh(true)
}

// 合入前风险审批：与工具审批共用弹窗，但决定的是「是否合入」，不能勾「始终允许」
const riskApprovals = computed(() => store.state?.risk_approvals ?? [])
const riskComments = reactive<Record<string, string>>({})
const riskResponding = shallowRef('')
function riskLevelLabel(level: string): string {
  return level === 'high' ? t('高风险')
    : level === 'medium' ? t('中风险') : t('低风险')
}
function riskLevelType(level: string): 'error' | 'warning' | 'info' {
  return level === 'high' ? 'error' : level === 'medium' ? 'warning' : 'info'
}
async function respondRiskApproval(id: string, approved: boolean): Promise<void> {
  riskResponding.value = id
  try {
    await api.riskApprovalRespond(id, approved, riskComments[id] || '')
    message(approved ? t('已批准合入') : t('已拒绝合入，任务保持受阻'),
            approved ? 'success' : 'warning')
    delete riskComments[id]
  } catch (e: any) { message(e.message, 'error') }
  finally { riskResponding.value = '' }
  void refresh(true)
}
const pVisible = computed({
  get: () => !!ui.projectDlg,
  set: (v: boolean) => { if (!v) ui.projectDlg = null },
})
watch(() => ui.projectDlg, (dlg) => {
  if (!dlg) return
  pDraft.err = ''
  const p = dlg.original ? store.state?.projects.find(x => x.name === dlg.original) : null
  pDraft.name = p?.name ?? ''
  pDraft.path = p?.path ?? ''
  pDraft.tasks_dir = p?.tasks_dir ?? 'tasks'
  pDraft.guide_file = p?.guide_file ?? 'AGENT.md'
  pDraft.max_tasks = p?.max_tasks_per_cycle ?? 1
  pDraft.max_turns = p?.max_agent_turns ?? 40
  pDraft.branch = p?.allowed_branch ?? ''
  pDraft.cmds = p?.allowed_commands?.join(', ') ?? ''
  pDraft.enabled = p?.enabled ?? true
  const pl = p?.llm ?? null
  pDraft.use_llm = !!pl
  pDraft.ll_provider = (pl?.provider as '' | 'openai' | 'anthropic') ?? ''
  pDraft.ll_base = pl?.base_url ?? ''
  pDraft.ll_protocol = (pl?.tool_protocol as '' | 'native' | 'text') ?? ''
  pDraft.ll_key = ''  // 不回填明文：只显示掩码占位，留空即保持原值
  llKeyTouched.value = false
  pDraft.ll_model = pl?.model ?? ''
  pModels.value = []
  const pr = p?.repo ?? null
  pDraft.use_repo = !!pr
  pDraft.repo_url = pr?.url ?? ''
  pDraft.repo_token = ''  // 同上：token/密码均只回掩码
  pDraft.repo_user = pr?.username ?? ''
  pDraft.repo_pass = ''
  repoTokenTouched.value = false
  repoPassTouched.value = false
  pDraft.repo_provider = (pr?.provider as '' | 'github' | 'gitee' | 'gitlab' | 'gitea') ?? ''
  pDraft.repo_auto_pull = pr?.auto_pull_interval ? Math.round(pr.auto_pull_interval / 60) : 0
  // 工具开关覆盖：项目已有覆盖用其值；否则以当前全局生效值打底（开覆盖即所见即所得）
  // finish 只是展示行（常开且禁用），不进覆盖 map
  const pt = p?.tools ?? null
  pDraft.use_tools = !!pt
  pDraft.tools = {}
  const defaultOff = new Set(store.state?.default_off_tools ?? [
    ...browserToolNames, ...devServerToolNames, 'http_request',
  ])
  for (const t of store.state?.tools_catalog ?? []) {
    if (t.name === 'finish') continue
    const defVal = !defaultOff.has(t.name)
    pDraft.tools[t.name] = pt?.[t.name] ?? store.state?.tools_cfg?.[t.name] ?? defVal
  }
  pDraft.perm_mode = (p?.permission_mode ?? '') as '' | PermissionMode
  // 审查 Agent：开关三态（review_enabled null/true/false）+ 绑定回显 + 返工次数
  pDraft.use_review = p?.review_enabled ?? false
  const prl = p?.review_llm ?? null
  pDraft.review_ll_provider = (prl?.provider as '' | 'openai' | 'anthropic') ?? ''
  pDraft.review_ll_base = prl?.base_url ?? ''
  pDraft.review_ll_protocol = (prl?.tool_protocol as '' | 'native' | 'text') ?? ''
  pDraft.review_ll_key = ''  // 不回填明文：掩码占位，留空保持原值
  reviewKeyTouched.value = false
  pDraft.review_ll_model = prl?.model ?? ''
  pDraft.review_rounds = p?.review_max_rounds ?? null
  pDraft.debug_hosts_text = (p?.debug_allowed_hosts ?? []).join('\n')
  pDraft.use_cam = p?.commit_and_merge !== undefined && p?.commit_and_merge !== null
  pDraft.git_author_name = p?.git_author_name ?? ''
  pDraft.git_author_email = p?.git_author_email ?? ''
  pDraft.cam_enabled = p?.commit_and_merge ?? false
  pDraft.premerge_test_enabled = p?.premerge_test_enabled ?? false
  pDraft.premerge_test_command = p?.premerge_test_command
    ? JSON.stringify(p.premerge_test_command)
    : ''

  for (const k of Object.keys(teamMemberKeyTouched)) delete teamMemberKeyTouched[k]
  const ptTeam = p?.team ?? null
  if (ptTeam) {
    pDraft.team.enabled = !!ptTeam.enabled
    pDraft.team.execution_mode = ptTeam.execution_mode || 'parallel_worktree'
    pDraft.team.max_qa_rounds = ptTeam.max_qa_rounds ?? 3
    pDraft.team.max_split_depth = ptTeam.max_split_depth ?? 1
    pDraft.team.chain_within_cycle = ptTeam.chain_within_cycle ?? true
    pDraft.team.max_chain_steps_per_cycle = ptTeam.max_chain_steps_per_cycle ?? 10
    pDraft.team.fallback_role = ptTeam.fallback_role || 'backend'
    pDraft.team.members = (ptTeam.members || []).map((m: TeamMemberT) => ({
      id: m.id,
      profile: m.profile || '',
      enabled: m.enabled !== false,
      name: m.name || '',
      role: m.role || '',
      description: m.description || '',
      ll_model: m.llm?.model || '',
      ll_protocol: m.llm?.tool_protocol || '',
      ll_base: m.llm?.base_url || '',
      ll_key: '',
      ll_key_masked: m.llm?.api_key_masked || '',
      allow_test_scripts: m.allow_test_scripts ?? false,
      extra_prompt: m.extra_prompt || '',
    }))
  } else {
    pDraft.team.enabled = false
    pDraft.team.execution_mode = 'parallel_worktree'
    pDraft.team.max_qa_rounds = 3
    pDraft.team.max_split_depth = 1
    pDraft.team.chain_within_cycle = true
    pDraft.team.max_chain_steps_per_cycle = 10
    pDraft.team.fallback_role = 'backend'
    pDraft.team.members = defaultTeamMembers()
  }

  pDraft.stack = ''; pDraft.run = ''; pDraft.test = ''
})

// ---- 专属模型绑定：拉取列表 / 测试连接（未填字段回退全局 llm 配置） ----
const pModels = ref<ModelInfo[]>([])
const pFetching = shallowRef(false)
const pTesting = shallowRef(false)
const pModelOptions = computed(() => pModels.value.map(m => ({ label: m.id, value: m.id })))
// 选项标签随语言切换更新（tNow 一次性取值，须包 computed）
const llProviderOptions = computed(() => [
  { label: t('继承全局'), value: '' },
  { label: t('openai（兼容协议）'), value: 'openai' },
  { label: 'anthropic', value: 'anthropic' },
])
const effLlm = () => ({
  provider: pDraft.ll_provider || store.state?.llm.provider || 'openai',
  base_url: pDraft.ll_base.trim() || store.state?.llm.base_url || '',
  // 明文密钥不再下发；留空时后端自动回退已存密钥（fetch_models/test_model 逻辑）
  api_key: pDraft.ll_key.trim(),
  model: pDraft.ll_model.trim() || store.state?.llm.model || '',
})

async function fetchProjectModels(): Promise<void> {
  pFetching.value = true
  try {
    const r = await api.models(effLlm())
    pModels.value = r.models
    message.success(tFmt('已获取 {p0} 个模型：在「模型」框下拉选择或输入', { p0: r.models.length }))
  } catch (e: any) { message.error(e.message) }
  finally { pFetching.value = false }
}

// 仓库平台：显式选择优先，否则按 host 关键词识别（与后端 detect_repo 同规则）
const repoPlatform = computed(() => {
  if (pDraft.repo_provider) return pDraft.repo_provider.toUpperCase()
  const m = /^https?:\/\/([^/]+)/.exec(pDraft.repo_url.trim())
  if (!m) return ''
  const host = m[1].toLowerCase()
  const hit = ['github', 'gitee', 'gitlab'].find(k => host.includes(k))
  return hit ? hit.toUpperCase() : ''
})
// 选项标签随语言切换更新（tNow 一次性取值，须包 computed）
const repoProviderOptions = computed(() => [
  { label: t('自动识别（按 URL）'), value: '' },
  { label: 'GitHub', value: 'github' },
  { label: 'Gitee', value: 'gitee' },
  { label: t('GitLab（含自托管）'), value: 'gitlab' },
  { label: t('Gitea（含自托管）'), value: 'gitea' },
])
// 编辑对话框里回显上次自动拉取结果（状态来自 /api/state 的 issue_auto_pull）
const repoAutoPullHint = computed(() => {
  const p = ui.projectDlg?.original
    ? store.state?.projects.find(x => x.name === ui.projectDlg?.original) : null
  const s = p?.issue_auto_pull
  if (pDraft.repo_auto_pull <= 0) return '未启用自动拉取'
  if (!s) return '尚未自动拉取（保存后生效）'
  if (s.last_error) return `上次失败：${s.last_error}`
  if (s.imported || s.skipped) return `上次：导入 ${s.imported}，跳过 ${s.skipped}`
  return '上次：无新 Issue'
})

async function testProjectModel(): Promise<void> {
  const eff = effLlm()
  if (!eff.model) { message.warning('请先填写或选择模型名'); return }
  pTesting.value = true
  try {
    const r = await api.testModel(eff)
    message.success(r.message || t('连接成功'))
  } catch (e: any) { message.error(e.message) }
  finally { pTesting.value = false }
}

// ---- 审查 Agent：模型列表拉取 / 与任务模型的互斥校验 ----
const rModels = ref<ModelInfo[]>([])
const rFetching = shallowRef(false)
const rModelOptions = computed(() => rModels.value.map(m => ({ label: m.id, value: m.id })))

// ---- Team 成员可用模型候选池（仅动态整合全局大模型、项目模型、预设池模型、审查模型与在线拉取列表，不写死任何硬编码预设） ----
const availableTeamModelOptions = computed(() => {
  const set = new Set<string>()
  if (store.state?.llm?.model) set.add(store.state.llm.model)
  if (pDraft.ll_model?.trim()) set.add(pDraft.ll_model.trim())
  for (const m of pModels.value) {
    if (m.id) set.add(m.id)
  }
  for (const p of Object.values(store.state?.agent_profiles || {})) {
    if (p.llm?.model) set.add(p.llm.model)
  }
  if (store.state?.review_cfg?.model) set.add(store.state.review_cfg.model)
  if (pDraft.review_ll_model?.trim()) set.add(pDraft.review_ll_model.trim())
  for (const m of rModels.value) {
    if (m.id) set.add(m.id)
  }
  return Array.from(set).map(id => ({ label: id, value: id }))
})

function getMemberModelPlaceholder(m: TeamMemberDraft): string {
  if (m.profile && store.state?.agent_profiles?.[m.profile]?.llm?.model) {
    return `继承预设（${store.state?.agent_profiles?.[m.profile]?.llm?.model}）`
  }
  if (pDraft.use_llm && pDraft.ll_model?.trim()) {
    return `继承项目（${pDraft.ll_model.trim()}）`
  }
  if (store.state?.llm?.model) {
    return `继承全局（${store.state.llm.model}）`
  }
  return '留空继承预设/全局'
}

const memberFetchingId = shallowRef<string | null>(null)

async function fetchMemberModels(m: TeamMemberDraft): Promise<void> {
  memberFetchingId.value = m.id
  try {
    const prof = m.profile ? store.state?.agent_profiles?.[m.profile] : null
    const provider = prof?.llm?.provider || pDraft.ll_provider || store.state?.llm.provider || 'openai'
    const baseUrl = (m.ll_base || '').trim() || (prof?.llm?.base_url || '').trim() || pDraft.ll_base.trim() || store.state?.llm.base_url || ''
    const apiKey = (m.ll_key || '').trim() || (prof?.llm?.api_key || '').trim() || pDraft.ll_key.trim()
    const r = await api.models({
      provider,
      base_url: baseUrl,
      api_key: apiKey,
      model: m.ll_model || prof?.llm?.model || pDraft.ll_model || store.state?.llm.model || '',
    })
    for (const item of r.models) {
      if (item.id && !pModels.value.some(existing => existing.id === item.id)) {
        pModels.value.push(item)
      }
    }
    message.success(tFmt('已为角色「{p0}」获取 {p1} 个模型，可在专属模型下拉框选择', { p0: m.name || m.role || m.id, p1: r.models.length }))
  } catch (e: any) {
    message.error(e.message || t('获取模型列表失败'))
  } finally {
    memberFetchingId.value = null
  }
}

function getMemberRoleTemplate(role: string): string {
  if (store.state?.role_prompt_templates?.[role]) {
    return store.state.role_prompt_templates[role]
  }
  const builtIn = store.state?.built_in_roles?.[role]
  if (builtIn?.default_prompt_template) {
    return builtIn.default_prompt_template
  }
  return ''
}

function loadMemberTemplate(m: TeamMemberDraft): void {
  const tpl = getMemberRoleTemplate(m.role || m.id)
  if (!tpl) {
    message.warning(t('未找到该角色的内置模版'))
    return
  }
  m.extra_prompt = tpl
  message.success(tFmt('已为角色「{p0}」载入内置模版', { p0: m.name || m.role || m.id }))
}
// 生效任务模型（项目绑定优先，回退全局）：审查模型与之同名即「自审自批」禁止
const effTaskModel = computed(() => {
  if (pDraft.use_llm && pDraft.ll_model.trim()) return pDraft.ll_model.trim()
  return store.state?.llm.model || ''
})
// 生效审查模型（项目绑定优先，回退全局审查配置）：互斥校验与占位提示共用
const effReviewModel = computed(() => {
  if (!pDraft.use_review) return ''
  if (pDraft.review_ll_model.trim()) return pDraft.review_ll_model.trim()
  return store.state?.review_cfg?.model || ''
})
const reviewModelConflict = computed(() => {
  if (!effReviewModel.value || !effTaskModel.value) return ''
  return effReviewModel.value.trim().toLowerCase() === effTaskModel.value.trim().toLowerCase()
    ? '审查模型不能与任务模型相同（独立模型交叉审查，禁止自审自批）' : ''
})
const reviewKeyPlaceholder = computed(() => {
  const p = ui.projectDlg?.original
    ? store.state?.projects.find(x => x.name === ui.projectDlg?.original) : null
  return p?.review_llm?.api_key_masked
    ? `已配置（${p.review_llm.api_key_masked}），留空保持原值`
    : '审查模型服务密钥'
})

async function fetchReviewModels(): Promise<void> {
  rFetching.value = true
  try {
    const r = await api.models({
      provider: pDraft.review_ll_provider || store.state?.review_cfg?.provider || 'openai',
      base_url: pDraft.review_ll_base.trim() || store.state?.review_cfg?.base_url || '',
      api_key: pDraft.review_ll_key.trim(),
      model: pDraft.review_ll_model.trim() || store.state?.review_cfg?.model || '',
    })
    rModels.value = r.models
    message.success(tFmt('已获取 {p0} 个模型：在「模型」框下拉选择或输入', { p0: r.models.length }))
  } catch (e: any) { message.error(e.message) }
  finally { rFetching.value = false }
}

// ---- 结构化审查意见解析：从任务正文「## 审查结果」节提取 - [severity] file:line — msg ----
interface ReviewIssueVm { severity: string; loc: string; message: string }
const reviewIssues = computed<ReviewIssueVm[]>(() => {
  const body = tDraft.description || ''
  const m = /## 审查结果([\s\S]*?)(?=\n## |$)/.exec(body)
  if (!m) return []
  const out: ReviewIssueVm[] = []
  for (const line of m[1].split('\n')) {
    const hit = /^\s*-\s*\[(blocker|major|minor)\]\s*(.+?)\s+—\s+(.+)$/.exec(line.trim())
    if (hit) out.push({ severity: hit[1], loc: hit[2], message: hit[3] })
  }
  return out
})

// 新建项目时按路径自动探测技术栈预填（防抖 400ms，输入完整路径后触发）；
// 编辑项目不探测——AGENT.md 已存在，保存时不会重写
let probeTimer: ReturnType<typeof setTimeout> | undefined
watch(() => pDraft.path, (path) => {
  if (ui.projectDlg?.original) return
  clearTimeout(probeTimer)
  const p = path.trim()
  if (!p) return
  probeTimer = setTimeout(async () => {
    try {
      const r = await api.probe(p)
      pDraft.stack = r.stack; pDraft.run = r.run; pDraft.test = r.test
    } catch { /* 目录不存在等情况：留空等用户手填 */ }
  }, 400)
})

function browsePath(): void {
  ui.browserPick = (path: string) => { pDraft.path = path }
  ui.browserStart = pDraft.path
  ui.browserDlg = true
}

function parsePremergeTestCommand(): string[] | null {
  const raw = pDraft.premerge_test_command.trim()
  if (!raw) return null
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed) || parsed.length === 0
        || parsed.some(part => typeof part !== 'string' || !part.trim() || part.includes('\0'))) {
      return null
    }
    return parsed as string[]
  } catch {
    return null
  }
}

async function saveProject(): Promise<void> {
  if (!ui.projectDlg) return
  if (pDraft.use_repo && !pDraft.repo_url.trim()) {
    pDraft.err = '已启用仓库绑定，请填写仓库 URL'
    return
  }
  // 审查模型互斥（前端第一道，后端仍强制校验）：同名直接拦截
  if (reviewModelConflict.value) {
    pDraft.err = reviewModelConflict.value
    return
  }
  if (pDraft.premerge_test_command.trim() && !parsePremergeTestCommand()) {
    pDraft.err = t('合并前复测命令必须是非空字符串组成的 JSON argv 数组，例如 ["npm", "test"]')
    return
  }
  if (pDraft.premerge_test_enabled && !parsePremergeTestCommand()) {
    pDraft.err = t('已启用合并前复测，请填写有效的 JSON argv 数组，例如 ["npm", "test"]')
    return
  }
  try {
    await api.projectSave({
      name: pDraft.name.trim(), path: pDraft.path.trim(),
      tasks_dir: pDraft.tasks_dir.trim() || 'tasks',
      guide_file: pDraft.guide_file.trim() || 'AGENT.md',
      max_tasks_per_cycle: pDraft.max_tasks,
      max_agent_turns: pDraft.max_turns,
      allowed_branch: pDraft.branch.trim() || undefined,
      allowed_commands: pDraft.cmds.split(/[,，]/).map(s => s.trim()).filter(Boolean) || undefined,
      enabled: pDraft.enabled,
      // 专属模型绑定：关=清除（null）；开=只提交非空字段（其余继承全局，后端清洗）。
      // api_key 三态：未触碰不带键（后端保持原值）；触碰过按输入上报，空串=清除回继承全局
      llm: pDraft.use_llm ? {
        ...(pDraft.ll_provider ? { provider: pDraft.ll_provider } : {}),
        ...(pDraft.ll_base.trim() ? { base_url: pDraft.ll_base.trim() } : {}),
        ...(pDraft.ll_protocol ? { tool_protocol: pDraft.ll_protocol } : {}),
        ...(llKeyTouched.value ? { api_key: pDraft.ll_key.trim() } : {}),
        ...(pDraft.ll_model.trim() ? { model: pDraft.ll_model.trim() } : {}),
      } : null,
      // 远程仓库绑定：关或 URL 空=清除；平台显式选择优先，否则后端按 URL 识别；
      // 自动拉取间隔分钟换算为秒（0=仅手动）。token/password 同三态（H2 脱敏配套）
      repo: pDraft.use_repo && pDraft.repo_url.trim() ? {
        url: pDraft.repo_url.trim(),
        ...(pDraft.repo_provider ? { provider: pDraft.repo_provider } : {}),
        ...(repoTokenTouched.value ? { token: pDraft.repo_token.trim() } : {}),
        ...(pDraft.repo_user.trim() ? { username: pDraft.repo_user.trim() } : {}),
        ...(repoPassTouched.value ? { password: pDraft.repo_pass } : {}),
        ...(pDraft.repo_auto_pull > 0
          ? { auto_pull_interval: Math.max(1, Math.round(pDraft.repo_auto_pull)) * 60 } : {}),
      } : null,
      // 工具开关覆盖：关=清除（null，跟随全局）；开=全量布尔 map（后端逐项覆盖全局）
      tools: pDraft.use_tools ? { ...pDraft.tools } : null,
      // 访问权限覆盖：'' = 跟随全局（null 清除覆盖）
      permission_mode: pDraft.perm_mode || null,
      // 审查 Agent：开关（关=跟随全局 null）；模型绑定部分覆盖全局 review.llm，
      // api_key 三态同 llm；返工次数 null=跟随全局
      review_enabled: pDraft.use_review ? true : null,
      review_llm: pDraft.use_review ? {
        ...(pDraft.review_ll_provider ? { provider: pDraft.review_ll_provider } : {}),
        ...(pDraft.review_ll_base.trim() ? { base_url: pDraft.review_ll_base.trim() } : {}),
        ...(pDraft.review_ll_protocol ? { tool_protocol: pDraft.review_ll_protocol } : {}),
        ...(reviewKeyTouched.value ? { api_key: pDraft.review_ll_key.trim() } : {}),
        ...(pDraft.review_ll_model.trim() ? { model: pDraft.review_ll_model.trim() } : {}),
      } : null,
      review_max_rounds: pDraft.review_rounds,
      debug_allowed_hosts: pDraft.debug_hosts_text.trim()
        ? pDraft.debug_hosts_text.split(/[\n,;]+/).map(s => s.trim()).filter(Boolean)
        : null,
      commit_and_merge: pDraft.use_cam ? pDraft.cam_enabled : null,
      premerge_test_enabled: pDraft.premerge_test_enabled,
      premerge_test_command: parsePremergeTestCommand(),
      git_author_name: pDraft.git_author_name?.trim() || null,
      git_author_email: pDraft.git_author_email?.trim() || null,
      team: pDraft.team.enabled ? {
        enabled: true,
        execution_mode: pDraft.team.execution_mode,
        max_qa_rounds: pDraft.team.max_qa_rounds,
        max_split_depth: pDraft.team.max_split_depth,
        chain_within_cycle: pDraft.team.chain_within_cycle,
        max_chain_steps_per_cycle: pDraft.team.max_chain_steps_per_cycle,
        fallback_role: pDraft.team.fallback_role.trim() || undefined,
        members: pDraft.team.members.map(m => {
          const mObj: any = {
            id: m.id.trim(),
            profile: m.profile.trim() || undefined,
            enabled: m.enabled !== false,
            name: m.name.trim() || undefined,
            role: m.role.trim() || undefined,
            description: m.description.trim() || undefined,
            allow_test_scripts: m.allow_test_scripts,
            extra_prompt: m.extra_prompt.trim() || undefined,
          }
          if (m.ll_model.trim() || m.ll_protocol || m.ll_base.trim() || teamMemberKeyTouched[m.id]) {
            const mLlm: any = {}
            if (m.ll_model.trim()) mLlm.model = m.ll_model.trim()
            if (teamMemberKeyTouched[m.id]) mLlm.api_key = m.ll_key.trim()
            if (m.ll_protocol) mLlm.tool_protocol = m.ll_protocol
            if (m.ll_base.trim()) mLlm.base_url = m.ll_base.trim()
            mObj.llm = mLlm
          }
          return mObj
        }),
      } : null,
      original_name: ui.projectDlg.original ?? undefined,
      // 新建项目时把技术栈三项写进 AGENT.md；编辑项目不传（指引已存在不重写）
      ...(ui.projectDlg.original ? {} : {
        stack: pDraft.stack.trim(), run: pDraft.run.trim(), test: pDraft.test.trim(),
      }),
    })
    message.success(ui.projectDlg.original ? t('已修改项目') : t('已新建项目'))
    ui.projectDlg = null
    navigateTo('projects')
  } catch (e: any) { pDraft.err = e.message }
  void refresh(true)
}

// ---------- 任务对话框（open 可编辑；归档/非待办以只读模式查看） ----------
const tVisible = computed({
  get: () => !!ui.taskDlg,
  set: (v: boolean) => { if (!v) ui.taskDlg = null },
})
// 关闭时 ui.taskDlg 被置 null，computed 会立刻翻回非只读态，导致收起动画期间
// 按钮/提示/标题闪变——打开时快照到 ref，动画期间保持最后状态
const tReadonly = shallowRef(false)
const tTitle = shallowRef(t('任务'))
const tDraft = reactive({
  id: '', idLocked: false, title: '', priority: 'P2',
  labels: '', assignee: '', description: '', err: '',
  depends_on: [] as string[],
  parent_id: '',
  qa_round: 0,
  dep_state: '',
  // 乐观锁基准（编辑已有任务时由 /api/task 下发；保存时回传 base_version）
  versionToken: '',
})
// 409 冲突后「保留我的修改」重试标记：下次 saveTask 带 force_overwrite=true
const tForceOverwrite = shallowRef(false)
// Phase 1：前置依赖概览（状态 / 是否归档 / 成果摘要），数据来自 /api/task
const depTasks = ref<TaskDepT[]>([])

type DepTagType = 'success' | 'warning' | 'default' | 'error' | 'info'
function depTag(d: TaskDepT): { type: DepTagType; label: string } {
  if (d.missing) return { type: 'error', label: '已删除' }
  const map: Record<string, { type: DepTagType; label: string }> = {
    done: { type: 'success', label: t('已完成') },
    open: { type: 'info', label: t('待执行') },
    in_progress: { type: 'warning', label: t('执行中') },
    blocked: { type: 'error', label: t('受阻') },
    cancelled: { type: 'default', label: t('已取消') },
  }
  return map[d.status] || { type: 'default', label: d.status || '未知' }
}
function depSummary(text: string): string {
  const s = (text || '').replace(/\s+/g, ' ').trim()
  return s.length > 180 ? s.slice(0, 180) + '…' : s
}
function openDependency(id: string): void {
  const project = ui.taskDlg?.project
  if (project && id) openTaskView(project, id)
}

// ---- 运行台账（V1-R1）：任务详情内的运行记录与回放（只读） ----
const runList = ref<RunSummaryT[]>([])
const runLoading = ref(false)
const runOpen = ref('')
const runDetail = ref<RunDetailT | null>(null)
const runDetailLoading = ref(false)
const runArtifact = ref('')
const runArtifactName = ref('')
const runError = ref('')
// 目录迁移期间锁定回放（迁移线程可能正移动当前 run 的目录）
const runlogBusy = computed(() => store.state?.runlog_migration?.state === 'running')

async function loadRuns(): Promise<void> {
  const dlg = ui.taskDlg
  if (!dlg?.id) { runList.value = []; return }
  runLoading.value = true
  try {
    const r = await api.runs({ project: dlg.project, task_id: dlg.id, limit: 20 })
    runList.value = r.runs || []
  } catch {
    runList.value = []
  } finally {
    runLoading.value = false
  }
}

async function toggleRun(r: RunSummaryT): Promise<void> {
  if (runlogBusy.value) return
  if (runOpen.value === r.run_id) {
    runOpen.value = ''; runDetail.value = null; runArtifact.value = ''
    return
  }
  runOpen.value = r.run_id
  runArtifact.value = ''; runArtifactName.value = ''
  runDetail.value = null
  runError.value = ''
  runDetailLoading.value = true
  try {
    runDetail.value = await api.runDetail(r.project || ui.taskDlg?.project || '', r.run_id)
  } catch (e: any) {
    runError.value = String(e?.message || e)
  } finally {
    runDetailLoading.value = false
  }
}

async function viewRunArtifact(r: RunSummaryT, name: string): Promise<void> {
  if (runlogBusy.value) return
  try {
    runArtifactName.value = name
    runArtifact.value = await api.runArtifact(r.project || ui.taskDlg?.project || '', r.run_id, name)
  } catch (e: any) {
    runArtifact.value = `读取失败：${e?.message || e}`
  }
}

function runEventLine(e: { type: string; data: Record<string, any> }): string {
  const d = e.data || {}
  switch (e.type) {
    case 'progress': return String(d.line || '')
    case 'tool_call': return `${d.name || '?'}${d.ok ? '' : '（失败）'}${d.intercepted ? '（拦截）' : ''} ${d.ms || 0}ms ${d.preview || ''}`
    case 'tool_result': {
      const r = String(d.result || '')
      return `\n${d.name || '?'} ${d.ok ? '✓' : '✗'}\n${r.length > 1200 ? r.slice(0, 1200) + '\n…（已截断，完整内容见运行目录）' : r}`
    }
    case 'chat': {
      const ls: string[] = [`第 ${d.turn || '?'} 轮模型消息`]
      const c = String(d.content || '')
      if (c) ls.push(c.length > 1600 ? c.slice(0, 1600) + '\n…（已截断）' : c)
      const rs = String(d.reasoning || '')
      if (rs) ls.push('💭 思考：' + (rs.length > 1600 ? rs.slice(0, 1600) + '\n…（已截断）' : rs))
      const tcs = Array.isArray(d.tool_calls) ? d.tool_calls : []
      for (const tc of tcs) ls.push(`→ 计划调用 ${tc.name || '?'} ${(tc.args || '').slice(0, 300)}`)
      return ls.join('\n')
    }
    case 'model_call': return `${d.model || ''} ${d.prompt_tokens || 0}+${d.completion_tokens || 0} tokens`
    case 'review_verdict': return `${d.verdict || ''} ${d.summary || ''}`
    case 'review_start': return `model=${d.model || ''} round=${d.round || 0}`
    case 'risk_assess': return `${d.level || ''} ${d.policy || ''} ${d.files || 0} files +${d.additions || 0}/-${d.deletions || 0} ${d.digest || ''}`.trim()
    case 'risk_approval_request': return `${d.level || ''} digest=${d.digest || ''} timeout=${d.timeout_minutes || 0}m`
    case 'risk_approval_decision': return `${d.verdict || ''} ${d.level || ''} ${d.by || ''}`.trim()
    case 'commit': return `${d.hash || ''} @ ${d.branch || ''}`
    case 'merge': return `${d.mode || ''} ${d.review_verdict || ''}`
    case 'run_end': return `${d.outcome || ''} / ${d.exit_reason || ''}`
    case 'run_start': return `${d.role || ''} ${d.model || ''}`
    default: return JSON.stringify(d).slice(0, 120)
  }
}
const PRI_OPTIONS = ['P0', 'P1', 'P2', 'P3', 'P4']
const priOptions = computed(() => {
  const opts = PRI_OPTIONS.map(v => ({ label: v, value: v }))
  if (tDraft.priority && !PRI_OPTIONS.includes(tDraft.priority))
    opts.push({ label: tDraft.priority, value: tDraft.priority })
  return opts
})

const activeTaskProject = computed(() =>
  ui.taskDlg ? store.state?.projects.find(x => x.name === ui.taskDlg?.project) ?? null : null)

const projectTaskOptions = computed(() => {
  if (!activeTaskProject.value) return []
  const currentId = tDraft.id
  return (activeTaskProject.value.tasks || [])
    .filter(tk => tk.id !== currentId)
    .map(tk => ({
      label: `[${tk.priority}] ${tk.title || tk.id} (${tk.status === 'done' ? t('已完成') : tk.status})`,
      value: tk.id,
    }))
})

const projectAssigneeOptions = computed(() => {
  const p = activeTaskProject.value
  const opts: { label: string; value: string }[] = [
    { label: t('未指派（由 PM 拆解或自动认领）'), value: '' },
  ]
  if (p && p.team_members_effective && p.team_members_effective.length > 0) {
    for (const m of p.team_members_effective) {
      opts.push({
        label: `${m.name} (${m.role})`,
        value: m.id,
      })
    }
  } else {
    opts.push(
      { label: t('项目经理 (pm)'), value: 'pm' },
      { label: t('后端开发 (backend)'), value: 'backend' },
      { label: t('前端开发 (frontend)'), value: 'frontend' },
      { label: t('测试工程师 (qa)'), value: 'qa' },
      { label: t('运维/DevOps (devops)'), value: 'devops' },
      { label: t('UI 视觉审查 (ui_critic)'), value: 'ui_critic' },
      { label: t('安全审计 (security)'), value: 'security' },
    )
  }
  return opts
})

// ---- 编辑草稿本地暂存（移动端远程编辑防丢）：输入防抖 300ms 落 localStorage，
// 异常中断（来电/切后台被杀）重开时提示恢复；保存成功即清除 ----
const DRAFT_PREFIX = 'orcsy_draft_'
const draftKey = computed(() => {
  if (!ui.taskDlg || tReadonly.value) return ''
  return `${DRAFT_PREFIX}${ui.taskDlg.project}_${ui.taskDlg.id || 'new'}`
})
const draftAvailable = shallowRef(false)
let draftTimer = 0

function saveDraft(): void {
  if (!draftKey.value || !ui.taskDlg) return
  // project/id 供整页重载（如切语言）后启动扫描定位重开；id 统一用 'new' 表示新建
  localStorage.setItem(draftKey.value, JSON.stringify({
    project: ui.taskDlg.project, id: ui.taskDlg.id || 'new',
    title: tDraft.title, priority: tDraft.priority, labels: tDraft.labels,
    assignee: tDraft.assignee, description: tDraft.description,
    depends_on: tDraft.depends_on, at: Date.now(),
  }))
}
function clearDraft(): void {
  if (draftKey.value) localStorage.removeItem(draftKey.value)
  draftAvailable.value = false
}
function restoreDraft(): void {
  try {
    const raw = draftKey.value ? localStorage.getItem(draftKey.value) : null
    if (!raw) return
    const d = JSON.parse(raw) as Record<string, any>
    if (typeof d.title === 'string') tDraft.title = d.title
    if (typeof d.priority === 'string') tDraft.priority = d.priority
    if (typeof d.labels === 'string') tDraft.labels = d.labels
    if (typeof d.assignee === 'string') tDraft.assignee = d.assignee
    if (typeof d.description === 'string') tDraft.description = d.description
    if (Array.isArray(d.depends_on)) tDraft.depends_on = d.depends_on
    draftAvailable.value = false
    message.success(t('已恢复未保存的草稿'))
  } catch { /* 草稿损坏按无草稿处理 */ }
}
function discardDraft(): void {
  clearDraft()
  message.success(t('已丢弃草稿'))
}
// 弹窗内容变化防抖落草稿；首次加载（watch 里填值）不触发——用 pending 标记
let draftPaused = true
watch(tDraft, () => {
  if (draftPaused || !draftKey.value) return
  window.clearTimeout(draftTimer)
  draftTimer = window.setTimeout(saveDraft, 300)
})

// ---- 切语言整页重载 / 异常中断后的草稿找回 ----
// ① beforeunload 同步落盘：防抖窗口（300ms）内的最后输入在页面卸载瞬间不丢；
// ② 启动时扫描未恢复草稿（带重开上下文的最新一条），提示「继续编辑」——
//    点击后打开对应任务弹窗，弹窗内自动出现既有「恢复」条（打开时检查草稿）
onMounted(() => {
  window.addEventListener('beforeunload', () => {
    if (ui.taskDlg && !tReadonly.value && !draftPaused && draftKey.value) {
      window.clearTimeout(draftTimer)
      saveDraft()
    }
  })
  // 延后一拍再扫：避开首屏渲染与初始加载提示
  window.setTimeout(() => {
    try {
      let found: { project: string; id: string; at: number } | null = null
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i)
        if (!k || !k.startsWith(DRAFT_PREFIX)) continue
        let d: Record<string, any>
        try { d = JSON.parse(localStorage.getItem(k) || '{}') } catch { continue }
        // 只提示带重开上下文的草稿（project/id 字段本次新增；旧草稿仍走弹窗内手动恢复）
        if (typeof d.project !== 'string' || !d.project) continue
        const at = typeof d.at === 'number' ? d.at : 0
        if (!found || at > found.at) found = { project: d.project, id: String(d.id ?? 'new'), at }
      }
      if (!found) return
      const target = found
      window.$dialog?.warning({
        title: t('检测到未完成的任务草稿'),
        content: tFmt('项目「{p0}」有一份未保存的任务草稿（{p1}），是否继续编辑？',
          { p0: target.project, p1: target.id === 'new' ? t('新建任务') : target.id }),
        positiveText: t('继续编辑'),
        negativeText: t('稍后再说'),
        onPositiveClick() {
          ui.taskDlg = target.id === 'new'
            ? { project: target.project, id: null }
            : { project: target.project, id: target.id }
        },
      })
    } catch { /* 扫描失败按无草稿处理 */ }
  }, 800)
})

// ---- 移动端 Markdown 快捷工具条：一键插入常用标记，光标定位插入点 ----
const descInputRef = ref<any>(null)

async function mdInsert(before: string, after = ''): Promise<void> {
  const el: HTMLTextAreaElement | undefined = descInputRef.value?.textareaElRef
    ?? descInputRef.value?.$el?.querySelector?.('textarea')
  if (!el) return
  const s = el.selectionStart ?? tDraft.description.length
  const e = el.selectionEnd ?? s
  const sel = tDraft.description.slice(s, e)
  const insert = `${before}${sel}${after}`
  tDraft.description = tDraft.description.slice(0, s) + insert + tDraft.description.slice(e)
  await nextTick()
  el.focus()
  const caret = s + before.length + sel.length
  el.setSelectionRange(caret, caret)
}

watch(() => ui.taskDlg, async (dlg) => {
  if (!dlg) return
  tReadonly.value = !!dlg.readonly
  tTitle.value = tReadonly.value
    ? `查看任务 · ${dlg.project}/${dlg.id}`
    : `任务 · ${dlg.project}/${dlg.id || '新建'}`
  tDraft.err = ''
  tDraft.id = dlg.id ?? ''
  tDraft.idLocked = dlg.id != null
  tDraft.title = ''; tDraft.priority = 'P2'; tDraft.labels = ''
  tDraft.assignee = ''; tDraft.description = ''
  tDraft.depends_on = []
  tDraft.parent_id = ''
  tDraft.qa_round = 0
  tDraft.dep_state = ''
  tDraft.versionToken = ''
  depTasks.value = []
  tForceOverwrite.value = false
  // 运行记录：重开对话框时清空，避免看到上一个任务的 run
  runList.value = []; runOpen.value = ''; runDetail.value = null
  runArtifact.value = ''; runArtifactName.value = ''; runError.value = ''
  // 打开即暂停草稿落盘（watch 填值期间不当作用户输入），填完再恢复
  draftPaused = true
  draftAvailable.value = false
  if (dlg.id) {
    // 先用看板缓存填，再拉全文覆盖（/api/state 描述截断 2000 字）；
    // 归档任务不在 tasks 里，从 archived 缓存找，最终以 /api/task 的全文为准
    const p = store.state?.projects.find(x => x.name === dlg.project)
    const t = p?.tasks.find(x => x.id === dlg.id) || p?.archived.find(x => x.id === dlg.id)
    if (t) {
      tDraft.title = t.title; tDraft.priority = t.priority
      tDraft.labels = t.labels.join(', '); tDraft.assignee = t.assignee
      tDraft.description = t.description
      tDraft.depends_on = t.depends_on ? [...t.depends_on] : []
      tDraft.parent_id = t.parent_id || ''
      tDraft.qa_round = t.qa_round || 0
      tDraft.dep_state = t.dep_state || ''
      // 首屏先用缓存拼出依赖概览（API 返回后覆盖为带成果摘要的完整数据）
      depTasks.value = (t.depends_on || []).map((id: string) => {
        const d = p?.tasks.find(x => x.id === id) || p?.archived.find(x => x.id === id)
        return {
          id, title: d?.title || '', status: d?.status || 'missing',
          assignee: d?.assignee || '', archived: !!p?.archived.find(x => x.id === id),
          missing: !d, summary: '', qa_round: d?.qa_round || 0,
          parent_id: d?.parent_id || '',
        }
      })
    }
    try {
      const r = await api.taskFull(dlg.project, dlg.id)
      // 归档任务可能不在 /api/state 的最近归档缓存里：标题/优先级/指派一并从全文补齐
      if (r.title) tDraft.title = r.title
      if (r.priority) tDraft.priority = r.priority
      if (r.assignee) tDraft.assignee = r.assignee
      if (r.labels) tDraft.labels = r.labels.join(', ')
      if (r.description) tDraft.description = r.description
      if (r.depends_on) tDraft.depends_on = [...r.depends_on]
      if (r.dep_tasks) depTasks.value = r.dep_tasks
      if (r.parent_id) tDraft.parent_id = r.parent_id
      if (r.qa_round) tDraft.qa_round = r.qa_round
      if (r.error) tDraft.err = `注意：任务文件解析有问题 —— ${r.error}`
      // 版本快照：编辑保存时回传 base_version 触发乐观锁比对
      tDraft.versionToken = r.version_token || ''
    } catch { /* 拉不到就用缓存 */ }
  }
  // 有未保存草稿则提示恢复（防来电/切后台杀进程丢输入）
  try {
    const raw = draftKey.value ? localStorage.getItem(draftKey.value) : null
    draftAvailable.value = !!(raw && !tReadonly.value)
  } catch { draftAvailable.value = false }
  // 运行台账：已有任务才拉取（新建任务尚无 run）
  if (dlg.id) void loadRuns()
  await nextTick()
  draftPaused = false
})

async function saveTask(force = false): Promise<void> {
  if (!ui.taskDlg || tReadonly.value) return
  if (force) tForceOverwrite.value = true
  try {
    // id 留空 = 后端自动生成「优先级-标题」；title 留空则报错
    await api.taskSave({
      project: ui.taskDlg.project, id: tDraft.id.trim(),
      title: tDraft.title.trim() || tDraft.id.trim(),
      priority: tDraft.priority, status: 'open',
      labels: tDraft.labels.split(/[,，]/).map(s => s.trim()).filter(Boolean),
      assignee: tDraft.assignee.trim(),
      depends_on: tDraft.depends_on,
      description: tDraft.description,
      // 乐观锁：编辑已有任务带 base_version 比对；409 后明确选择覆盖才带 force
      ...(tDraft.versionToken && !force ? { base_version: tDraft.versionToken } : {}),
      ...(force ? { force_overwrite: true } : {}),
    })
    message.success(t('任务已保存'))
    clearDraft()
    ui.taskDlg = null
  } catch (e: any) {
    if (e instanceof ConflictError) {
      tDraft.err = ''
      const latest: Record<string, any> = e.latestTask || {}
      ;(window as any).$dialog?.warning({
        title: t('任务已被修改'),
        content: `${e.message}\n\n当前最新状态：[${latest.priority || '?'}] ${latest.title || latest.id || ''}`
          + tFmt('（{p0}）。\n\n选择「保留我的修改」将覆盖最新内容；「采用最新状态」会放弃本页未保存的输入。', { p0: latest.status || '?' }),
        positiveText: t('保留我的修改'),
        negativeText: t('采用最新状态'),
        onPositiveClick() { void saveTask(true) },
        onNegativeClick() {
          // 载入最新内容：放弃本页输入，以服务端最新为准
          if (latest.title != null) tDraft.title = latest.title
          if (latest.priority != null) tDraft.priority = latest.priority
          if (latest.description != null) tDraft.description = latest.description
          if (Array.isArray(latest.depends_on)) tDraft.depends_on = [...latest.depends_on]
          if (latest.assignee != null) tDraft.assignee = latest.assignee
          if (latest.labels) {
            tDraft.labels = Array.isArray(latest.labels)
              ? latest.labels.join(', ') : String(latest.labels)
          }
          tDraft.versionToken = latest.version_token || ''
          tForceOverwrite.value = false
          tDraft.err = '已载入最新任务内容，请核对后再保存'
        },
      })
      return
    }
    tDraft.err = e.message
  }
  void refresh(true)
}

// ---------- 手机连接向导（移动端远程访问） ----------
// 枚举本机可达地址（局域网直连/公网域名/回环）+ 扫码二维码；后端 /api/system/endpoints
// 免登录，二维码 /api/system/qr 需登录（桌面端此刻必然已登录）。
// 仅监听 127.0.0.1 时：不渲染打不开的回环二维码，改为展示「开放手机访问」操作指引
const cVisible = computed({
  get: () => ui.connectDlg,
  set: (v: boolean) => { ui.connectDlg = v },
})
const cEndpoints = shallowRef<{ name: string; url: string; active?: boolean }[]>([])
const cAuthRequired = shallowRef(false)
const cAllLoopback = shallowRef(false)
const cHint = shallowRef('')
const cLoading = shallowRef(false)
const cQrSvg = shallowRef('')
const cSelected = shallowRef('')
const cEnableChecked = shallowRef(false)

watch(() => ui.connectDlg, async (show) => {
  if (!show) return
  cLoading.value = true
  cQrSvg.value = ''
  cSelected.value = ''
  try {
    const r = await api.systemEndpoints()
    cEndpoints.value = r.endpoints || []
    cAuthRequired.value = r.auth_required
    cAllLoopback.value = r.all_loopback === true
    cHint.value = r.hint || ''
    cPasswordConfigured.value = r.password_configured === true
    // 二维码默认地址：公网域名 → 可达局域网地址；仅回环可达时不选（改出指引卡片）
    const actives = cEndpoints.value.filter(e => e.active !== false)
    const prefer = actives.find(e => e.url.startsWith('https://'))
      || actives.find(e => !e.url.includes('127.0.0.1'))
    await selectEndpoint(prefer?.url || '')
  } catch (e: any) { message.error(e.message) }
  finally { cLoading.value = false }
})

async function selectEndpoint(url: string): Promise<void> {
  cSelected.value = url
  cQrSvg.value = ''
  if (!url) return
  try { cQrSvg.value = await api.systemQr(url) } catch { cQrSvg.value = '' }
}

function clickEndpoint(e: { name: string; url: string; active?: boolean }): void {
  if (e.active === false) {
    message.warning(t('该地址未开放：可勾选上方「一键开放手机访问」自动改配置并重启。'))
    return
  }
  void selectEndpoint(e.url)
}

// ---- 一键开放手机访问：打勾 → 确认 → 改配置落盘（host=0.0.0.0 + 密码）→ 自动重启 ----
// 复用 /api/config（settings_service 落盘 + PBKDF2 密文存储）与 /api/app-restart +
// 自更新同款「重启等待」UI（遮罩 + 应用回归自动 reload）。生成的访问密码暂存
// sessionStorage：重启 reload 后登录页横幅再展示一次（配置里只存密文，不可找回）
const cEnableBusy = shallowRef(false)
const cEnabled = shallowRef(false)      // 配置已保存（等待重启生效）
const cPasswordConfigured = shallowRef(false)
const cEnableNote = shallowRef('')

function genPassword(): string {
  // 无歧义字母表（去 0/O/1/l/I）：手机手抄也认得出的 12 位强密码（CSPRNG）
  const chars = '23456789abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ'
  const rnd = crypto.getRandomValues(new Uint32Array(12))
  return Array.from(rnd, n => chars[n % chars.length]).join('')
}

function onEnableToggle(on: boolean): void {
  if (!on || cEnableBusy.value || cEnabled.value) return
  // 未配置登录密码时自动生成一个（沿用现有密码时留空 = 不改）
  const pwd = cPasswordConfigured.value ? '' : genPassword()
  // 两种分支都走翻译；未配置密码时把一次性密码作为参数注入，避免普通引号下的字面量 ${pwd}
  const body = cPasswordConfigured.value
    ? t('看板将监听所有网络接口（web.host: 0.0.0.0），并沿用现有登录密码。')
    : tFmt('看板将绑定全部网卡（web.host: 0.0.0.0），并设置访问密码：\n\n{p0}\n\n请先复制保存该密码（手机登录要用；配置里只存密文，此弹窗关闭后不可再查看，重启后登录页会再提示一次）。', { p0: pwd })
  window.$dialog?.warning({
    title: t('开放手机局域网访问？'),
    content: body
      + t('\n\n配置保存后应用会自动重启（数秒离线），重启后重新打开本向导即可看到可扫码的局域网地址。'),
    positiveText: t('开启并自动重启'),
    negativeText: t('取消'),
    onPositiveClick() { void doEnable(pwd) },
    onNegativeClick() { cEnableChecked.value = false },
    onClose() { cEnableChecked.value = false },
  })
}

async function doEnable(pwd: string): Promise<void> {
  cEnableBusy.value = true
  try {
    // settings_service 分块保存：只带 web 键，其余配置项保持原值不动
    await api.config({ web: { host: '0.0.0.0', ...(pwd ? { auth_password: pwd } : {}) } })
    cEnabled.value = true
    cEnableNote.value = ''
    if (pwd) {
      // 新密码只此一次明文：暂存 sessionStorage，重启 reload 后登录页横幅再提示
      sessionStorage.setItem('orcsyMobilePwd', pwd)
    }
    try {
      await api.restartApp()
      beginRestartWait()  // 遮罩 + 探测应用回归后自动 reload（登录页横幅提示密码）
    } catch (e: any) {
      // 源码/Docker 部署不支持程序内重启：配置已落盘，指引用户手动重启
      cEnableNote.value = '配置已保存，但程序内重启不可用（'
        + (e.message || '当前部署方式') + '）——请手动重启 Orcsy 后生效。'
    }
  } catch (e: any) {
    cEnableChecked.value = false
    message.error(e.message || t('开启失败'))
  } finally { cEnableBusy.value = false }
}

async function copyEndpoint(url: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(url)
    message.success(t('已复制访问地址'))
  } catch { message.error('复制失败，请长按地址手动复制') }
}

// ---------- 人工把关提交 ----------
// 已改为独立「人工把关提交」页（views/CommitView.vue，路由 /#/commit?project=xxx）：
// 左侧改动文件清单 + 右侧 diff 对比。入口 openCommitDlg() 直接路由跳转，不在此弹窗。

// ---------- AGENT.md 编辑 ----------
const gVisible = computed({
  get: () => !!ui.guideDlg,
  set: (v: boolean) => { if (!v) ui.guideDlg = null },
})
const gDraft = reactive({ file: '', content: '', err: '' })
watch(() => ui.guideDlg, async (dlg) => {
  if (!dlg) return
  gDraft.err = ''; gDraft.content = ''; gDraft.file = ''
  try {
    const r = await api.guide(dlg.project)
    gDraft.file = r.file; gDraft.content = r.content
  } catch (e: any) { gDraft.err = e.message }
})

async function saveGuide(): Promise<void> {
  if (!ui.guideDlg) return
  try {
    await api.guideSave(ui.guideDlg.project, gDraft.content)
    message.success(t('指引已保存'))
    ui.guideDlg = null
  } catch (e: any) { gDraft.err = e.message }
  void refresh(true)
}

// ---------- 技能 新建/编辑（内容存于技能库，不落项目目录） ----------
const kVisible = computed({
  get: () => !!ui.skillDlg,
  set: (v: boolean) => { if (!v) ui.skillDlg = null },
})
const kDraft = reactive({
  original: null as string | null,
  name: '', description: '', scope: 'global', enabled: true, body: '', err: '',
})
const scopeOptions = computed(() => [
  { label: t('全局（所有项目生效）'), value: 'global' },
  ...(store.state?.projects || []).map(p => ({ label: tFmt('仅 {p0}', { p0: p.name }), value: p.name })),
])
watch(() => ui.skillDlg, async (dlg) => {
  if (!dlg) return
  kDraft.err = ''; kDraft.original = dlg.original
  kDraft.name = ''; kDraft.description = ''; kDraft.scope = 'global'
  kDraft.enabled = true; kDraft.body = ''
  if (dlg.original) {
    try {
      const r = await api.skillGet(dlg.original)
      kDraft.name = r.name; kDraft.description = r.description
      kDraft.scope = r.scope || 'global'; kDraft.enabled = r.enabled
      kDraft.body = r.body
      if (r.error) kDraft.err = `注意：SKILL.md 解析有问题 —— ${r.error}`
    } catch (e: any) { kDraft.err = e.message }
  }
})

async function saveSkill(): Promise<void> {
  if (!ui.skillDlg) return
  try {
    await api.skillSave({
      original_name: kDraft.original ?? undefined,
      name: kDraft.name.trim(),
      description: kDraft.description.trim(),
      scope: kDraft.scope,
      enabled: kDraft.enabled,
      body: kDraft.body,
    })
    message.success(kDraft.original ? t('技能已保存') : t('技能已新建'))
    ui.skillDlg = null
  } catch (e: any) { kDraft.err = e.message }
  void refresh(true)
}

// ---------- 扫描 AI 工具技能（批量勾选导入） ----------
interface ScanSkill { name: string; description: string; path: string; exists: boolean }
interface ScanSource { tool: string; path: string; skills: ScanSkill[] }

const scanVisible = computed({
  get: () => ui.skillScanDlg,
  set: (v: boolean) => { ui.skillScanDlg = v },
})
const scanSources = ref<ScanSource[]>([])
const scanChecked = ref<Record<string, boolean>>({})
const scanning = shallowRef(false)
const importing = shallowRef(false)
const scanErr = shallowRef('')

const allScanSkills = computed(() => scanSources.value.flatMap(s => s.skills))
const checkedPaths = computed(() =>
  allScanSkills.value
    .filter(k => scanChecked.value[k.path]).map(k => k.path))

// 全选 / 反选：批量勾选多个工具目录下扫描到的技能（已导入的后端会跳过）
function checkAll(): void {
  const checked: Record<string, boolean> = {}
  for (const k of allScanSkills.value) checked[k.path] = true
  scanChecked.value = checked
}
function invertChecked(): void {
  const checked: Record<string, boolean> = {}
  for (const k of allScanSkills.value) checked[k.path] = !scanChecked.value[k.path]
  scanChecked.value = checked
}

watch(scanVisible, async (v) => {
  if (!v) return
  scanErr.value = ''
  scanning.value = true
  scanSources.value = []
  try {
    const r = await api.skillScan()
    scanSources.value = r.sources || []
    // 默认勾选尚未导入的技能
    const checked: Record<string, boolean> = {}
    for (const s of scanSources.value)
      for (const k of s.skills) checked[k.path] = !k.exists
    scanChecked.value = checked
  } catch (e: any) { scanErr.value = e.message }
  finally { scanning.value = false }
})

async function importChecked(): Promise<void> {
  if (!checkedPaths.value.length) return
  importing.value = true
  try {
    const r = await api.skillImportBatch(checkedPaths.value)
    message.success(r.message || t('已导入'))
    ui.skillScanDlg = false
  } catch (e: any) { scanErr.value = e.message }
  finally { importing.value = false }
  void refresh(true)
}

// ---------- 目录选择器 ----------
const bVisible = computed({
  get: () => ui.browserDlg,
  set: (v: boolean) => { ui.browserDlg = v },
})
const bStack = ref<DirListing | null>(null)
const bManual = shallowRef('')
const bNewFolder = shallowRef('')
const bErr = shallowRef('')

async function browseTo(path: string): Promise<void> {
  try {
    bStack.value = await api.browse(path)
    bErr.value = ''
  } catch (e: any) { bErr.value = e.message }
}
watch(bVisible, (v) => {
  if (v) { bManual.value = ui.browserStart; bNewFolder.value = ''; void browseTo(ui.browserStart || '') }
})
function pickPath(): void {
  const v = bManual.value.trim() || bStack.value?.path || ''
  if (v) ui.browserPick?.(v)
  ui.browserDlg = false
}
// 文件选择模式：点击列表里的文件即确认返回（目录仍可进入）
function pickFile(f: string): void {
  ui.browserPick?.(f)
  ui.browserDlg = false
}

// 在当前浏览的目录下新建文件夹，成功后自动进入
async function newFolder(): Promise<void> {
  const name = bNewFolder.value.trim()
  if (!name) { message.warning('请输入文件夹名称'); return }
  const base = bStack.value?.path || ''
  if (!base) { message.warning('请先进入一个盘符或目录'); return }
  const sep = base.endsWith('\\') || base.endsWith('/') ? '' : '\\'
  try {
    const r = await api.mkdir(base + sep + name)
    message.success(t('已创建 ') + r.path)
    bNewFolder.value = ''
    await browseTo(r.path)
  } catch (e: any) { message.error(e.message) }
}
</script>

<template>
  <!-- 项目 新建/编辑 -->
  <NModal v-model:show="pVisible" preset="card" :title="ui.projectDlg?.original ? t('编辑项目') : t('新建项目')"
          class="project-dialog" :class="{ 'project-dialog-mobile': isMobile }"
          :style="isMobile
            ? { width: '100vw', height: viewportHeight + 'px', maxHeight: viewportHeight + 'px' }
            : { width: 'min(800px, calc(100vw - 48px))', maxHeight: 'min(880px, calc(100dvh - 48px))' }"
          :header-style="{ flexShrink: 0 }"
          :content-style="{ flex: '1 1 auto', padding: '0', minHeight: '0', overflowY: 'auto', overscrollBehavior: 'contain' }"
          :footer-style="{ padding: '0', flexShrink: 0 }">
    <div class="project-content">
    <section class="project-basics">
    <h3 class="project-basics-title">{{ t('基础信息') }}</h3>
    <div class="duo">
      <div class="f1"><label>{{ t('项目名（字母/数字/连字符）') }}</label><NInput v-model:value="pDraft.name" placeholder="my-app" /></div>
      <div class="f1"><label>{{ t('每轮最多任务数') }}</label><NInputNumber v-model:value="pDraft.max_tasks" :min="1" :max="50" /></div>
    </div>
    <label>{{ t('项目路径（本地目录）') }}</label>
    <div class="pathline">
      <NInput v-model:value="pDraft.path" placeholder="D:\Repos\my-app" />
      <NButton @click="browsePath">{{ t('选择…') }}</NButton>
    </div>
    <div class="duo">
      <div class="f1"><label>{{ t('任务目录（相对项目根）') }}</label><NInput v-model:value="pDraft.tasks_dir" /></div>
      <div class="f1"><label>{{ t('指引文件') }}</label><NInput v-model:value="pDraft.guide_file" /></div>
    </div>
    </section>
    <ProjectFormSection :title="t('执行设置')" icon="tune" :summary="pDraft.branch || t('工作分支（可选，默认当前分支）')">
    <div class="duo">
      <div class="f1"><label>{{ t('工具循环轮数上限（-1 = 不设上限）') }}</label>
        <NInputNumber v-model:value="pDraft.max_turns" :min="-1" :max="200"
                      :placeholder="t('40；填 -1 不设上限（靠 finish/强制停止兜底）')" /></div>
      <div class="f1"><label>{{ t('工作分支（可选，默认当前分支）') }}</label><NInput v-model:value="pDraft.branch" placeholder="main" /></div>
    </div>
    <label>{{ t('命令白名单（逗号分隔，留空用默认：python/pytest/node/npm/git…）') }}</label>
    <NInput v-model:value="pDraft.cmds" placeholder="python, pytest, npm, dotnet" />
    <template v-if="!ui.projectDlg?.original">
      <label style="margin-top:10px">
        {{ t('技术栈（按路径自动探测预填，可修改；保存时写入 AGENT.md 的 Project 节，留空不填）') }}
      </label>
      <div class="duo">
        <div class="f1"><label>{{ t('技术栈 Stack') }}</label><NInput v-model:value="pDraft.stack" placeholder="Node.js (Vue)" /></div>
        <div class="f1"><label>{{ t('本地运行 Run') }}</label><NInput v-model:value="pDraft.run" placeholder="npm run dev" /></div>
        <div class="f1"><label>{{ t('测试 Test') }}</label><NInput v-model:value="pDraft.test" placeholder="npm test" /></div>
      </div>
    </template>
    <div class="premerge-test" style="margin-top:12px; padding:10px; border:1px solid var(--line); border-radius:6px">
      <label class="checkline">
        <NCheckbox v-model:checked="pDraft.premerge_test_enabled" />
        {{ t('启用合并前复测（项目级 opt-in）') }}
      </label>
      <div class="hint" style="margin-top:6px">
        {{ t('启用后，在隔离候选工作区合入最新已验收基线后重测；主工作区须干净，或仅包含与 stage 一致的已落地增量。测试失败将保留任务分支，等待人工检查与重新入队。') }}
      </div>
      <div v-if="pDraft.premerge_test_enabled || pDraft.premerge_test_command.trim()" style="margin-top:8px">
        <label>{{ t('复测命令 argv（JSON 字符串数组，不使用 shell 解析）') }}</label>
        <NInput v-model:value="pDraft.premerge_test_command" type="textarea" :rows="2"
                placeholder="[&quot;npm&quot;, &quot;test&quot;]" />
      </div>
    </div>
    <label class="checkline" style="margin-top:12px">
      <NSwitch v-model:value="pDraft.use_cam" />
      {{ t('覆盖 Git 代码提交与合并策略') }}（{{ t('关闭 = 跟随全局设置') }}{{ store.state?.git_cfg?.commit_and_merge ? t('开启') : t('关闭') }}）
    </label>
    <template v-if="pDraft.use_cam">
      <label class="checkline" style="margin-top:6px">
        <NSwitch size="small" v-model:value="pDraft.cam_enabled" />
        {{ t('任务完成后自动提交并合并代码（开启 = worktree 分支提交合并；关闭 = 直接保留在工作区）') }}
      </label>
      <div class="frow" style="margin-top:8px">
        <div class="f1"><label>{{ t('提交作者名（留空 = 跟随全局）') }}</label>
          <NInput v-model:value="pDraft.git_author_name" :placeholder="store.state?.git_cfg?.author_name || 'Orcsy Agent'" /></div>
        <div class="f1"><label>{{ t('提交作者邮箱（留空 = 跟随全局）') }}</label>
          <NInput v-model:value="pDraft.git_author_email" :placeholder="store.state?.git_cfg?.author_email || 'orcsy@example.com'" /></div>
      </div>
    </template>
    </ProjectFormSection>
    <ProjectFormSection :title="t('多模型 Agent 项目小组模式 (Team)')" icon="group"
                        :summary="pDraft.team.enabled ? t('小组协同模式已启用') : t('传统单兵模式')">
      <label class="checkline">
        <NSwitch v-model:value="pDraft.team.enabled" />
        {{ t('多模型 Agent 项目小组模式 (Team)') }}
      </label>
        <div v-if="!pDraft.team.enabled" class="hint" style="margin-top:8px">
          {{ t('小组模式当前关闭；开启上方开关后可配置并发策略、QA 回归轮数与成员名册。收起不影响已保存的配置。') }}
        </div>
        <template v-if="pDraft.team.enabled">
        <div style="margin-top:8px; font-size:12px; color:var(--dim); line-height:1.6">
          <i class="ms sm">security</i> <b>{{ t('工作区沙盒已强制启用') }}</b>：{{ t('所有写代码角色按任务在独立 Worktree 沙盒中编码，通过审查后安全合入主分支工作区（未提交状态）') }}，<b>{{ t('默认不自动 commit 与 push') }}</b>，{{ t('最后提交完全由人工把关！') }}
        </div>

        <div v-if="editingProject && !editingProject.is_git_repo" style="margin-top:6px; display:flex; align-items:center; gap:8px;">
          <NTag size="small" type="warning">{{ t('非 Git 目录') }}</NTag>
          <span style="font-size:12px; color:var(--text)">{{ t('Worktree 沙盒隔离需在 Git 仓库运行：') }}</span>
          <NButton size="tiny" type="primary" secondary :loading="gitIniting" @click="onGitInitProject">{{ t('一键初始化 Git 仓库') }}</NButton>
        </div>

        <div class="duo" style="margin-top:8px">
          <div class="f1"><label>{{ t('并发策略') }}</label>
            <NSelect v-model:value="pDraft.team.execution_mode" :options="[
              { label: t('按任务独立 Worktree 安全并发 (推荐)'), value: 'parallel_worktree' },
              { label: t('项目内单任务串行互斥'), value: 'sequential' },
            ]" />
          </div>
          <div class="f1"><label>{{ t('无 PM 时未指派任务兜底角色') }}</label>
            <NSelect v-model:value="pDraft.team.fallback_role" :options="pDraft.team.members.map(m => ({ label: `${m.name || m.id} (${m.role || m.id})`, value: m.id }))" clearable :placeholder="t('留空则需手动指派')" />
          </div>
        </div>

        <div class="duo" style="margin-top:6px">
          <div class="f1"><label>{{ t('QA 最大回归轮数（防死循环熔断）') }}</label>
            <NInputNumber v-model:value="pDraft.team.max_qa_rounds" :min="1" :max="10" />
          </div>
          <div class="f1" style="display:flex; flex-direction:column; justify-content:center;">
            <label class="checkline" style="margin-top:14px">
              <NSwitch size="small" v-model:value="pDraft.team.chain_within_cycle" />
              <span style="font-size:12px">{{ t('同周期链式推进（前置完成立即接棒下一阶段）') }}</span>
            </label>
          </div>
        </div>

        <!-- 小组成员列表 -->
        <div style="margin-top:10px;">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
            <span style="font-weight:600; font-size:12px; color:var(--text)">{{ t('小组成员名册与专属模型配置：') }}</span>
            <NButton size="tiny" quaternary type="primary" @click="addTeamMember">{{ t('+ 添加成员') }}</NButton>
          </div>

          <div style="display:flex; flex-direction:column; gap:8px;">
            <div v-for="(m, mIdx) in pDraft.team.members" :key="m.id" style="border:1px solid var(--line); border-radius:6px; padding:8px 10px; background:var(--card);">
              <div class="tm-head" style="display:flex; justify-content:space-between; align-items:center;">
                <div style="display:flex; align-items:center; gap:6px;">
                  <NSwitch size="small" v-model:value="m.enabled" />
                  <span style="font-weight:600; font-size:12px; display:inline-flex; align-items:center;"><i class="ms sm">{{ getMemberRoleIcon(m.role || m.id) }}</i> {{ m.name || m.id }}</span>
                  <NTag size="tiny" round type="info">{{ (m.role || m.id).toUpperCase() }}</NTag>
                  <NTag size="tiny" round :bordered="false" style="font-family:Consolas, monospace; font-size:10.5px; background:rgba(128,128,160,0.12); color:var(--dim);">id: {{ m.id }}</NTag>
                </div>
                <div style="display:flex; align-items:center; gap:6px;">
                  <NButton size="tiny" quaternary type="error" @click="removeTeamMember(mIdx)">{{ t('删除') }}</NButton>
                </div>
              </div>

              <div v-if="m.enabled" class="tm-grid" style="margin-top:8px; display:grid; grid-template-columns: 1fr 1fr; gap:8px;">
                <div>
                  <label style="font-size:11px">{{ t('引用全局预设') }}</label>
                  <NSelect size="small" v-model:value="m.profile" :options="globalProfileOptions" clearable :placeholder="t('自定义配置')" />
                </div>
                <div>
                  <label style="font-size:11px">{{ t('角色类型') }}</label>
                  <NSelect size="small" v-model:value="m.role" :options="roleSelectOptions" />
                </div>
                <div>
                  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:2px;">
                    <label style="font-size:11px; margin:0">{{ t('专属模型覆盖 (留空继承预设/全局)') }}</label>
                    <NButton size="tiny" quaternary type="primary" :loading="memberFetchingId === m.id" @click="fetchMemberModels(m)" style="font-size:10px; padding:0 2px">{{ t('获取模型列表') }}</NButton>
                  </div>
                  <NSelect size="small" v-model:value="m.ll_model"
                           :options="availableTeamModelOptions"
                           filterable tag clearable
                           :placeholder="getMemberModelPlaceholder(m)"
                           @update:value="(v: string | null) => { m.ll_model = v || '' }" />
                </div>
                <div>
                  <label style="font-size:11px">{{ t('接口 base_url（留空继承预设/项目/全局）') }}</label>
                  <NInput size="small" v-model:value="m.ll_base" placeholder="https://..." />
                </div>
                <div>
                  <label style="font-size:11px">{{ t('API Key 覆盖') }}</label>
                  <NInput size="small" v-model:value="m.ll_key" type="password" show-password-on="click"
                          :placeholder="m.ll_key_masked ? tFmt('已配置({p0})', { p0: m.ll_key_masked }) : t('留空继承')"
                          @update:value="teamMemberKeyTouched[m.id] = true" />
                </div>
              </div>
              <div style="margin-top:6px">
                <label style="font-size:11px">{{ t('工具调用协议（留空 = 跟随全局）') }}</label>
                <NSelect size="small" v-model:value="m.ll_protocol" :options="[
                  { label: t('跟随全局'), value: '' },
                  { label: 'native — 原生 function calling', value: 'native' },
                  { label: 'text — 文本协议兜底（解析器互斥的中转用）', value: 'text' },
                ]" />
              </div>

              <div v-if="m.enabled" style="margin-top:6px;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:2px;">
                  <label style="font-size:11px; margin:0; color:var(--dim)">{{ t('角色专属提示词覆盖 (选填)') }}</label>
                  <NButton size="tiny" quaternary type="primary" @click="loadMemberTemplate(m)" style="font-size:10px; padding:0 2px"><i class="ms sm">description</i> {{ t('载入内置模版') }}</NButton>
                </div>
                <NInput size="small" v-model:value="m.extra_prompt" type="textarea" :rows="2" :autosize="{ minRows: 2, maxRows: 6 }"
                        :placeholder="t('留空继承全局预设提示词；亦可载入内置模版或填入本项目特定规范')" />
              </div>

              <div v-if="m.enabled && (m.role === 'qa' || m.id === 'qa')" style="margin-top:6px;">
                <label class="checkline" style="margin:0">
                  <NSwitch size="small" v-model:value="m.allow_test_scripts" />
                  <span style="font-size:11.5px">{{ t('允许 QA 编写自动化测试脚本并纳入提交') }}</span>
                </label>
              </div>
            </div>
          </div>
        </div>
        </template>
    </ProjectFormSection>
    <ProjectFormSection :title="t('专属模型')" icon="smart_toy"
                        :summary="pDraft.use_llm ? (pDraft.ll_model || t('项目覆盖')) : t('跟随全局')">
    <label class="checkline">
      <NSwitch v-model:value="pDraft.use_llm" />
      {{ t('绑定专属模型（仅该模型处理本项目任务；关闭=跟随全局大模型配置）') }}
    </label>
    <template v-if="pDraft.use_llm">
      <div class="duo" style="margin-top:6px">
        <div class="f1"><label>{{ t('协议 provider') }}</label>
          <NSelect v-model:value="pDraft.ll_provider" :options="llProviderOptions" /></div>
        <div class="f2"><label>{{ t('模型 model') }}</label>
          <NSelect v-model:value="pDraft.ll_model" :options="pModelOptions" filterable tag clearable
                   :placeholder="t('留空继承全局（当前 ') + (store.state?.llm.model || '-') + t('）')"
                   @update:value="(v: string | null) => { pDraft.ll_model = v || '' }" /></div>
      </div>
      <div class="duo">
        <div class="f1"><label>{{ t('接口 base_url') }}</label>
          <NInput v-model:value="pDraft.ll_base" :placeholder="(pDraft.ll_provider === 'anthropic' ? t('填官方根地址即可（自动补 /v1)；') : '') + t('留空继承全局（当前 ') + (store.state?.llm.base_url || '-') + t('）')" /></div>
                <div class="f1"><label>{{ t('工具调用协议（留空继承全局）') }}</label>
                  <NSelect v-model:value="pDraft.ll_protocol" :options="[
                    { label: t('跟随全局'), value: '' },
                    { label: 'native — 原生 function calling', value: 'native' },
                    { label: 'text — 文本协议兜底', value: 'text' },
                  ]" /></div>

        <div class="f1"><label>{{ t('密钥 api_key') }}</label>
          <NInput v-model:value="pDraft.ll_key" type="password" show-password-on="click"
                  :placeholder="llKeyPlaceholder" @update:value="llKeyTouched = true" /></div>
      </div>
      <div class="hint" v-if="pDraft.ll_provider === 'anthropic'">
        {{ t('Anthropic 协议：程序自动补 /v1（已含 /v1 则不重复）；多数兼容服务不提供 /models 列表端点，拉取 404 时直接手填模型名即可。') }}
      </div>
      <div class="btnrow" style="margin-top:6px">
        <NButton size="small" :loading="pFetching" @click="fetchProjectModels">{{ t('获取模型列表') }}</NButton>
        <NButton size="small" :loading="pTesting" @click="testProjectModel">{{ t('测试连接') }}</NButton>
        <span class="hint">{{ t('未填写的字段自动继承全局「大模型接入」配置') }}</span>
      </div>
    </template>
    </ProjectFormSection>
    <ProjectFormSection :title="t('审查 Agent')" icon="fact_check"
                        :summary="pDraft.use_review ? (pDraft.review_ll_model || t('项目覆盖')) : t('跟随全局')">
    <label class="checkline">
      <NSwitch v-model:value="pDraft.use_review" />
      {{ t('审查 Agent（任务合并前由独立模型审查，通过才归档；关闭=跟随全局开关）') }}
    </label>
    <template v-if="pDraft.use_review">
      <div class="duo" style="margin-top:6px">
        <div class="f1"><label>{{ t('协议 provider') }}</label>
          <NSelect v-model:value="pDraft.review_ll_provider" :options="llProviderOptions" /></div>
        <div class="f2"><label>{{ t('审查模型 model') }}</label>
          <NSelect v-model:value="pDraft.review_ll_model" :options="rModelOptions" filterable tag clearable
                   :placeholder="t('留空继承全局（当前 ') + (store.state?.review_cfg?.model || t('未配置')) + t('）')"
                   @update:value="(v: string | null) => { pDraft.review_ll_model = v || '' }" /></div>
      </div>
      <div class="duo">
        <div class="f1"><label>{{ t('接口 base_url') }}</label>
          <NInput v-model:value="pDraft.review_ll_base" :placeholder="t('留空继承全局审查模型配置')" /></div>
                <div class="f1"><label>{{ t('工具调用协议（留空继承全局）') }}</label>
                  <NSelect v-model:value="pDraft.review_ll_protocol" :options="[
                    { label: t('跟随全局'), value: '' },
                    { label: 'native — 原生 function calling', value: 'native' },
                    { label: 'text — 文本协议兜底', value: 'text' },
                  ]" /></div>

        <div class="f1"><label>{{ t('密钥 api_key') }}</label>
          <NInput v-model:value="pDraft.review_ll_key" type="password" show-password-on="click"
                  :placeholder="reviewKeyPlaceholder" @update:value="reviewKeyTouched = true" /></div>
      </div>
      <div class="duo">
        <div class="f1"><label>{{ t('返工审查轮数上限（-1 表示不上限）') }}</label>
          <NInputNumber v-model:value="pDraft.review_rounds" :min="-1" :max="10"
                        :placeholder="t('留空跟随全局（输入 -1 不设上限）')" clearable /></div>
        <div class="f1 btnrow" style="align-items:flex-end">
          <NButton size="small" :loading="rFetching" @click="fetchReviewModels" style="margin-bottom:2px">{{ t('获取模型列表') }}</NButton>
        </div>
      </div>
      <div class="hint" v-if="reviewModelConflict" style="color:#d03050"><i class="ms sm">warning</i> {{ reviewModelConflict }}</div>
      <div class="hint" v-else>{{ t('审查模型必须与任务模型不同；返工轮数留空 = 跟随全局') }}（{{ t('当前') }} {{ store.state?.review_cfg?.max_review_rounds === -1 ? t('不上限') : (store.state?.review_cfg?.max_review_rounds ?? 2) + t(' 次') }}）。{{ t('输入 -1 表示不设上限持续返工。') }}</div>
    </template>
    </ProjectFormSection>
    <ProjectFormSection :title="t('工具与权限')" icon="security"
                        :summary="pDraft.use_tools || pDraft.perm_mode || pDraft.debug_hosts_text.trim() ? t('项目覆盖') : t('跟随全局')">
    <label>{{ t('访问权限（人工审批模式）') }}</label>
    <NSelect v-model:value="pDraft.perm_mode" :options="permOptions" />
    <div class="hint">{{ t('询问 = 任务挂起等待看板顶栏「待审批」批准/拒绝；超时自动拒绝，任务不会永久挂起。') }}</div>
    <label class="checkline" style="margin-top:12px">
      <NSwitch v-model:value="pDraft.use_tools" />
      {{ t('覆盖 Agent 工具开关（关闭=跟随全局「Agent 工具能力」配置）') }}
    </label>
    <template v-if="pDraft.use_tools">
      <div style="margin:8px 0;padding:8px 10px;background:rgba(0,128,255,0.06);border-radius:6px;border:1px solid rgba(0,128,255,0.2)">
        <div style="font-weight:600;font-size:12px;margin-bottom:6px">{{ t('本地调试工具组快捷开关：') }}</div>
        <div style="display:flex;flex-wrap:wrap;gap:12px">
          <label class="checkline" style="display:inline-flex;align-items:center;gap:6px">
            <NSwitch size="small" v-model:value="pBrowserGroupEnabled" />
            <span style="font-size:12px">{{ t('浏览器调试 (browser)') }}</span>
          </label>
          <label class="checkline" style="display:inline-flex;align-items:center;gap:6px">
            <NSwitch size="small" v-model:value="pDevServerGroupEnabled" />
            <span style="font-size:12px">{{ t('开发服务 (dev_server)') }}</span>
          </label>
          <label class="checkline" style="display:inline-flex;align-items:center;gap:6px">
            <NSwitch size="small" v-model:value="pDraft.tools['http_request']" />
            <span style="font-size:12px">{{ t('本地 API (http_request)') }}</span>
          </label>
        </div>
      </div>
      <div class="ptools-grouped" style="margin-top:6px; border:1px solid var(--line); border-radius:8px; padding:6px 10px;">
        <div v-for="cat in projectToolCategories" :key="cat.key" class="pcat-group">
          <div class="pcat-head">
            <div style="display:flex;align-items:center;gap:6px">
              <span><i class="ms sm">{{ miIcon(cat.icon) || cat.icon }}</i></span>
              <span style="font-weight:600;font-size:12px;color:var(--text)">{{ cat.label }}</span>
            </div>
            <div style="display:flex;align-items:center;gap:6px">
              <NTag size="tiny" round :type="pGetGroupToolsCount(cat.tools).enabled === pGetGroupToolsCount(cat.tools).total ? 'success' : 'warning'">
                {{ pGetGroupToolsCount(cat.tools).enabled }}/{{ pGetGroupToolsCount(cat.tools).total }}
              </NTag>
              <NButton size="tiny" quaternary @click="pToggleToolsGroup(cat.tools, true)">{{ t('全选') }}</NButton>
              <NButton size="tiny" quaternary @click="pToggleToolsGroup(cat.tools, false)">{{ t('全关') }}</NButton>
            </div>
          </div>
          <div class="pcat-items">
            <label v-for="name in cat.tools" :key="name" class="checkline ptool-line">
              <NSwitch v-if="name === 'finish'" size="small" :value="true" disabled />
              <NSwitch v-else size="small" v-model:value="pDraft.tools[name]" />
              <span class="ptool-name">{{ name }}</span>
              <NTag size="tiny" :bordered="false" :type="pGetToolRiskTag(name).type" style="font-size:10px;padding:0 3px">
                {{ pGetToolRiskTag(name).label }}
              </NTag>
              <span class="hint ptool-desc">{{ pGetToolDesc(name) }}</span>
            </label>
          </div>
        </div>
      </div>
      <div class="hint" style="margin-top:4px">{{ t('finish 为协议工具始终可用；其余开关保存后立即生效（下一轮任务按此执行）。') }}</div>
    </template>
    <div style="margin-top:10px">
      <label style="font-size:12px;font-weight:bold"><i class="ms sm">language</i> {{ t('额外允许的调试主机（局域网测试机 / 内部域名）') }}</label>
      <NInput v-model:value="pDraft.debug_hosts_text" type="textarea" :rows="2"
              :placeholder="t('每行一个主机名或IP（如 192.168.1.50 或 dev.local，只追加，全局回环地址默认放行）')" />
      <div class="hint">{{ t('该项目测试时允许额外访问的主机白名单；链路本地与云元数据等硬禁区强制拦截。') }}</div>
    </div>
    </ProjectFormSection>
    <ProjectFormSection :title="t('远程仓库 Issues')" icon="cloud_sync"
                        :summary="pDraft.use_repo ? (repoPlatform || t('已绑定')) : t('未绑定')">
    <label class="checkline">
      <NSwitch v-model:value="pDraft.use_repo" />
      {{ t('绑定远程仓库 Issues（把 open Issue 拉取为任务；单向导入，状态不回写）') }}
    </label>
    <template v-if="pDraft.use_repo">
      <div class="duo" style="margin-top:6px">
        <div class="f2"><label>{{ t('仓库 URL') }}{{ repoPlatform ? ' · ' + repoPlatform : '' }}</label>
          <NInput v-model:value="pDraft.repo_url" placeholder="https://github.com/owner/repo" /></div>
        <div class="f1"><label>{{ t('平台（自托管必选）') }}</label>
          <NSelect v-model:value="pDraft.repo_provider" :options="repoProviderOptions" /></div>
      </div>
      <div class="duo">
        <div class="f1"><label>{{ t('访问 token（与账号密码二选一）') }}</label>
          <NInput v-model:value="pDraft.repo_token" type="password" show-password-on="click"
                  :placeholder="repoTokenPlaceholder" @update:value="repoTokenTouched = true" /></div>
        <div class="f1"><label>{{ t('账号 + 密码（可选，GitLab / Gitee / Gitea 支持）') }}</label>
          <div class="duo">
            <NInput v-model:value="pDraft.repo_user" placeholder="username" />
            <NInput v-model:value="pDraft.repo_pass" type="password" show-password-on="click"
                    :placeholder="repoPassPlaceholder" @update:value="repoPassTouched = true" />
          </div></div>
      </div>
      <div class="hint" style="margin-top:6px">
        {{ t('认证二选一：token 优先；GitHub 只支持 token。自托管 Gitea / GitLab 请在「平台」里显式选择。绑定后在项目卡片点「拉取 Issues」导入任务，优先级默认 P2（带 P0~P4 标签按标签）。') }}
      </div>
      <div class="duo" style="margin-top:6px">
        <div class="f1"><label>{{ t('自动拉取间隔（分钟，0 = 仅手动）') }}</label>
          <NInputNumber v-model:value="pDraft.repo_auto_pull" :min="0" :step="5"
                        :disabled="!pDraft.repo_url.trim()"
                        :placeholder="t('0 表示只在点「拉取 Issues」时拉取')" /></div>
        <div class="f2"><label>{{ t('上次自动拉取') }}</label>
          <div class="hint" style="padding-top:6px">
            {{ repoAutoPullHint }}</div></div>
      </div>
      <div class="hint">
        {{ t('填写间隔后后台线程每 N 分钟自动把 open Issue 拉取为任务（去重规则与手动拉取相同）。') }}
      </div>
    </template>
    </ProjectFormSection>
    </div>
    <template #footer>
      <div v-if="pDraft.err" class="err project-error" role="alert">{{ pDraft.err }}</div>
      <div class="project-footer">
        <label class="checkline project-enabled" :title="t('启用该项目（停用后调度器跳过，配置保留）')">
          <NSwitch v-model:value="pDraft.enabled" :aria-label="t('启用项目')" />
          <span>{{ t('启用项目') }}</span>
        </label>
        <div class="project-actions">
          <NButton @click="ui.projectDlg = null">{{ t('取消') }}</NButton>
          <NButton type="primary" @click="saveProject">{{ t('保存') }}</NButton>
        </div>
      </div>
    </template>
  </NModal>

  <!-- 任务 编辑/新建/只读查看。
       移动端（≤768px）切换为全屏抽屉（Sheet）：吸顶操作条（取消/保存常驻可视区，
       软键盘顶起时仍可触达）+ Markdown 快捷工具条 + 草稿恢复提示；高度跟随
       visualViewport（键盘弹起自动压缩滚动区，杜绝遮挡） -->
  <NModal v-model:show="tVisible" preset="card" :title="tTitle"
          :class="isMobile ? 'tsheet' : ''"
          :style="isMobile
            ? { width: '100vw', height: viewportHeight + 'px', maxHeight: viewportHeight + 'px' }
            : { width: 'min(560px, 92vw)' }">
    <!-- 移动端吸顶操作条：保存/取消永远固定在最顶端 -->
    <div class="tsheet-bar" v-if="isMobile">
      <NButton size="small" quaternary @click="ui.taskDlg = null">{{ tReadonly ? t('关闭') : t('取消') }}</NButton>
      <span class="tsheet-title">{{ tTitle }}</span>
      <NButton v-if="!tReadonly" size="small" type="primary"
               @click="saveTask(tForceOverwrite)">{{ t('保存') }}</NButton>
    </div>
    <!-- 未保存草稿恢复提示：来电/切后台被杀后重开不丢输入 -->
    <div class="draftbar" v-if="draftAvailable">
      <span class="draftbar-msg">{{ t('检测到未保存的草稿') }}</span>
      <NButton size="tiny" type="primary" @click="restoreDraft">{{ t('恢复') }}</NButton>
      <NButton size="tiny" quaternary @click="discardDraft">{{ t('丢弃') }}</NButton>
    </div>
    <div class="hint" v-if="tReadonly">{{ t('该任务不在待办状态（已归档/执行中/受阻），只读查看；重新入队后再编辑。') }}</div>
    <div class="hint" v-else>{{ t('仅待办（open）状态的任务可编辑；执行中/受阻的任务请等本轮结束后再改。') }}</div>
    <div class="err" v-if="tDraft.err">{{ tDraft.err }}</div>
    <div class="duo">
      <div class="f1"><label>{{ t('任务 ID（留空自动生成：优先级-标题）') }}</label><NInput v-model:value="tDraft.id" :disabled="tDraft.idLocked" placeholder="fix-login-timeout" /></div>
      <div class="f1"><label>{{ t('优先级') }}</label>
        <NSelect v-model:value="tDraft.priority" :options="priOptions" :disabled="tReadonly" /></div>
    </div>
    <label>{{ t('标题') }}</label>
    <NInput v-model:value="tDraft.title" :disabled="tReadonly" :placeholder="t('一句话描述要做什么')" />
    <div class="duo">
      <div class="f1"><label>{{ t('标签（逗号分隔）') }}</label><NInput v-model:value="tDraft.labels" :disabled="tReadonly" placeholder="bug, ui" /></div>
      <div class="f1">
        <label>{{ t('指派执行角色') }}</label>
        <NSelect v-model:value="tDraft.assignee" :options="projectAssigneeOptions" :disabled="tReadonly" filterable tag clearable :placeholder="t('留空自动认领')" />
      </div>
    </div>
    <div style="margin-bottom: 6px;">
      <label>{{ t('前置依赖任务（全部完成后本任务方可认领执行）') }}</label>
      <NSelect v-model:value="tDraft.depends_on" multiple filterable :options="projectTaskOptions" :disabled="tReadonly" :placeholder="t('选择依赖的前置任务（可选）...')" />
      <div v-if="tDraft.parent_id" class="hint" style="margin-top: 4px;">
        <i class="ms sm">link</i> {{ t('关联父需求/来源任务：') }}
        <button type="button" class="deplink" @click="openDependency(tDraft.parent_id)">
          <i class="ms sm">open_in_new</i>{{ tDraft.parent_id }}
        </button>
        {{ tDraft.qa_round ? tFmt('· 回归轮次 #{p0}', { p0: tDraft.qa_round }) : '' }}
      </div>
      <div v-if="depTasks.length" class="deplist">
        <div class="deplist-head">{{ t('前置任务成果（点击任务打开详情）') }}</div>
        <div v-for="d in depTasks" :key="d.id" class="depitem">
          <NTag size="tiny" :bordered="false" :type="depTag(d).type">{{ depTag(d).label }}</NTag>
          <button type="button" class="deplink" :disabled="d.missing" @click="openDependency(d.id)">
            <i class="ms sm">open_in_new</i>{{ d.id }}<template v-if="d.title"> · {{ d.title }}</template>
          </button>
          <span v-if="d.summary" class="depsum" :title="d.summary">{{ depSummary(d.summary) }}</span>
        </div>
      </div>
    </div>
    <label>{{ t('任务描述（Markdown 正文）') }}</label>
    <!-- 移动端 Markdown 快捷工具条：一键插入标题/待办/加粗/代码块 -->
    <div class="md-bar" v-if="isMobile && !tReadonly">
      <button type="button" @click="mdInsert('# ', '')">H1</button>
      <button type="button" @click="mdInsert('## ', '')">H2</button>
      <button type="button" @click="mdInsert('- [ ] ', '')"><i class="ms sm">check_box</i> {{ t('待办') }}</button>
      <button type="button" @click="mdInsert('- ', '')"><i class="ms sm">format_list_bulleted</i> {{ t('列表') }}</button>
      <button type="button" @click="mdInsert('**', '**')">{{ t('B 粗体') }}</button>
      <button type="button" :title="t('行内代码')" @click="mdInsert('`', '`')"><i class="ms sm">code</i></button>
      <button type="button" :title="t('代码块')" @click="mdInsert('\n```\n', '\n```\n')"><i class="ms sm">integration_instructions</i></button>
    </div>
    <NInput ref="descInputRef" v-model:value="tDraft.description" type="textarea" :disabled="tReadonly" :autosize="{ minRows: 7, maxRows: 16 }" />
    <!-- 结构化审查意见（从正文「## 审查结果」节解析）：severity 色标条目列表 -->
    <template v-if="reviewIssues.length">
      <label style="margin-top:10px">{{ t('审查意见（独立审查 Agent 退回的问题清单）') }}</label>
      <div class="review-issues">
        <div v-for="(it, idx) in reviewIssues" :key="idx" class="review-issue">
          <NTag size="tiny" :type="it.severity === 'blocker' ? 'error' : it.severity === 'major' ? 'warning' : 'default'"
                :bordered="false">{{ it.severity }}</NTag>
          <span class="ri-loc">{{ it.loc }}</span>
          <span class="ri-msg">{{ it.message }}</span>
        </div>
      </div>
    </template>
    <!-- 运行台账（V1-R1）：历史 run 列表 + 事件回放 + 工件查看（只读） -->
    <div v-if="ui.taskDlg?.id" class="runsec">
      <div class="runsec-head">
        <label style="margin:0">{{ t('运行记录') }}</label>
        <span class="hint" style="margin:0">{{ runList.length ? tFmt('{p0} 次', { p0: runList.length }) : t('暂无') }}</span>
        <NButton size="tiny" quaternary :loading="runLoading" :disabled="runlogBusy"
                 @click="loadRuns">{{ t('刷新') }}</NButton>
      </div>
      <div v-if="runlogBusy" class="hint">
        {{ t('运行台账正在迁移目录，暂不可回放；迁移完成后自动恢复。') }}
      </div>
      <div v-if="!runLoading && !runList.length" class="hint">
        {{ t('暂无运行记录：任务首次执行后生成；配置 runlog.enabled=false 时不记录。') }}
      </div>
      <div v-for="r in runList" :key="r.run_id" class="runrow">
        <div class="runrow-head clickable" :class="{ disabled: runlogBusy }"
             @click="toggleRun(r)">
          <span class="run-badge" :class="`o-${r.outcome || 'running'}`">{{ r.outcome || 'running' }}</span>
          <span class="run-time">{{ (r.started_at || '').slice(5, 16).replace('T', ' ') }}</span>
          <span class="run-meta">{{ r.role || r.agent_name || 'agent' }} · {{ r.event_count }} {{ t('事件') }}{{ r.review_verdict ? t(' · 审查 ') + r.review_verdict : '' }}</span>
          <span class="run-toggle"><i class="ms sm">{{ runOpen === r.run_id ? 'expand_more' : 'chevron_right' }}</i></span>
        </div>
        <div v-if="runOpen === r.run_id" class="run-detail">
          <div v-if="runDetailLoading" class="hint">{{ t('加载中…') }}</div>
          <template v-else-if="runDetail">
            <div v-if="runError" class="err">{{ runError }}</div>
            <div class="hint">
              exit: {{ runDetail.meta.exit_reason || '-' }} · tokens:
              {{ runDetail.meta.tokens?.prompt_tokens || 0 }}+{{ runDetail.meta.tokens?.completion_tokens || 0 }}
              <template v-if="runDetail.meta.commit"> · commit {{ runDetail.meta.commit }}</template>
            </div>
            <div class="run-actions">
              <NButton v-for="a in runDetail.artifacts" :key="a.name" size="tiny" quaternary
                       :disabled="runlogBusy"
                       :type="runArtifactName === a.name ? 'primary' : 'default'"
                       @click="viewRunArtifact(r, a.name)">{{ a.name }}</NButton>
            </div>
            <pre v-if="runArtifact" class="run-artifact">{{ runArtifact }}</pre>
            <div v-if="runDetail.event_total > runDetail.events.length" class="hint">
              {{ t('仅显示前') }} {{ runDetail.events.length }} / {{ runDetail.event_total }} {{ t('条事件（可在 meta.json 查看完整计数）') }}
            </div>
            <div class="run-events">
              <div v-for="e in runDetail.events" :key="e.seq" class="run-event">
                <span class="re-seq">{{ e.seq }}</span>
                <span class="re-type">{{ e.type }}</span>
                <span class="re-ts">{{ (e.ts || '').slice(11, 19) }}</span>
                <span class="re-data">{{ runEventLine(e) }}</span>
              </div>
            </div>
          </template>
        </div>
      </div>
    </div>
    <div class="dlg-actions" v-if="!isMobile">
      <NButton @click="ui.taskDlg = null">{{ tReadonly ? t('关闭') : t('取消') }}</NButton>
      <NButton v-if="!tReadonly" type="primary" @click="saveTask(tForceOverwrite)">{{ t('保存') }}</NButton>
    </div>
  </NModal>

  <!-- 手机连接向导：可达地址清单 + 扫码二维码 + 开放指引 -->
  <NModal v-model:show="cVisible" preset="card" :title="t('手机远程连接')"
          style="width: min(480px, 94vw)">
    <div class="hint">{{ t('手机与电脑连同一 Wi-Fi（或经反代/内网穿透/公网域名），用手机相机扫码直达看板；') }}
      {{ t('首次打开需输入登录密码') }}{{ cAuthRequired ? t('（已开启访问鉴权）') : t('（未开启鉴权可直接访问）') }}。</div>
    <!-- 仅监听 127.0.0.1：不渲染打不开的回环二维码，展示一键开启 + 手动指引 -->
    <div class="ep-guide" v-if="cAllLoopback && !cLoading">
      <div class="ep-guide-title"><i class="ms sm">warning</i> {{ t('手机现在还连不上：看板只监听了本机回环（127.0.0.1）') }}</div>
      <div class="ep-enable">
        <NCheckbox :checked="cEnableChecked" :disabled="cEnableBusy || cEnabled"
                   @update:checked="onEnableToggle">
          {{ t('一键开放手机访问（自动改配置并重启应用）') }}
        </NCheckbox>
        <div class="ep-enable-note" v-if="cEnabled"><i class="ms sm">check_circle</i> {{ t('已开放（配置已保存）') }}{{ cEnableNote ? '——' + cEnableNote : t('，应用重启中，请稍候…') }}</div>
      </div>
      <div class="ep-guide-steps">
        <div>{{ t('手动方式：打开 config.yaml（数据目录下），把') }} <code>web.host</code> {{ t('改为') }} <code>0.0.0.0</code>，
          {{ t('设置') }} <code>web.auth_password</code> {{ t('登录密码后重启。') }}</div>
      </div>
      <div class="ep-guide-hint" v-if="cHint">{{ cHint }}</div>
    </div>
    <NSpin :show="cLoading">
      <div class="qr-wrap" v-if="cSelected">
        <div v-if="cQrSvg" class="qr-svg" v-html="cQrSvg" />
        <div v-else class="qr-empty">{{ t('二维码生成失败，请直接输入下方地址') }}</div>
        <div class="qr-url">{{ cSelected }}</div>
      </div>
      <div class="ep-list">
        <button v-for="e in cEndpoints" :key="e.url" class="ep-item"
                :class="{ on: cSelected === e.url, off: e.active === false }"
                @click="clickEndpoint(e)">
          <span class="ep-name">{{ e.name }}</span>
          <span class="ep-url">{{ e.url }}</span>
        </button>
      </div>
      <div class="dlg-actions">
        <NButton size="small" :disabled="!cSelected" @click="copyEndpoint(cSelected)">{{ t('复制地址') }}</NButton>
        <NButton size="small" type="primary" @click="ui.connectDlg = false">{{ t('完成') }}</NButton>
      </div>
    </NSpin>
  </NModal>

  <!-- AGENT.md 编辑 -->
  <NModal v-model:show="gVisible" preset="card" class="gdlg"
          :title="ui.guideDlg ? tFmt('编辑指引 · {p0}', { p0: ui.guideDlg.project }) : t('编辑指引')"
          style="width: min(860px, 94vw)">
    <div class="hint" v-if="gDraft.file">{{ gDraft.file }}</div>
    <div class="err" v-if="gDraft.err">{{ gDraft.err }}</div>
    <NInput v-model:value="gDraft.content" type="textarea" class="geditor"
            :autosize="false" :input-props="{ spellcheck: false }"
            style="height: 55vh" />
    <div class="dlg-actions">
      <NButton @click="ui.guideDlg = null">{{ t('取消') }}</NButton>
      <NButton type="primary" @click="saveGuide">{{ t('保存指引') }}</NButton>
    </div>
  </NModal>

  <!-- 技能 新建/编辑 -->
  <NModal v-model:show="kVisible" preset="card"
          :title="ui.skillDlg?.original ? tFmt('编辑技能 · {p0}', { p0: ui.skillDlg.original }) : t('新建技能')"
          style="width: min(760px, 94vw)">
    <div class="hint">{{ t('技能存放在 Orcsy 技能库（不落项目目录）；正文为 SKILL.md 的 Markdown 正文，frontmatter 由系统维护。') }}</div>
    <div class="err" v-if="kDraft.err">{{ kDraft.err }}</div>
    <div class="duo">
      <div class="f1"><label>{{ t('技能名（字母数字/下划线/连字符）') }}</label><NInput v-model:value="kDraft.name" placeholder="vue-best-practices" /></div>
      <div class="f1"><label>{{ t('适用范围') }}</label>
        <NSelect v-model:value="kDraft.scope" :options="scopeOptions" /></div>
    </div>
    <label>{{ t('描述（进入 Agent 系统提示词的索引，写清何时该用这个技能）') }}</label>
    <NInput v-model:value="kDraft.description" :placeholder="t('一句话说明技能的用途与适用场景')" />
    <label class="checkline" style="margin-top:4px">
      <NSwitch v-model:value="kDraft.enabled" />
      {{ t('启用（停用后不注入索引，内容保留）') }}
    </label>
    <label>{{ t('技能内容（Markdown 正文，Agent 通过 read_skill 工具按需读取）') }}</label>
    <NInput v-model:value="kDraft.body" type="textarea" class="keditor"
            :autosize="false" :input-props="{ spellcheck: false }"
            style="height: 38vh" :placeholder="t('# 何时使用 / # 步骤与规范 / # 注意事项 …')" />
    <div class="dlg-actions">
      <NButton @click="ui.skillDlg = null">{{ t('取消') }}</NButton>
      <NButton type="primary" @click="saveSkill">{{ t('保存技能') }}</NButton>
    </div>
  </NModal>

  <!-- 扫描 AI 工具技能 -->
  <NModal v-model:show="scanVisible" preset="card" :title="t('扫描 AI 工具的本地技能')"
          style="width: min(660px, 94vw)">
    <div class="hint">{{ t('扫描常见 AI 工具的用户级技能目录（ZCode / 通用 Agent / Claude Code / Codex / OpenCode 等），勾选后整目录复制进 Orcsy 技能库；同一技能在多个工具里重复部署时只显示一次。找不到想要的可点「导入本地技能」手工选目录。') }}</div>
    <div class="err" v-if="scanErr">{{ scanErr }}</div>
    <div v-if="scanning" class="hint">{{ t('扫描中…') }}</div>
    <template v-else>
      <div v-if="!scanSources.length" class="hint">
        {{ t('没有发现常见 AI 工具的技能目录。可手工选择目录导入，或在上述工具里先安装技能。') }}
      </div>
      <div v-if="scanSources.length" class="scan-toolbar">
        <span class="hint">{{ t('共') }} {{ allScanSkills.length }} {{ t('个技能，已选') }} {{ checkedPaths.length }} {{ t('个') }}</span>
        <NButton size="tiny" @click="checkAll">{{ t('全选') }}</NButton>
        <NButton size="tiny" @click="invertChecked">{{ t('反选') }}</NButton>
      </div>
      <div v-for="src in scanSources" :key="src.path" class="scan-group">
        <div class="scan-tool">{{ src.tool }} <span class="scan-path">{{ src.path }}</span></div>
        <label v-for="k in src.skills" :key="k.path" class="scan-item">
          <input type="checkbox" v-model="scanChecked[k.path]" />
          <span class="scan-name">{{ k.name }}</span>
          <span class="scan-desc">{{ k.description || t('（无描述）') }}</span>
          <NTag v-if="k.exists" size="small" :bordered="false">{{ t('已导入') }}</NTag>
        </label>
      </div>
    </template>
    <div class="dlg-actions">
      <NButton @click="ui.skillScanDlg = false">{{ t('取消') }}</NButton>
      <NButton type="primary" :loading="importing" :disabled="!checkedPaths.length" @click="importChecked">
        {{ t('导入选中') }}（{{ checkedPaths.length }}）
      </NButton>
    </div>
  </NModal>

  <!-- 目录/文件选择器（browserMode='file' 时点文件即选中，如手动导入更新包） -->
  <NModal v-model:show="bVisible" preset="card"
          :title="ui.browserMode === 'file' ? t('选择本地文件') : t('选择本地目录')"
          style="width: min(560px, 92vw)">
    <div class="brow-crumb">
      <!-- parent=="" 是盘符列表根入口（可点）；仅 null/undefined 才禁用 -->
      <NButton size="small" @click="browseTo(bStack?.parent ?? '')"
               :disabled="bStack?.parent == null"><i class="ms sm">arrow_upward</i> {{ t('上一级') }}</NButton>
      <span class="bcur">{{ bStack?.path || t('（请选择盘符 / 常用目录）') }}</span>
    </div>
    <div class="blist">
      <div v-for="d in bStack?.dirs || []" :key="d" class="bitem" @click="browseTo(d)">{{ d }}</div>
      <template v-if="ui.browserMode === 'file'">
        <div v-for="f in bStack?.files || []" :key="f" class="bitem bfile" @click="pickFile(f)">
          {{ f.split('\\').pop()?.split('/').pop() }}
        </div>
        <div v-if="bStack && !bStack.files?.length" class="bempty">{{ t('（当前目录没有文件）') }}</div>
      </template>
      <div v-else-if="bStack && !bStack.dirs.length" class="bempty">{{ bStack.note || t('（没有子目录，可直接用下面的路径框）') }}</div>
    </div>
    <div class="pathline" style="margin-bottom:8px">
      <NInput v-model:value="bNewFolder" size="small" :placeholder="t('在当前目录下新建文件夹，输入名称')"
              @keydown.enter="newFolder" />
      <NButton size="small" @click="newFolder">{{ t('＋ 新建文件夹') }}</NButton>
    </div>
    <div class="pathline">
      <NInput v-model:value="bManual" :placeholder="t('或直接粘贴完整路径')" @keydown.enter="pickPath" />
      <NButton type="primary" @click="pickPath">{{ ui.browserMode === 'file' ? t('使用此文件') : t('使用此目录') }}</NButton>
    </div>
    <div class="dlg-actions"><NButton @click="ui.browserDlg = false">{{ t('取消') }}</NButton></div>
  </NModal>

  <!-- 人工审批面板：列表绑定 store.state.approvals，5s 轮询自动刷新；
       本面板故意不暂停轮询（store.anyModal 不含 approvalDlg），处理结果即时可见 -->
  <NModal v-model:show="approvalVisible" preset="card" :title="t('待人工审批')"
          style="width: min(640px, 92vw)">
    <div class="hint">
      {{ t('Agent 按访问权限模式（变更前询问 / 自主模式）挂起了以下操作，等待你的决定；') }}
      {{ t('批准后任务继续执行。超过配置的时限无人响应将自动拒绝。') }}
    </div>
    <div class="hint">
      {{ t('风险合入审批由合入前风险策略触发，与工具访问权限无关；批准后才会合入，拒绝或超时保留任务分支。') }}
    </div>
    <div v-if="!approvals.length && !riskApprovals.length" class="appr-empty">
      {{ t('当前没有挂起的审批请求（已处理或超时的条目会自动消失）。') }}
    </div>
    <div v-for="a in approvals" :key="a.id" class="appr">
      <div class="appr-head">
        <NTag size="small" :bordered="false" type="info">{{ a.project }}</NTag>
        <NTag size="small" :bordered="false">{{ a.task_id }}</NTag>
        <NTag size="small" :bordered="false" type="warning">{{ a.tool }}</NTag>
        <span class="hint appr-wait">{{ t('已等待') }} {{ fmtWaited(a.waited_seconds) }} ·
          {{ PERM_LABEL[a.mode] || a.mode }}</span>
      </div>
      <pre class="appr-args">{{ a.args_preview }}</pre>
      <div class="appr-ops">
        <NCheckbox v-model:checked="alwaysAllow[a.id]">{{ t('本任务内始终允许') }} {{ a.tool }}</NCheckbox>
        <div class="btnrow">
          <NButton size="small" :disabled="responding === a.id"
                   @click="respondApproval(a.id, false)">{{ t('拒绝') }}</NButton>
          <NButton size="small" type="primary" :loading="responding === a.id"
                   @click="respondApproval(a.id, true)">{{ t('批准') }}</NButton>
        </div>
      </div>
    </div>
    <div v-if="riskApprovals.length" class="appr-sec">
      <div class="appr-sec-title">{{ t('风险合入审批') }}</div>
      <div v-for="r in riskApprovals" :key="r.id" class="appr risk">
        <div class="appr-head">
          <NTag size="small" :bordered="false" :type="riskLevelType(r.risk_level)">
            {{ riskLevelLabel(r.risk_level) }}
          </NTag>
          <NTag size="small" :bordered="false" type="info">{{ r.project }}</NTag>
          <NTag size="small" :bordered="false">{{ r.task_id }}</NTag>
          <NTag v-if="r.mode === 'quorum'" size="small" :bordered="false" type="warning">
            {{ r.provider || t('会签') }} · {{ r.approved_count || 0 }}/{{ r.required || 0 }}
          </NTag>
          <span class="hint appr-wait">{{ t('已等待') }} {{ fmtWaited(r.waited_seconds) }}</span>
        </div>
        <div class="appr-title">{{ r.title }}</div>
        <div class="risk-reasons">
          <div v-for="(reason, ri) in r.reasons" :key="ri">• {{ reason }}</div>
        </div>
        <div class="hint">
          {{ r.files.length }} {{ t('个文件') }} ·
          <span class="risk-add">+{{ r.additions }}</span>/<span class="risk-del">-{{ r.deletions }}</span>
          · {{ r.branch }}@{{ r.commit }}
        </div>
        <template v-if="r.mode === 'quorum'">
          <div v-if="r.approvers?.length" class="risk-reasons">
            <div v-for="a in r.approvers" :key="a.email">• {{ a.email }} · {{ a.name || a.id }}</div>
          </div>
          <div class="hint" style="display:flex; align-items:center; gap:6px;">
            {{ t('会签由外部 Provider 完成；这里只查看进度，不在本页批准。') }}
            <ProviderHelp />
          </div>
          <div class="btnrow" style="justify-content:flex-end; margin-top:8px">
            <a v-if="r.external_url" :href="r.external_url" target="_blank" rel="noopener">
              <NButton size="small">{{ t('打开审批') }}</NButton>
            </a>
            <NButton size="small" @click="refresh(true)">{{ t('刷新状态') }}</NButton>
          </div>
        </template>
        <template v-else>
          <NInput v-model:value="riskComments[r.id]" type="textarea" :rows="2"
                  :placeholder="t('审批意见（可选）')" />
          <div class="btnrow" style="justify-content:flex-end; margin-top:8px">
            <NButton size="small" :disabled="riskResponding === r.id"
                     @click="respondRiskApproval(r.id, false)">{{ t('拒绝合入') }}</NButton>
            <NButton size="small" type="primary" :loading="riskResponding === r.id"
                     @click="respondRiskApproval(r.id, true)">{{ t('批准合入') }}</NButton>
          </div>
        </template>
      </div>
    </div>
  </NModal>
</template>

<style scoped>
.err { color: var(--n-error-color); font-size: 12.5px; margin-bottom: 6px; }
.hint { font-size: 12px; color: var(--faint); margin-bottom: 8px; }
.duo { display: flex; gap: 10px; }
.duo > div { flex: 1; display: flex; flex-direction: column; gap: 4px; }
/* 项目弹窗只滚动正文；折叠配置不改动草稿或功能开关。 */
.project-content { display: flex; flex-direction: column; gap: 10px; padding: 0 24px 16px; }
.project-basics { min-width: 0; padding-bottom: 4px; }
.project-basics-title { margin: 0 0 8px; color: var(--text); font-size: 13px; font-weight: 600; }
.project-content .duo { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 12px; margin-top: 8px; }
.project-content .duo > *, .project-content .tm-grid > * { min-width: 0; }
.project-content .duo .duo { grid-template-columns: minmax(0, 1fr); margin-top: 0; }
.project-content .pathline { margin-top: 4px; min-width: 0; }
.project-content .pathline > :first-child { flex: 1; min-width: 0; }
.project-content .pathline > :last-child { flex: none; }
.project-content label { line-height: 1.5; overflow-wrap: anywhere; }
.project-content .checkline { flex-wrap: nowrap; align-items: flex-start; gap: 8px; }
.project-content :deep(.n-switch) { flex-shrink: 0; }
.project-content .btnrow { flex-wrap: wrap; }
.project-content .btnrow .hint { flex: 1 1 220px; margin-bottom: 0; }
.project-content .hint { line-height: 1.6; margin: 6px 0 0; overflow-wrap: anywhere; }
.project-content .tm-head { flex-wrap: wrap; gap: 8px; }
.project-content .tm-head > div { flex-wrap: wrap; min-width: 0; }
.project-content .tm-head > div:first-child { flex: 1 1 240px; }
.project-content .tm-grid { grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }
.project-content .pcat-head { flex-wrap: wrap; gap: 6px; }
.project-content .ptool-line { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 4px 8px; }
.project-content .ptool-name { min-width: 0; overflow-wrap: anywhere; }
.project-content .ptool-desc { grid-column: 2 / -1; margin: 0; }
.project-footer { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 16px;
  padding: 14px 24px; border-top: 1px solid var(--line); }
.project-enabled { flex: 1; margin: 0; min-width: 0; }
.project-actions { display: flex; gap: 8px; margin-left: auto; flex: none; }
.project-error { padding: 8px 24px 0; margin: 0; max-height: 80px; overflow-y: auto; overflow-wrap: anywhere; }
@media (max-width: 768px) {
  .project-content { padding: 0 12px 12px; gap: 8px; }
  .project-content .duo, .project-content .tm-grid { grid-template-columns: minmax(0, 1fr) !important; }
  .project-content .duo { gap: 8px; }
  .project-content .tm-head > div:first-child { flex-basis: 100%; }
  .project-content .pathline :deep(.n-button), .project-actions :deep(.n-button) { min-height: 40px; }
  .project-footer { gap: 10px; padding: 12px max(12px, env(safe-area-inset-right)) max(12px, env(safe-area-inset-bottom)) max(12px, env(safe-area-inset-left)); }
  .project-error { padding: 8px 12px 0; }
}
@media (max-width: 380px) {
  .project-enabled { flex-basis: 100%; }
  .project-actions { width: 100%; }
  .project-actions :deep(.n-button) { flex: 1; }
}
label, .checkline { font-size: 12.5px; color: var(--dim); display: flex; gap: 8px; align-items: center; margin-top: 4px; }
.dlg-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 14px; }
.pathline { display: flex; gap: 8px; }
.btnrow { display: flex; gap: 8px; align-items: center; }
.brow-crumb { display: flex; gap: 10px; align-items: center; margin-bottom: 8px; }
.bcur { font-size: 12.5px; color: var(--dim); word-break: break-all; }
.blist {
  border: 1px solid var(--line); border-radius: 8px;
  max-height: 300px; overflow: auto; margin-bottom: 10px;
}
.bitem { padding: 7px 12px; cursor: pointer; font-size: 13px; }
.bitem:hover { background: rgba(128, 140, 180, 0.16); }
.bempty { padding: 12px; font-size: 12.5px; color: var(--faint); }
.ptools { border: 1px solid var(--line); border-radius: 8px; padding: 6px 10px;
  max-height: 220px; overflow: auto; margin-bottom: 6px; }
.pcat-group {
  margin-bottom: 8px;
  border-bottom: 1px dashed var(--line);
  padding-bottom: 6px;
}
.pcat-group:last-child {
  border-bottom: none;
  margin-bottom: 0;
  padding-bottom: 0;
}
.pcat-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 4px 0;
  margin-bottom: 4px;
}
.pcat-items {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-left: 2px;
}
.ptool-line { align-items: baseline; margin-top: 4px; }
.ptool-name { font-family: Consolas, monospace; font-size: 12px; color: var(--text);
  flex: none; min-width: 88px; }
.ptool-desc { flex: 1; min-width: 0; margin-bottom: 0; }
.appr { border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px;
  margin-bottom: 10px; }
.appr-head { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.appr-wait { margin-bottom: 0; }
.appr-args { font: 12px/1.6 Consolas, "Cascadia Mono", monospace; color: var(--dim);
  background: rgba(128, 128, 160, 0.1); border-radius: 6px; padding: 8px 10px;
  margin: 8px 0; white-space: pre-wrap; word-break: break-all; max-height: 160px;
  overflow: auto; }
.appr-ops { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
.appr-empty { color: var(--faint); font-size: 13px; text-align: center; padding: 22px 0; }
.appr-sec { margin-top: 14px; border-top: 1px solid var(--line); padding-top: 10px; }
.appr-sec-title { font-weight: 600; font-size: 12.5px; margin-bottom: 8px; }
.appr.risk { border-color: rgba(240, 90, 90, 0.35); }
.appr-title { font-weight: 600; font-size: 13px; margin: 6px 0; }
.risk-reasons { font-size: 12px; color: var(--dim); margin: 4px 0 8px;
  max-height: 100px; overflow: auto; }
.risk-add { color: #4caf50; }
.risk-del { color: #e57373; }
.geditor { font: 12.5px/1.7 Consolas, "Cascadia Mono", monospace; }
.keditor { font: 12.5px/1.7 Consolas, "Cascadia Mono", monospace; }
.scan-group { margin-bottom: 10px; }
.scan-toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.scan-toolbar .hint { flex: 1; margin: 0; }
.scan-tool { font-size: 12.5px; font-weight: 600; margin-bottom: 4px; }
.scan-path { font-weight: 400; font-size: 11.5px; color: var(--faint); word-break: break-all; }
.scan-item { display: flex; gap: 8px; align-items: center; padding: 5px 8px; border: 1px solid var(--line); border-radius: 6px; margin-bottom: 4px; cursor: pointer; }
.scan-item:hover { background: rgba(128, 140, 180, 0.12); }
.scan-item input { flex: none; }
.scan-name { font-weight: 600; font-size: 12.5px; flex: none; }
.scan-desc { flex: 1; font-size: 12px; color: var(--dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 结构化审查意见：severity 色标 + 文件位置 + 问题描述 */
.review-issues { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px;
  display: flex; flex-direction: column; gap: 6px; max-height: 200px; overflow: auto; }
.review-issue { display: flex; gap: 8px; align-items: baseline; font-size: 12.5px; min-width: 0; }
/* 位置列可收缩并省略显示：长路径不得把右侧描述挤成窄列 */
.ri-loc { font-family: Consolas, "Cascadia Mono", monospace; color: var(--dim);
  flex: 0 1 auto; min-width: 0; max-width: 45%; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.ri-msg { color: var(--text); min-width: 0; flex: 1 1 auto; word-break: break-word; }

/* ---- 移动端任务全屏抽屉（Sheet）：吸顶操作条 + 键盘避让 + 工具条 ---- */
.tsheet { border-radius: 0; }
.tsheet :deep(.n-card-header) { display: none; }  /* 顶部由吸顶操作条接管 */
.tsheet :deep(.n-card__content) {
  display: flex; flex-direction: column;
  height: calc(100% - 2px); overflow-y: auto; padding-bottom: 24px;
}
.tsheet-bar {
  position: sticky; top: 0; z-index: 5;
  display: flex; gap: 10px; align-items: center; justify-content: space-between;
  background: var(--panel, #131a2a); margin: -8px -8px 8px; padding: 8px 10px;
  border-bottom: 1px solid var(--line, #253048);
}
.tsheet-title { font-size: 14px; font-weight: 600; color: var(--text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; text-align: center; }
.draftbar {
  display: flex; gap: 8px; align-items: center;
  background: rgba(91, 140, 255, 0.12); border: 1px solid var(--acc, #5b8cff);
  border-radius: 8px; padding: 8px 12px; margin-bottom: 8px;
}
.draftbar-msg { flex: 1; font-size: 12.5px; color: var(--text); }
/* Markdown 快捷工具条：手机打字效率（插入位置=当前光标/选区） */
.md-bar { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 6px; }
.md-bar button {
  appearance: none; border: 1px solid var(--line, #253048); background: var(--card, #1a2235);
  color: var(--text, #e5eaf6); font: 12px/1.4 inherit; padding: 5px 9px;
  border-radius: 6px; cursor: pointer;
}
.md-bar button:active { border-color: var(--acc, #5b8cff); color: var(--acc, #5b8cff); }
/* ---- 手机连接向导 ---- */
.qr-wrap { display: flex; flex-direction: column; align-items: center; gap: 8px;
  padding: 14px 0 6px; }
.qr-svg { width: 220px; height: 220px; background: #fff; border-radius: 10px;
  padding: 10px; line-height: 0; }
.qr-svg :deep(svg) { width: 100%; height: 100%; }
.qr-empty { font-size: 12.5px; color: var(--dim); padding: 24px 0; }
.qr-url { font: 13px/1.5 Consolas, monospace; color: var(--text); word-break: break-all;
  text-align: center; }
.ep-list { display: flex; flex-direction: column; gap: 6px; margin-top: 6px; }
.ep-item {
  appearance: none; cursor: pointer; text-align: left; font: inherit;
  border: 1px solid var(--line); background: var(--card); border-radius: 8px;
  padding: 8px 12px; display: flex; flex-direction: column; gap: 2px;
}
.ep-item.on { border-color: var(--acc, #5b8cff); }
.ep-item.off { opacity: 0.55; cursor: not-allowed; }
/* 小组成员卡片（手机）：头部换行防成员名竖排、按钮截断；字段网格单列 */
@media (max-width: 768px) {
  .tm-head { flex-wrap: wrap !important; row-gap: 6px; }
  .tm-head > div:first-child { flex: 1; min-width: 0; }
  .tm-head span { white-space: normal; }
  .tm-grid { grid-template-columns: 1fr !important; }
}
.ep-name { font-size: 12.5px; color: var(--dim); }
.ep-url { font: 12.5px/1.5 Consolas, monospace; color: var(--text); word-break: break-all; }
/* 仅监听 127.0.0.1 时的开放步骤指引卡片 */
.ep-guide { border: 1px solid #e8a840; background: rgba(232, 168, 64, 0.12);
  border-radius: 10px; padding: 12px 14px; margin: 10px 0 4px; }
.ep-guide-title { font-size: 13px; font-weight: 600; color: var(--text); margin-bottom: 8px; }
.ep-guide-steps { font-size: 12.5px; color: var(--text); display: flex;
  flex-direction: column; gap: 4px; line-height: 1.7; }
.ep-guide-steps code { font-family: Consolas, monospace; background: rgba(128, 128, 160, 0.18);
  border-radius: 4px; padding: 1px 5px; }
.ep-guide-hint { font-size: 11.5px; color: var(--dim); margin-top: 8px; word-break: break-all; }
.ep-enable { margin: 6px 0 8px; }
.ep-enable-note { font-size: 12.5px; color: var(--text); margin-top: 6px; word-break: break-all; }
/* ---- 运行台账（V1-R1）：任务详情内的 run 列表与回放 ---- */
.runsec { margin-top: 14px; padding-top: 10px; border-top: 1px solid var(--line); }
.runsec-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.runsec-head .hint { flex: 1; }
.runrow { border: 1px solid var(--line); border-radius: 8px; margin-bottom: 6px; overflow: hidden; }
.runrow-head { display: flex; align-items: center; gap: 8px; padding: 7px 10px; font-size: 12.5px; }
.runrow-head:hover { background: rgba(128, 140, 180, 0.12); }
.runrow-head.disabled { opacity: 0.55; cursor: not-allowed; }
.run-badge { font-size: 11px; padding: 1px 7px; border-radius: 999px;
  background: rgba(128, 140, 180, 0.18); color: var(--dim); }
.run-badge.o-done { background: rgba(64, 180, 120, 0.18); color: #4cbf87; }
.run-badge.o-blocked { background: rgba(220, 120, 90, 0.18); color: #e08a6a; }
.run-badge.o-cancelled { background: rgba(160, 160, 160, 0.18); color: var(--faint); }
.run-badge.o-open { background: rgba(90, 140, 255, 0.18); color: #6f9dff; }
.run-time { font-family: Consolas, monospace; color: var(--dim); }
.run-meta { flex: 1; color: var(--faint); overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
.run-toggle { color: var(--faint); }
/* Phase 1：任务详情里的前置任务概览与跳转 */
.deplist { margin-top: 6px; border: 1px solid var(--line); border-radius: 6px;
  padding: 6px 8px; background: rgba(128, 140, 180, 0.05); }
.deplist-head { font-size: 11.5px; color: var(--faint); margin-bottom: 4px; }
.depitem { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; padding: 3px 0; }
.deplink { border: 0; background: none; padding: 0; color: var(--acc, #5b8cff);
  font: inherit; font-size: 12px; cursor: pointer; text-align: left; }
.deplink:disabled { color: var(--faint); cursor: default; }
.deplink .ms { font-size: 13px; margin-right: 2px; vertical-align: -2px; }
.depsum { flex-basis: 100%; font-size: 11.5px; color: var(--dim);
  white-space: pre-wrap; overflow: hidden; text-overflow: ellipsis;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.run-detail { padding: 8px 10px; border-top: 1px dashed var(--line); }
.run-actions { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 6px; }
.run-artifact { max-height: 240px; overflow: auto; background: rgba(0, 0, 0, 0.22);
  border-radius: 6px; padding: 8px; font: 11.5px/1.5 Consolas, monospace;
  white-space: pre-wrap; word-break: break-all; margin: 0 0 8px; }
.run-events { max-height: 260px; overflow: auto; border: 1px solid var(--line);
  border-radius: 6px; }
.run-event { display: flex; gap: 6px; padding: 3px 7px;
  font: 11.5px/1.5 Consolas, monospace; border-bottom: 1px solid rgba(128, 140, 180, 0.1); }
.run-event:last-child { border-bottom: none; }
.re-seq { color: var(--faint); min-width: 26px; text-align: right; }
.re-type { color: #6f9dff; min-width: 92px; }
.re-ts { color: var(--faint); min-width: 58px; }
.re-data { flex: 1; color: var(--text); word-break: break-all; white-space: pre-wrap; }
</style>

<!-- NModal 瞬移到 body 后 scoped :deep() 够不到 NCard 内部节点：这两条须全局。
     类名 tsheet 是本组件专属（移动端任务全屏抽屉），不与其它样式冲突 -->
<style>
/* 项目卡片由固定标题、可滚动正文和固定操作区组成。 */
.project-dialog.n-card { display: flex; flex-direction: column; overflow: hidden; margin: 24px auto; }
@media (max-width: 768px) {
  .project-dialog-mobile.n-card { margin: 0; border-radius: 0; }
  .project-dialog-mobile.n-card > .n-card-header { padding: max(14px, env(safe-area-inset-top)) max(12px, env(safe-area-inset-right)) 14px max(12px, env(safe-area-inset-left)); }
}
.tsheet .n-card-header { display: none; }
.tsheet .n-card__content {
  display: flex; flex-direction: column;
  height: calc(100% - 2px); overflow-y: auto; padding-bottom: 24px;
}
</style>
