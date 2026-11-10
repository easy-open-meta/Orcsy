<script setup lang="ts">
import { NButton, NCheckbox, NInput, NInputNumber, NModal, NSelect, NSwitch, NTag, useDialog, useMessage } from 'naive-ui'
import { computed, reactive, ref, shallowRef, watch } from 'vue'
import { api, type DirListing, type ModelInfo, type PermissionMode } from '../api'
import { navigateTo, refresh, ui, store } from '../store'

// 把 useMessage/useDialog 桥接到 window，供任意组件调用。
// bridge 兼容两种用法：message('文本') 默认 info；message.success('文本') 等——
// 本组件内统一用 message.xxx，window.$message 供其它组件直呼
const messageApi = useMessage()
const message = ((content: string, type: 'success' | 'warning' | 'error' | 'info' = 'info') => {
  (messageApi as any)[type]?.(content)
}) as any
Object.assign(message, messageApi)
window.$message = message
window.$dialog = useDialog() as any

// ---------- 项目对话框 ----------
const pDraft = reactive({
  name: '', path: '', tasks_dir: 'tasks', guide_file: 'AGENT.md',
  max_tasks: 1, max_turns: 40, branch: '', cmds: '', enabled: true,
  stack: '', run: '', test: '', err: '',
  // 专属模型绑定：use_llm 关=跟随全局；开=部分覆盖（空字段仍继承全局）
  use_llm: false, ll_provider: '' as '' | 'openai' | 'anthropic',
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
  review_rounds: null as number | null,
  debug_hosts_text: '',
  use_cam: false,
  cam_enabled: false,
  team: {
    enabled: false,
    execution_mode: 'parallel_worktree' as 'parallel_worktree' | 'sequential',
    max_qa_rounds: 3,
    max_split_depth: 1,
    chain_within_cycle: true,
    max_chain_steps_per_cycle: 10,
    fallback_role: 'backend',
    members: [] as any[],
  },
})

const teamMemberKeyTouched = reactive<Record<string, boolean>>({})
const gitIniting = shallowRef(false)

const roleSelectOptions = [
  { label: '👑 项目经理 (PM)', value: 'pm' },
  { label: '⚙️ 后端开发工程师', value: 'backend' },
  { label: '🎨 前端开发工程师', value: 'frontend' },
  { label: '🧪 测试工程师 (QA)', value: 'qa' },
  { label: '🧩 自定义专职角色', value: 'custom' },
]

function getMemberRoleIcon(role: string): string {
  const map: Record<string, string> = { pm: '👑', backend: '⚙️', frontend: '🎨', qa: '🧪', custom: '🧩' }
  return map[role] || '🤖'
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
  const existing = new Set(pDraft.team.members.map((m: any) => m.id))
  while (existing.has(id)) {
    id = `${base}_${n}`
    n++
  }
  pDraft.team.members.push({
    id, profile: '', enabled: true, name: '', role: 'custom',
    description: '', ll_model: '', ll_key: '', ll_key_masked: '',
    allow_test_scripts: false, extra_prompt: '',
  })
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
    message.success(res.message || '已成功初始化 Git 仓库')
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

const defaultProjectToolCategories = [
  {
    key: 'file_ops',
    label: '文件操作与编辑',
    icon: '📁',
    tools: ['read_file', 'write_file', 'edit_file', 'multi_edit', 'delete_file', 'move_file', 'list_dir'],
  },
  {
    key: 'code_search',
    label: '代码搜索与定位',
    icon: '🔍',
    tools: ['find_files', 'grep'],
  },
  {
    key: 'command_exec',
    label: '终端命令执行',
    icon: '💻',
    tools: ['run_command'],
  },
  {
    key: 'task_collab',
    label: '任务管理与协议',
    icon: '⚡',
    tools: ['create_tasks', 'finish'],
  },
  {
    key: 'external_net',
    label: '网络与外部知识',
    icon: '🌐',
    tools: ['read_skill', 'web_fetch', 'speedtest_mirrors'],
  },
  {
    key: 'local_debug',
    label: '本地开发服务与 API',
    icon: '🚀',
    tools: ['dev_server_start', 'dev_server_logs', 'dev_server_stop', 'http_request'],
  },
  {
    key: 'browser_auto',
    label: 'Chromium 浏览器自动化',
    icon: '🧭',
    tools: [
      'browser_navigate', 'browser_snapshot', 'browser_click',
      'browser_type', 'browser_wait', 'browser_screenshot',
      'browser_console', 'browser_network', 'browser_eval',
      'browser_close',
    ],
  },
]

const projectToolCategories = computed(() => {
  return store.state?.tool_categories ?? defaultProjectToolCategories
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
const PERM_LABEL: Record<string, string> = {
  ask_on_change: '变更前询问',
  autonomous: '自主模式',
  full_access: '完全访问',
}
const permOptions = computed(() => [
  { label: `跟随全局（当前：${PERM_LABEL[store.state?.permissions_cfg?.mode || 'full_access']}）`, value: '' },
  { label: '变更前询问（文件/命令变更都询问）', value: 'ask_on_change' },
  { label: '自主模式（仅高风险询问：删除/移动/执行命令）', value: 'autonomous' },
  { label: '完全访问（不询问，减少确认次数）', value: 'full_access' },
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
    message(approved ? '已批准，任务继续执行' : '已拒绝，模型将调整方案或按受阻收尾',
            approved ? 'success' : 'warning')
    delete alwaysAllow[id]
  } catch (e: any) { message(e.message, 'error') }
  finally { responding.value = '' }
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
  pDraft.review_ll_key = ''  // 不回填明文：掩码占位，留空保持原值
  reviewKeyTouched.value = false
  pDraft.review_ll_model = prl?.model ?? ''
  pDraft.review_rounds = p?.review_max_rounds ?? null
  pDraft.debug_hosts_text = (p?.debug_allowed_hosts ?? []).join('\n')
  pDraft.use_cam = p?.commit_and_merge !== undefined && p?.commit_and_merge !== null
  pDraft.cam_enabled = p?.commit_and_merge ?? false

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
    pDraft.team.members = (ptTeam.members || []).map((m: any) => ({
      id: m.id,
      profile: m.profile || '',
      enabled: m.enabled !== false,
      name: m.name || '',
      role: m.role || '',
      description: m.description || '',
      ll_model: m.llm?.model || '',
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
    pDraft.team.members = [
      { id: 'pm', profile: 'pm_default', enabled: true, name: '', role: 'pm', description: '', ll_model: '', ll_key: '', ll_key_masked: '', allow_test_scripts: false, extra_prompt: '' },
      { id: 'backend', profile: 'backend_default', enabled: true, name: '', role: 'backend', description: '', ll_model: '', ll_key: '', ll_key_masked: '', allow_test_scripts: false, extra_prompt: '' },
      { id: 'frontend', profile: 'frontend_default', enabled: true, name: '', role: 'frontend', description: '', ll_model: '', ll_key: '', ll_key_masked: '', allow_test_scripts: false, extra_prompt: '' },
      { id: 'qa', profile: 'qa_default', enabled: true, name: '', role: 'qa', description: '', ll_model: '', ll_key: '', ll_key_masked: '', allow_test_scripts: false, extra_prompt: '' },
    ]
  }

  pDraft.stack = ''; pDraft.run = ''; pDraft.test = ''
})

// ---- 专属模型绑定：拉取列表 / 测试连接（未填字段回退全局 llm 配置） ----
const pModels = ref<ModelInfo[]>([])
const pFetching = shallowRef(false)
const pTesting = shallowRef(false)
const pModelOptions = computed(() => pModels.value.map(m => ({ label: m.id, value: m.id })))
const llProviderOptions = [
  { label: '继承全局', value: '' },
  { label: 'openai（兼容协议）', value: 'openai' },
  { label: 'anthropic', value: 'anthropic' },
]
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
    message.success(`已获取 ${r.models.length} 个模型：在「模型」框下拉选择或输入`)
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
const repoProviderOptions = [
  { label: '自动识别（按 URL）', value: '' },
  { label: 'GitHub', value: 'github' },
  { label: 'Gitee', value: 'gitee' },
  { label: 'GitLab（含自托管）', value: 'gitlab' },
  { label: 'Gitea（含自托管）', value: 'gitea' },
]
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
    message.success(r.message || '连接成功')
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

function getMemberModelPlaceholder(m: any): string {
  if (m.profile && store.state?.agent_profiles?.[m.profile]?.llm?.model) {
    return `继承预设（${store.state.agent_profiles[m.profile].llm.model}）`
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

async function fetchMemberModels(m: any): Promise<void> {
  memberFetchingId.value = m.id
  try {
    const prof = m.profile ? store.state?.agent_profiles?.[m.profile] : null
    const provider = prof?.llm?.provider || pDraft.ll_provider || store.state?.llm.provider || 'openai'
    const baseUrl = (prof?.llm?.base_url || '').trim() || pDraft.ll_base.trim() || store.state?.llm.base_url || ''
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
    message.success(`已为角色「${m.name || m.role || m.id}」获取 ${r.models.length} 个模型，可在专属模型下拉框选择`)
  } catch (e: any) {
    message.error(e.message || '获取模型列表失败')
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

function loadMemberTemplate(m: any): void {
  const tpl = getMemberRoleTemplate(m.role || m.id)
  if (!tpl) {
    message.warning('未找到该角色的内置模版')
    return
  }
  m.extra_prompt = tpl
  message.success(`已为角色「${m.name || m.role || m.id}」载入内置模版`)
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
    message.success(`已获取 ${r.models.length} 个模型：在「模型」框下拉选择或输入`)
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
        ...(reviewKeyTouched.value ? { api_key: pDraft.review_ll_key.trim() } : {}),
        ...(pDraft.review_ll_model.trim() ? { model: pDraft.review_ll_model.trim() } : {}),
      } : null,
      review_max_rounds: pDraft.review_rounds,
      debug_allowed_hosts: pDraft.debug_hosts_text.trim()
        ? pDraft.debug_hosts_text.split(/[\n,;]+/).map(s => s.trim()).filter(Boolean)
        : null,
      commit_and_merge: pDraft.use_cam ? pDraft.cam_enabled : null,
      team: pDraft.team.enabled ? {
        enabled: true,
        execution_mode: pDraft.team.execution_mode,
        max_qa_rounds: pDraft.team.max_qa_rounds,
        max_split_depth: pDraft.team.max_split_depth,
        chain_within_cycle: pDraft.team.chain_within_cycle,
        max_chain_steps_per_cycle: pDraft.team.max_chain_steps_per_cycle,
        fallback_role: pDraft.team.fallback_role.trim() || undefined,
        members: pDraft.team.members.map((m: any) => {
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
          if (m.ll_model.trim() || teamMemberKeyTouched[m.id]) {
            const mLlm: any = {}
            if (m.ll_model.trim()) mLlm.model = m.ll_model.trim()
            if (teamMemberKeyTouched[m.id]) mLlm.api_key = m.ll_key.trim()
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
    message.success(ui.projectDlg.original ? '已修改项目' : '已新建项目')
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
const tTitle = shallowRef('任务')
const tDraft = reactive({
  id: '', idLocked: false, title: '', priority: 'P2',
  labels: '', assignee: '', description: '', err: '',
  depends_on: [] as string[],
  parent_id: '',
  qa_round: 0,
  dep_state: '',
})
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
    .filter(t => t.id !== currentId)
    .map(t => ({
      label: `[${t.priority}] ${t.title || t.id} (${t.status === 'done' ? '已完成' : t.status})`,
      value: t.id,
    }))
})

const projectAssigneeOptions = computed(() => {
  const p = activeTaskProject.value
  const opts: { label: string; value: string }[] = [
    { label: '未指派（由 PM 拆解或自动认领）', value: '' },
  ]
  if (p && p.team_members_effective && p.team_members_effective.length > 0) {
    for (const m of p.team_members_effective) {
      opts.push({
        label: `${m.icon} ${m.name} (${m.role})`,
        value: m.id,
      })
    }
  } else {
    opts.push(
      { label: '👑 项目经理 (pm)', value: 'pm' },
      { label: '⚙️ 后端开发 (backend)', value: 'backend' },
      { label: '🎨 前端开发 (frontend)', value: 'frontend' },
      { label: '🧪 测试工程师 (qa)', value: 'qa' },
    )
  }
  return opts
})

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
    }
    try {
      const r = await api.taskFull(dlg.project, dlg.id)
      if (r.description) tDraft.description = r.description
      if (r.depends_on) tDraft.depends_on = [...r.depends_on]
      if (r.parent_id) tDraft.parent_id = r.parent_id
      if (r.qa_round) tDraft.qa_round = r.qa_round
      if (r.error) tDraft.err = `注意：任务文件解析有问题 —— ${r.error}`
    } catch { /* 拉不到就用缓存 */ }
  }
})

async function saveTask(): Promise<void> {
  if (!ui.taskDlg || tReadonly.value) return
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
    })
    message.success('任务已保存')
    ui.taskDlg = null
  } catch (e: any) { tDraft.err = e.message }
  void refresh(true)
}

// ---------- 人工确认主分支提交与推送对话框 ----------
const commitVisible = computed({
  get: () => !!ui.commitDlg,
  set: (v: boolean) => { if (!v) ui.commitDlg = null },
})
const commitDraft = reactive({
  message: '',
  push: false,
  submitting: false,
  err: '',
})
const committingProject = computed(() =>
  ui.commitDlg ? store.state?.projects.find(x => x.name === ui.commitDlg?.project) ?? null : null)

watch(() => ui.commitDlg, (dlg) => {
  if (!dlg) return
  commitDraft.message = ''
  commitDraft.push = false
  commitDraft.submitting = false
  commitDraft.err = ''
})

async function submitManualCommit(): Promise<void> {
  if (!ui.commitDlg || !committingProject.value) return
  if (!commitDraft.message.trim()) {
    commitDraft.err = '请填写 Commit Message 提交说明'
    return
  }
  commitDraft.submitting = true
  commitDraft.err = ''
  try {
    const res = await api.commitProject({
      name: ui.commitDlg.project,
      message: commitDraft.message.trim(),
      push: commitDraft.push,
    })
    message.success(res.message || '已成功提交并把关入库！')
    ui.commitDlg = null
    void refresh(true)
  } catch (e: any) {
    commitDraft.err = e.message
  } finally {
    commitDraft.submitting = false
  }
}

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
    message.success('指引已保存')
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
  { label: '全局（所有项目生效）', value: 'global' },
  ...(store.state?.projects || []).map(p => ({ label: `仅 ${p.name}`, value: p.name })),
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
    message.success(kDraft.original ? '技能已保存' : '技能已新建')
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
    message.success(r.message || '已导入')
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
    message.success('已创建 ' + r.path)
    bNewFolder.value = ''
    await browseTo(r.path)
  } catch (e: any) { message.error(e.message) }
}
</script>

<template>
  <!-- 项目 新建/编辑 -->
  <NModal v-model:show="pVisible" preset="card" :title="ui.projectDlg?.original ? '编辑项目' : '新建项目'"
          style="width: min(560px, 92vw)">
    <div class="err" v-if="pDraft.err">{{ pDraft.err }}</div>
    <div class="duo">
      <div class="f1"><label>项目名（字母/数字/连字符）</label><NInput v-model:value="pDraft.name" placeholder="my-app" /></div>
      <div class="f1"><label>每轮最多任务数</label><NInputNumber v-model:value="pDraft.max_tasks" :min="1" :max="50" /></div>
    </div>
    <label>项目路径（本地目录）</label>
    <div class="pathline">
      <NInput v-model:value="pDraft.path" placeholder="D:\Repos\my-app" />
      <NButton @click="browsePath">选择…</NButton>
    </div>
    <div class="duo">
      <div class="f1"><label>任务目录（相对项目根）</label><NInput v-model:value="pDraft.tasks_dir" /></div>
      <div class="f1"><label>指引文件</label><NInput v-model:value="pDraft.guide_file" /></div>
    </div>
    <div class="duo">
      <div class="f1"><label>工具循环轮数上限（-1 = 不设上限）</label>
        <NInputNumber v-model:value="pDraft.max_turns" :min="-1" :max="200"
                      placeholder="40；填 -1 不设上限（靠 finish/强制停止兜底）" /></div>
      <div class="f1"><label>工作分支（可选，默认当前分支）</label><NInput v-model:value="pDraft.branch" placeholder="main" /></div>
    </div>
    <label>命令白名单（逗号分隔，留空用默认：python/pytest/node/npm/git…）</label>
    <NInput v-model:value="pDraft.cmds" placeholder="python, pytest, npm, dotnet" />
    <!-- 多模型 Agent 项目小组模式切换 -->
    <div style="margin-top:12px; padding: 10px 12px; background: rgba(0,128,255,0.05); border: 1px solid rgba(0,128,255,0.25); border-radius: 8px;">
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <label class="checkline" style="margin:0; gap:8px;">
          <NSwitch v-model:value="pDraft.team.enabled" />
          <span style="font-weight:600; font-size:13px; color:var(--text)">👥 多模型 Agent 项目小组模式 (Team)</span>
        </label>
        <NTag size="tiny" round :type="pDraft.team.enabled ? 'info' : 'default'">
          {{ pDraft.team.enabled ? '小组协同模式已启用' : '传统单兵模式' }}
        </NTag>
      </div>

      <template v-if="pDraft.team.enabled">
        <div style="margin-top:8px; font-size:12px; color:var(--dim); line-height:1.6">
          🛡 <b>工作区沙盒已强制启用</b>：所有写代码角色按任务在独立 Worktree 沙盒中编码，通过审查后安全合入主分支工作区（未提交状态），<b>默认不自动 commit 与 push</b>，最后提交完全由人工把关！
        </div>

        <div v-if="editingProject && !editingProject.is_git_repo" style="margin-top:6px; display:flex; align-items:center; gap:8px;">
          <NTag size="small" type="warning">非 Git 目录</NTag>
          <span style="font-size:12px; color:var(--text)">Worktree 沙盒隔离需在 Git 仓库运行：</span>
          <NButton size="tiny" type="primary" secondary :loading="gitIniting" @click="onGitInitProject">一键初始化 Git 仓库</NButton>
        </div>

        <div class="duo" style="margin-top:8px">
          <div class="f1"><label>并发策略</label>
            <NSelect v-model:value="pDraft.team.execution_mode" :options="[
              { label: '按任务独立 Worktree 安全并发 (推荐)', value: 'parallel_worktree' },
              { label: '项目内单任务串行互斥', value: 'sequential' },
            ]" />
          </div>
          <div class="f1"><label>无 PM 时未指派任务兜底角色</label>
            <NSelect v-model:value="pDraft.team.fallback_role" :options="pDraft.team.members.map(m => ({ label: `${m.name || m.id} (${m.role || m.id})`, value: m.id }))" clearable placeholder="留空则需手动指派" />
          </div>
        </div>

        <div class="duo" style="margin-top:6px">
          <div class="f1"><label>QA 最大回归轮数（防死循环熔断）</label>
            <NInputNumber v-model:value="pDraft.team.max_qa_rounds" :min="1" :max="10" />
          </div>
          <div class="f1" style="display:flex; flex-direction:column; justify-content:center;">
            <label class="checkline" style="margin-top:14px">
              <NSwitch size="small" v-model:value="pDraft.team.chain_within_cycle" />
              <span style="font-size:12px">同周期链式推进（前置完成立即接棒下一阶段）</span>
            </label>
          </div>
        </div>

        <!-- 小组成员列表 -->
        <div style="margin-top:10px;">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
            <span style="font-weight:600; font-size:12px; color:var(--text)">小组成员名册与专属模型配置：</span>
            <NButton size="tiny" quaternary type="primary" @click="addTeamMember">+ 添加成员</NButton>
          </div>

          <div style="display:flex; flex-direction:column; gap:8px;">
            <div v-for="(m, mIdx) in pDraft.team.members" :key="m.id" style="border:1px solid var(--line); border-radius:6px; padding:8px 10px; background:var(--card);">
              <div style="display:flex; justify-content:space-between; align-items:center;">
                <div style="display:flex; align-items:center; gap:6px;">
                  <NSwitch size="small" v-model:value="m.enabled" />
                  <span style="font-weight:600; font-size:12px; display:inline-flex; align-items:center;">{{ getMemberRoleIcon(m.role || m.id) }} {{ m.name || m.id }}</span>
                  <NTag size="tiny" round type="info">{{ (m.role || m.id).toUpperCase() }}</NTag>
                  <NTag size="tiny" round :bordered="false" style="font-family:Consolas, monospace; font-size:10.5px; background:rgba(128,128,160,0.12); color:var(--dim);">id: {{ m.id }}</NTag>
                </div>
                <div style="display:flex; align-items:center; gap:6px;">
                  <NButton size="tiny" secondary :loading="memberFetchingId === m.id" @click="fetchMemberModels(m)">获取模型列表</NButton>
                  <NButton size="tiny" quaternary type="error" @click="removeTeamMember(mIdx)">删除</NButton>
                </div>
              </div>

              <div v-if="m.enabled" style="margin-top:8px; display:grid; grid-template-columns: 1fr 1fr; gap:8px;">
                <div>
                  <label style="font-size:11px">引用全局预设</label>
                  <NSelect size="small" v-model:value="m.profile" :options="globalProfileOptions" clearable placeholder="自定义配置" />
                </div>
                <div>
                  <label style="font-size:11px">角色类型</label>
                  <NSelect size="small" v-model:value="m.role" :options="roleSelectOptions" />
                </div>
                <div>
                  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:2px;">
                    <label style="font-size:11px; margin:0">专属模型覆盖 (留空继承预设/全局)</label>
                    <NButton size="tiny" quaternary type="primary" :loading="memberFetchingId === m.id" @click="fetchMemberModels(m)" style="font-size:10px; padding:0 2px">获取模型列表</NButton>
                  </div>
                  <NSelect size="small" v-model:value="m.ll_model"
                           :options="availableTeamModelOptions"
                           filterable tag clearable
                           :placeholder="getMemberModelPlaceholder(m)"
                           @update:value="(v: string | null) => { m.ll_model = v || '' }" />
                </div>
                <div>
                  <label style="font-size:11px">API Key 覆盖</label>
                  <NInput size="small" v-model:value="m.ll_key" type="password" show-password-on="click"
                          :placeholder="m.ll_key_masked ? `已配置(${m.ll_key_masked})` : '留空继承'"
                          @update:value="teamMemberKeyTouched[m.id] = true" />
                </div>
              </div>

              <div v-if="m.enabled" style="margin-top:6px;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:2px;">
                  <label style="font-size:11px; margin:0; color:var(--dim)">角色专属提示词覆盖 (选填)</label>
                  <NButton size="tiny" quaternary type="primary" @click="loadMemberTemplate(m)" style="font-size:10px; padding:0 2px">📋 载入内置模版</NButton>
                </div>
                <NInput size="small" v-model:value="m.extra_prompt" type="textarea" :rows="2" :autosize="{ minRows: 2, maxRows: 6 }"
                        placeholder="留空继承全局预设提示词；亦可载入内置模版或填入本项目特定规范" />
              </div>

              <div v-if="m.enabled && (m.role === 'qa' || m.id === 'qa')" style="margin-top:6px;">
                <label class="checkline" style="margin:0">
                  <NSwitch size="small" v-model:value="m.allow_test_scripts" />
                  <span style="font-size:11.5px">允许 QA 编写自动化测试脚本并纳入提交</span>
                </label>
              </div>
            </div>
          </div>
        </div>
      </template>
    </div>

    <label class="checkline" style="margin-top:12px">
      <NSwitch v-model:value="pDraft.use_llm" />
      绑定专属模型（仅该模型处理本项目任务；关闭=跟随全局大模型配置）
    </label>
    <template v-if="pDraft.use_llm">
      <div class="duo" style="margin-top:6px">
        <div class="f1"><label>协议 provider</label>
          <NSelect v-model:value="pDraft.ll_provider" :options="llProviderOptions" /></div>
        <div class="f2"><label>模型 model</label>
          <NSelect v-model:value="pDraft.ll_model" :options="pModelOptions" filterable tag clearable
                   :placeholder="'留空继承全局（当前 ' + (store.state?.llm.model || '-') + '）'"
                   @update:value="(v: string | null) => { pDraft.ll_model = v || '' }" /></div>
      </div>
      <div class="duo">
        <div class="f1"><label>接口 base_url</label>
          <NInput v-model:value="pDraft.ll_base" :placeholder="(pDraft.ll_provider === 'anthropic' ? '填官方根地址即可（自动补 /v1）；' : '') + '留空继承全局（当前 ' + (store.state?.llm.base_url || '-') + '）'" /></div>
        <div class="f1"><label>密钥 api_key</label>
          <NInput v-model:value="pDraft.ll_key" type="password" show-password-on="click"
                  :placeholder="llKeyPlaceholder" @update:value="llKeyTouched = true" /></div>
      </div>
      <div class="hint" v-if="pDraft.ll_provider === 'anthropic'">
        Anthropic 协议：程序自动补 /v1（已含 /v1 则不重复）；多数兼容服务不提供 /models 列表端点，拉取 404 时直接手填模型名即可。
      </div>
      <div class="btnrow" style="margin-top:6px">
        <NButton size="small" :loading="pFetching" @click="fetchProjectModels">获取模型列表</NButton>
        <NButton size="small" :loading="pTesting" @click="testProjectModel">测试连接</NButton>
        <span class="hint">未填写的字段自动继承全局「大模型接入」配置</span>
      </div>
    </template>
    <label class="checkline" style="margin-top:12px">
      <NSwitch v-model:value="pDraft.use_review" />
      审查 Agent（任务合并前由独立模型审查，通过才归档；关闭=跟随全局开关）
    </label>
    <template v-if="pDraft.use_review">
      <div class="duo" style="margin-top:6px">
        <div class="f1"><label>协议 provider</label>
          <NSelect v-model:value="pDraft.review_ll_provider" :options="llProviderOptions" /></div>
        <div class="f2"><label>审查模型 model</label>
          <NSelect v-model:value="pDraft.review_ll_model" :options="rModelOptions" filterable tag clearable
                   :placeholder="'留空继承全局（当前 ' + (store.state?.review_cfg?.model || '未配置') + '）'"
                   @update:value="(v: string | null) => { pDraft.review_ll_model = v || '' }" /></div>
      </div>
      <div class="duo">
        <div class="f1"><label>接口 base_url</label>
          <NInput v-model:value="pDraft.review_ll_base" placeholder="留空继承全局审查模型配置" /></div>
        <div class="f1"><label>密钥 api_key</label>
          <NInput v-model:value="pDraft.review_ll_key" type="password" show-password-on="click"
                  :placeholder="reviewKeyPlaceholder" @update:value="reviewKeyTouched = true" /></div>
      </div>
      <div class="duo">
        <div class="f1"><label>返工审查轮数上限（-1 表示不上限）</label>
          <NInputNumber v-model:value="pDraft.review_rounds" :min="-1" :max="10"
                        placeholder="留空跟随全局（输入 -1 不设上限）" clearable /></div>
        <div class="f1 btnrow" style="align-items:flex-end">
          <NButton size="small" :loading="rFetching" @click="fetchReviewModels" style="margin-bottom:2px">获取模型列表</NButton>
        </div>
      </div>
      <div class="hint" v-if="reviewModelConflict" style="color:#d03050">⚠ {{ reviewModelConflict }}</div>
      <div class="hint" v-else>审查模型必须与任务模型不同；返工轮数留空 = 跟随全局（当前 {{ store.state?.review_cfg?.max_review_rounds === -1 ? '不上限' : (store.state?.review_cfg?.max_review_rounds ?? 2) + ' 次' }}）。输入 -1 表示不设上限持续返工。</div>
    </template>
    <label style="margin-top:12px">访问权限（人工审批模式）</label>
    <NSelect v-model:value="pDraft.perm_mode" :options="permOptions" />
    <div class="hint">询问 = 任务挂起等待看板顶栏「🔔 待审批」批准/拒绝；超时自动拒绝，任务不会永久挂起。</div>
    <label class="checkline" style="margin-top:12px">
      <NSwitch v-model:value="pDraft.use_tools" />
      覆盖 Agent 工具开关（关闭=跟随全局「Agent 工具能力」配置）
    </label>
    <template v-if="pDraft.use_tools">
      <div style="margin:8px 0;padding:8px 10px;background:rgba(0,128,255,0.06);border-radius:6px;border:1px solid rgba(0,128,255,0.2)">
        <div style="font-weight:600;font-size:12px;margin-bottom:6px">本地调试工具组快捷开关：</div>
        <div style="display:flex;flex-wrap:wrap;gap:12px">
          <label class="checkline" style="display:inline-flex;align-items:center;gap:6px">
            <NSwitch size="small" v-model:value="pBrowserGroupEnabled" />
            <span style="font-size:12px">浏览器调试 (browser)</span>
          </label>
          <label class="checkline" style="display:inline-flex;align-items:center;gap:6px">
            <NSwitch size="small" v-model:value="pDevServerGroupEnabled" />
            <span style="font-size:12px">开发服务 (dev_server)</span>
          </label>
          <label class="checkline" style="display:inline-flex;align-items:center;gap:6px">
            <NSwitch size="small" v-model:value="pDraft.tools['http_request']" />
            <span style="font-size:12px">本地 API (http_request)</span>
          </label>
        </div>
      </div>
      <div class="ptools-grouped" style="margin-top:6px; max-height:280px; overflow-y:auto; border:1px solid var(--line); border-radius:8px; padding:6px 10px;">
        <div v-for="cat in projectToolCategories" :key="cat.key" class="pcat-group">
          <div class="pcat-head">
            <div style="display:flex;align-items:center;gap:6px">
              <span>{{ cat.icon }}</span>
              <span style="font-weight:600;font-size:12px;color:var(--text)">{{ cat.label }}</span>
            </div>
            <div style="display:flex;align-items:center;gap:6px">
              <NTag size="tiny" round :type="pGetGroupToolsCount(cat.tools).enabled === pGetGroupToolsCount(cat.tools).total ? 'success' : 'warning'">
                {{ pGetGroupToolsCount(cat.tools).enabled }}/{{ pGetGroupToolsCount(cat.tools).total }}
              </NTag>
              <NButton size="tiny" quaternary @click="pToggleToolsGroup(cat.tools, true)">全选</NButton>
              <NButton size="tiny" quaternary @click="pToggleToolsGroup(cat.tools, false)">全关</NButton>
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
      <div class="hint" style="margin-top:4px">finish 为协议工具始终可用；其余开关保存后立即生效（下一轮任务按此执行）。</div>
    </template>
    <div style="margin-top:10px">
      <label style="font-size:12px;font-weight:bold">🌐 额外允许的调试主机（局域网测试机 / 内部域名）</label>
      <NInput v-model:value="pDraft.debug_hosts_text" type="textarea" :rows="2"
              placeholder="每行一个主机名或IP（如 192.168.1.50 或 dev.local，只追加，全局回环地址默认放行）" />
      <div class="hint">该项目测试时允许额外访问的主机白名单；链路本地与云元数据等硬禁区强制拦截。</div>
    </div>
    <label class="checkline" style="margin-top:12px">
      <NSwitch v-model:value="pDraft.use_cam" />
      覆盖 Git 代码提交与合并策略（关闭 = 跟随全局设置{{ store.state?.git_cfg?.commit_and_merge ? '开启' : '关闭' }}）
    </label>
    <template v-if="pDraft.use_cam">
      <label class="checkline" style="margin-top:6px;margin-left:16px">
        <NSwitch size="small" v-model:value="pDraft.cam_enabled" />
        任务完成后自动提交并合并代码（开启 = worktree 分支提交合并；关闭 = 直接保留在工作区）
      </label>
    </template>
    <label class="checkline" style="margin-top:12px">
      <NSwitch v-model:value="pDraft.use_repo" />
      绑定远程仓库 Issues（把 open Issue 拉取为任务；单向导入，状态不回写）
    </label>
    <template v-if="pDraft.use_repo">
      <div class="duo" style="margin-top:6px">
        <div class="f2"><label>仓库 URL{{ repoPlatform ? ' · ' + repoPlatform : '' }}</label>
          <NInput v-model:value="pDraft.repo_url" placeholder="https://github.com/owner/repo" /></div>
        <div class="f1"><label>平台（自托管必选）</label>
          <NSelect v-model:value="pDraft.repo_provider" :options="repoProviderOptions" /></div>
      </div>
      <div class="duo">
        <div class="f1"><label>访问 token（与账号密码二选一）</label>
          <NInput v-model:value="pDraft.repo_token" type="password" show-password-on="click"
                  :placeholder="repoTokenPlaceholder" @update:value="repoTokenTouched = true" /></div>
        <div class="f1"><label>账号 + 密码（可选，GitLab / Gitee / Gitea 支持）</label>
          <div class="duo">
            <NInput v-model:value="pDraft.repo_user" placeholder="username" />
            <NInput v-model:value="pDraft.repo_pass" type="password" show-password-on="click"
                    :placeholder="repoPassPlaceholder" @update:value="repoPassTouched = true" />
          </div></div>
      </div>
      <div class="hint" style="margin-top:6px">
        认证二选一：token 优先；GitHub 只支持 token。自托管 Gitea / GitLab 请在「平台」里显式选择。绑定后在项目卡片点「拉取 Issues」导入任务，优先级默认 P2（带 P0~P4 标签按标签）。
      </div>
      <div class="duo" style="margin-top:6px">
        <div class="f1"><label>自动拉取间隔（分钟，0 = 仅手动）</label>
          <NInputNumber v-model:value="pDraft.repo_auto_pull" :min="0" :step="5"
                        :disabled="!pDraft.repo_url.trim()"
                        placeholder="0 表示只在点「拉取 Issues」时拉取" /></div>
        <div class="f2"><label>上次自动拉取</label>
          <div class="hint" style="padding-top:6px">
            {{ repoAutoPullHint }}</div></div>
      </div>
      <div class="hint">
        填写间隔后后台线程每 N 分钟自动把 open Issue 拉取为任务（去重规则与手动拉取相同）。
      </div>
    </template>
    <template v-if="!ui.projectDlg?.original">
      <label style="margin-top:10px">
        技术栈（按路径自动探测预填，可修改；保存时写入 AGENT.md 的 Project 节，留空不填）
      </label>
      <div class="duo">
        <div class="f1"><label>技术栈 Stack</label><NInput v-model:value="pDraft.stack" placeholder="Node.js (Vue)" /></div>
        <div class="f1"><label>本地运行 Run</label><NInput v-model:value="pDraft.run" placeholder="npm run dev" /></div>
        <div class="f1"><label>测试 Test</label><NInput v-model:value="pDraft.test" placeholder="npm test" /></div>
      </div>
    </template>
    <label class="checkline" style="margin-top: 16px; padding-top: 10px; border-top: 1px solid var(--line);">
      <NSwitch v-model:value="pDraft.enabled" />
      启用该项目（停用后调度器跳过，配置保留）
    </label>
    <div class="dlg-actions">
      <NButton @click="ui.projectDlg = null">取消</NButton>
      <NButton type="primary" @click="saveProject">保存</NButton>
    </div>
  </NModal>

  <!-- 任务 编辑/新建/只读查看 -->
  <NModal v-model:show="tVisible" preset="card" :title="tTitle"
          style="width: min(560px, 92vw)">
    <div class="hint" v-if="tReadonly">该任务不在待办状态（已归档/执行中/受阻），只读查看；重新入队后再编辑。</div>
    <div class="hint" v-else>仅待办（open）状态的任务可编辑；执行中/受阻的任务请等本轮结束后再改。</div>
    <div class="err" v-if="tDraft.err">{{ tDraft.err }}</div>
    <div class="duo">
      <div class="f1"><label>任务 ID（留空自动生成：优先级-标题）</label><NInput v-model:value="tDraft.id" :disabled="tDraft.idLocked" placeholder="fix-login-timeout" /></div>
      <div class="f1"><label>优先级</label>
        <NSelect v-model:value="tDraft.priority" :options="priOptions" :disabled="tReadonly" /></div>
    </div>
    <label>标题</label>
    <NInput v-model:value="tDraft.title" :disabled="tReadonly" placeholder="一句话描述要做什么" />
    <div class="duo">
      <div class="f1"><label>标签（逗号分隔）</label><NInput v-model:value="tDraft.labels" :disabled="tReadonly" placeholder="bug, ui" /></div>
      <div class="f1">
        <label>指派执行角色</label>
        <NSelect v-model:value="tDraft.assignee" :options="projectAssigneeOptions" :disabled="tReadonly" filterable tag clearable placeholder="留空自动认领" />
      </div>
    </div>
    <div style="margin-bottom: 6px;">
      <label>前置依赖任务（全部完成后本任务方可认领执行）</label>
      <NSelect v-model:value="tDraft.depends_on" multiple filterable :options="projectTaskOptions" :disabled="tReadonly" placeholder="选择依赖的前置任务（可选）..." />
      <div v-if="tDraft.parent_id" class="hint" style="margin-top: 4px;">
        🔗 关联父需求/来源任务：{{ tDraft.parent_id }} {{ tDraft.qa_round ? `· 回归轮次 #${tDraft.qa_round}` : '' }}
      </div>
    </div>
    <label>任务描述（Markdown 正文）</label>
    <NInput v-model:value="tDraft.description" type="textarea" :disabled="tReadonly" :autosize="{ minRows: 7, maxRows: 16 }" />
    <!-- 结构化审查意见（从正文「## 审查结果」节解析）：severity 色标条目列表 -->
    <template v-if="reviewIssues.length">
      <label style="margin-top:10px">审查意见（独立审查 Agent 退回的问题清单）</label>
      <div class="review-issues">
        <div v-for="(it, idx) in reviewIssues" :key="idx" class="review-issue">
          <NTag size="tiny" :type="it.severity === 'blocker' ? 'error' : it.severity === 'major' ? 'warning' : 'default'"
                :bordered="false">{{ it.severity }}</NTag>
          <span class="ri-loc">{{ it.loc }}</span>
          <span class="ri-msg">{{ it.message }}</span>
        </div>
      </div>
    </template>
    <div class="dlg-actions">
      <NButton @click="ui.taskDlg = null">{{ tReadonly ? '关闭' : '取消' }}</NButton>
      <NButton v-if="!tReadonly" type="primary" @click="saveTask">保存</NButton>
    </div>
  </NModal>

  <!-- AGENT.md 编辑 -->
  <NModal v-model:show="gVisible" preset="card" class="gdlg"
          :title="ui.guideDlg ? `编辑指引 · ${ui.guideDlg.project}` : '编辑指引'"
          style="width: min(860px, 94vw)">
    <div class="hint" v-if="gDraft.file">{{ gDraft.file }}</div>
    <div class="err" v-if="gDraft.err">{{ gDraft.err }}</div>
    <NInput v-model:value="gDraft.content" type="textarea" class="geditor"
            :autosize="false" :input-props="{ spellcheck: false }"
            style="height: 55vh" />
    <div class="dlg-actions">
      <NButton @click="ui.guideDlg = null">取消</NButton>
      <NButton type="primary" @click="saveGuide">保存指引</NButton>
    </div>
  </NModal>

  <!-- 技能 新建/编辑 -->
  <NModal v-model:show="kVisible" preset="card"
          :title="ui.skillDlg?.original ? `编辑技能 · ${ui.skillDlg.original}` : '新建技能'"
          style="width: min(760px, 94vw)">
    <div class="hint">技能存放在 Orcsy 技能库（不落项目目录）；正文为 SKILL.md 的 Markdown 正文，frontmatter 由系统维护。</div>
    <div class="err" v-if="kDraft.err">{{ kDraft.err }}</div>
    <div class="duo">
      <div class="f1"><label>技能名（字母数字/下划线/连字符）</label><NInput v-model:value="kDraft.name" placeholder="vue-best-practices" /></div>
      <div class="f1"><label>适用范围</label>
        <NSelect v-model:value="kDraft.scope" :options="scopeOptions" /></div>
    </div>
    <label>描述（进入 Agent 系统提示词的索引，写清何时该用这个技能）</label>
    <NInput v-model:value="kDraft.description" placeholder="一句话说明技能的用途与适用场景" />
    <label class="checkline" style="margin-top:4px">
      <NSwitch v-model:value="kDraft.enabled" />
      启用（停用后不注入索引，内容保留）
    </label>
    <label>技能内容（Markdown 正文，Agent 通过 read_skill 工具按需读取）</label>
    <NInput v-model:value="kDraft.body" type="textarea" class="keditor"
            :autosize="false" :input-props="{ spellcheck: false }"
            style="height: 38vh" placeholder="# 何时使用 / # 步骤与规范 / # 注意事项 …" />
    <div class="dlg-actions">
      <NButton @click="ui.skillDlg = null">取消</NButton>
      <NButton type="primary" @click="saveSkill">保存技能</NButton>
    </div>
  </NModal>

  <!-- 扫描 AI 工具技能 -->
  <NModal v-model:show="scanVisible" preset="card" title="扫描 AI 工具的本地技能"
          style="width: min(660px, 94vw)">
    <div class="hint">扫描常见 AI 工具的用户级技能目录（ZCode / 通用 Agent / Claude Code / Codex / OpenCode 等），勾选后整目录复制进 Orcsy 技能库；同一技能在多个工具里重复部署时只显示一次。找不到想要的可点「导入本地技能」手工选目录。</div>
    <div class="err" v-if="scanErr">{{ scanErr }}</div>
    <div v-if="scanning" class="hint">扫描中…</div>
    <template v-else>
      <div v-if="!scanSources.length" class="hint">
        没有发现常见 AI 工具的技能目录。可手工选择目录导入，或在上述工具里先安装技能。
      </div>
      <div v-if="scanSources.length" class="scan-toolbar">
        <span class="hint">共 {{ allScanSkills.length }} 个技能，已选 {{ checkedPaths.length }} 个</span>
        <NButton size="tiny" @click="checkAll">全选</NButton>
        <NButton size="tiny" @click="invertChecked">反选</NButton>
      </div>
      <div v-for="src in scanSources" :key="src.path" class="scan-group">
        <div class="scan-tool">{{ src.tool }} <span class="scan-path">{{ src.path }}</span></div>
        <label v-for="k in src.skills" :key="k.path" class="scan-item">
          <input type="checkbox" v-model="scanChecked[k.path]" />
          <span class="scan-name">{{ k.name }}</span>
          <span class="scan-desc">{{ k.description || '（无描述）' }}</span>
          <NTag v-if="k.exists" size="small" :bordered="false">已导入</NTag>
        </label>
      </div>
    </template>
    <div class="dlg-actions">
      <NButton @click="ui.skillScanDlg = false">取消</NButton>
      <NButton type="primary" :loading="importing" :disabled="!checkedPaths.length" @click="importChecked">
        导入选中（{{ checkedPaths.length }}）
      </NButton>
    </div>
  </NModal>

  <!-- 目录/文件选择器（browserMode='file' 时点文件即选中，如手动导入更新包） -->
  <NModal v-model:show="bVisible" preset="card"
          :title="ui.browserMode === 'file' ? '选择本地文件' : '选择本地目录'"
          style="width: min(560px, 92vw)">
    <div class="brow-crumb">
      <!-- parent=="" 是盘符列表根入口（可点）；仅 null/undefined 才禁用 -->
      <NButton size="small" @click="browseTo(bStack?.parent ?? '')"
               :disabled="bStack?.parent == null">⬆ 上一级</NButton>
      <span class="bcur">{{ bStack?.path || '（请选择盘符 / 常用目录）' }}</span>
    </div>
    <div class="blist">
      <div v-for="d in bStack?.dirs || []" :key="d" class="bitem" @click="browseTo(d)">{{ d }}</div>
      <template v-if="ui.browserMode === 'file'">
        <div v-for="f in bStack?.files || []" :key="f" class="bitem bfile" @click="pickFile(f)">
          {{ f.split('\\').pop()?.split('/').pop() }}
        </div>
        <div v-if="bStack && !bStack.files?.length" class="bempty">（当前目录没有文件）</div>
      </template>
      <div v-else-if="bStack && !bStack.dirs.length" class="bempty">{{ bStack.note || '（没有子目录，可直接用下面的路径框）' }}</div>
    </div>
    <div class="pathline" style="margin-bottom:8px">
      <NInput v-model:value="bNewFolder" size="small" placeholder="在当前目录下新建文件夹，输入名称"
              @keydown.enter="newFolder" />
      <NButton size="small" @click="newFolder">＋ 新建文件夹</NButton>
    </div>
    <div class="pathline">
      <NInput v-model:value="bManual" placeholder="或直接粘贴完整路径" @keydown.enter="pickPath" />
      <NButton type="primary" @click="pickPath">{{ ui.browserMode === 'file' ? '使用此文件' : '使用此目录' }}</NButton>
    </div>
    <div class="dlg-actions"><NButton @click="ui.browserDlg = false">取消</NButton></div>
  </NModal>

  <!-- 人工审批面板：列表绑定 store.state.approvals，5s 轮询自动刷新；
       本面板故意不暂停轮询（store.anyModal 不含 approvalDlg），处理结果即时可见 -->
  <NModal v-model:show="approvalVisible" preset="card" title="🔔 待人工审批"
          style="width: min(640px, 92vw)">
    <div class="hint">
      Agent 按访问权限模式（变更前询问 / 自主模式）挂起了以下操作，等待你的决定；
      批准后任务继续执行。超过配置的时限无人响应将自动拒绝。
    </div>
    <div v-if="!approvals.length" class="appr-empty">
      当前没有挂起的审批请求（已处理或超时的条目会自动消失）。
    </div>
    <div v-for="a in approvals" :key="a.id" class="appr">
      <div class="appr-head">
        <NTag size="small" :bordered="false" type="info">{{ a.project }}</NTag>
        <NTag size="small" :bordered="false">{{ a.task_id }}</NTag>
        <NTag size="small" :bordered="false" type="warning">{{ a.tool }}</NTag>
        <span class="hint appr-wait">已等待 {{ fmtWaited(a.waited_seconds) }} ·
          {{ PERM_LABEL[a.mode] || a.mode }}</span>
      </div>
      <pre class="appr-args">{{ a.args_preview }}</pre>
      <div class="appr-ops">
        <NCheckbox v-model:checked="alwaysAllow[a.id]">本任务内始终允许 {{ a.tool }}</NCheckbox>
        <div class="btnrow">
          <NButton size="small" :disabled="responding === a.id"
                   @click="respondApproval(a.id, false)">拒绝</NButton>
          <NButton size="small" type="primary" :loading="responding === a.id"
                   @click="respondApproval(a.id, true)">批准</NButton>
        </div>
      </div>
    </div>
  </NModal>

  <!-- 人工确认主分支提交与推送对话框 -->
  <NModal v-model:show="commitVisible" preset="card" :title="`人工把关提交 · ${ui.commitDlg?.project}`"
          style="width: min(560px, 92vw)">
    <div style="font-size: 13px; color: var(--text); line-height: 1.6; margin-bottom: 10px;">
      主分支工作区目前有 <b>{{ committingProject?.pending_git_changes?.count || 0 }}</b> 个已通过测试联调但未提交的文件。
      确认后将由系统以规范的 commit message 提交至当前主分支（可选推送到远程仓库）。
    </div>
    <div v-if="committingProject?.pending_git_changes?.files?.length" style="max-height: 140px; overflow-y: auto; border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px; margin-bottom: 10px;">
      <div v-for="f in committingProject.pending_git_changes.files" :key="f" style="font-family: Consolas, monospace; font-size: 11.5px; color: var(--dim); padding: 2px 0;">
        {{ f }}
      </div>
    </div>
    <div style="margin-bottom: 10px;">
      <label style="font-size: 12px; font-weight: 600; margin-bottom: 4px; display: block;">提交说明 Commit Message</label>
      <NInput v-model:value="commitDraft.message" placeholder="如 feat: 完成用户登录与前后端联调测试" />
    </div>
    <div style="margin-bottom: 14px;">
      <label class="checkline">
        <NSwitch size="small" v-model:value="commitDraft.push" />
        <span style="font-size: 12.5px;">同时推送到远程仓库 (git push origin)</span>
      </label>
    </div>
    <div class="err" v-if="commitDraft.err">{{ commitDraft.err }}</div>
    <div class="dlg-actions">
      <NButton @click="ui.commitDlg = null">取消</NButton>
      <NButton type="primary" :loading="commitDraft.submitting" @click="submitManualCommit">确认提交主分支</NButton>
    </div>
  </NModal>
</template>

<style scoped>
.err { color: var(--n-error-color); font-size: 12.5px; margin-bottom: 6px; }
.hint { font-size: 12px; color: var(--faint); margin-bottom: 8px; }
.duo { display: flex; gap: 10px; }
.duo > div { flex: 1; display: flex; flex-direction: column; gap: 4px; }
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
.review-issue { display: flex; gap: 8px; align-items: baseline; font-size: 12.5px; }
.ri-loc { font-family: Consolas, "Cascadia Mono", monospace; color: var(--dim); flex: none; }
.ri-msg { color: var(--text); min-width: 0; word-break: break-word; }
</style>
