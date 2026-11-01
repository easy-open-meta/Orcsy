<script setup lang="ts">
import { NButton, NInput, NInputNumber, NModal, NSelect, NSwitch, NTag, useDialog, useMessage } from 'naive-ui'
import { computed, reactive, ref, watch } from 'vue'
import { api, type DirListing, type ModelInfo } from '../api'
import { refresh, ui, store } from '../store'

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
  // 远程仓库绑定：拉取 open Issues 入任务队列（单向）；token 与账号密码二选一
  use_repo: false, repo_url: '', repo_token: '', repo_user: '', repo_pass: '',
  repo_provider: '' as '' | 'github' | 'gitee' | 'gitlab' | 'gitea',
})
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
  pDraft.ll_key = pl?.api_key ?? ''
  pDraft.ll_model = pl?.model ?? ''
  pModels.value = []
  const pr = p?.repo ?? null
  pDraft.use_repo = !!pr
  pDraft.repo_url = pr?.url ?? ''
  pDraft.repo_token = pr?.token ?? ''
  pDraft.repo_user = pr?.username ?? ''
  pDraft.repo_pass = pr?.password ?? ''
  pDraft.repo_provider = (pr?.provider as '' | 'github' | 'gitee' | 'gitlab' | 'gitea') ?? ''
  pDraft.stack = ''; pDraft.run = ''; pDraft.test = ''
})

// ---- 专属模型绑定：拉取列表 / 测试连接（未填字段回退全局 llm 配置） ----
const pModels = ref<ModelInfo[]>([])
const pFetching = ref(false)
const pTesting = ref(false)
const pModelOptions = computed(() => pModels.value.map(m => ({ label: m.id, value: m.id })))
const llProviderOptions = [
  { label: '继承全局', value: '' },
  { label: 'openai（兼容协议）', value: 'openai' },
  { label: 'anthropic', value: 'anthropic' },
]
const effLlm = () => ({
  provider: pDraft.ll_provider || store.state?.llm.provider || 'openai',
  base_url: pDraft.ll_base.trim() || store.state?.llm.base_url || '',
  api_key: pDraft.ll_key.trim() || store.state?.llm.api_key || '',
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
      // 专属模型绑定：关=清除（null）；开=只提交非空字段（其余继承全局，后端清洗）
      llm: pDraft.use_llm ? {
        ...(pDraft.ll_provider ? { provider: pDraft.ll_provider } : {}),
        ...(pDraft.ll_base.trim() ? { base_url: pDraft.ll_base.trim() } : {}),
        ...(pDraft.ll_key.trim() ? { api_key: pDraft.ll_key.trim() } : {}),
        ...(pDraft.ll_model.trim() ? { model: pDraft.ll_model.trim() } : {}),
      } : null,
      // 远程仓库绑定：关或 URL 空=清除；平台显式选择优先，否则后端按 URL 识别
      repo: pDraft.use_repo && pDraft.repo_url.trim() ? {
        url: pDraft.repo_url.trim(),
        ...(pDraft.repo_provider ? { provider: pDraft.repo_provider } : {}),
        ...(pDraft.repo_token.trim() ? { token: pDraft.repo_token.trim() } : {}),
        ...(pDraft.repo_user.trim() ? { username: pDraft.repo_user.trim() } : {}),
        ...(pDraft.repo_pass ? { password: pDraft.repo_pass } : {}),
      } : null,
      original_name: ui.projectDlg.original ?? undefined,
      // 新建项目时把技术栈三项写进 AGENT.md；编辑项目不传（指引已存在不重写）
      ...(ui.projectDlg.original ? {} : {
        stack: pDraft.stack.trim(), run: pDraft.run.trim(), test: pDraft.test.trim(),
      }),
    })
    message.success(ui.projectDlg.original ? '已修改项目' : '已新建项目')
    ui.projectDlg = null
    store.view = 'settings'
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
const tReadonly = ref(false)
const tTitle = ref('任务')
const tDraft = reactive({
  id: '', idLocked: false, title: '', priority: 'P2',
  labels: '', assignee: '', description: '', err: '',
})
const PRI_OPTIONS = ['P0', 'P1', 'P2', 'P3', 'P4']
const priOptions = computed(() => {
  const opts = PRI_OPTIONS.map(v => ({ label: v, value: v }))
  if (tDraft.priority && !PRI_OPTIONS.includes(tDraft.priority))
    opts.push({ label: tDraft.priority, value: tDraft.priority })
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
  if (dlg.id) {
    // 先用看板缓存填，再拉全文覆盖（/api/state 描述截断 2000 字）；
    // 归档任务不在 tasks 里，从 archived 缓存找，最终以 /api/task 的全文为准
    const p = store.state?.projects.find(x => x.name === dlg.project)
    const t = p?.tasks.find(x => x.id === dlg.id) || p?.archived.find(x => x.id === dlg.id)
    if (t) {
      tDraft.title = t.title; tDraft.priority = t.priority
      tDraft.labels = t.labels.join(', '); tDraft.assignee = t.assignee
      tDraft.description = t.description
    }
    try {
      const r = await api.taskFull(dlg.project, dlg.id)
      if (r.description) tDraft.description = r.description
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
      description: tDraft.description,
    })
    message.success('任务已保存')
    ui.taskDlg = null
  } catch (e: any) { tDraft.err = e.message }
  void refresh(true)
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
const scanning = ref(false)
const importing = ref(false)
const scanErr = ref('')

const checkedPaths = computed(() =>
  scanSources.value.flatMap(s => s.skills)
    .filter(k => scanChecked.value[k.path]).map(k => k.path))

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
const bManual = ref('')
const bNewFolder = ref('')
const bErr = ref('')

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
      <div class="f1"><label>工具循环轮数上限</label><NInputNumber v-model:value="pDraft.max_turns" :min="5" :max="200" /></div>
      <div class="f1"><label>工作分支（可选，默认当前分支）</label><NInput v-model:value="pDraft.branch" placeholder="main" /></div>
    </div>
    <label>命令白名单（逗号分隔，留空用默认：python/pytest/node/npm/git…）</label>
    <NInput v-model:value="pDraft.cmds" placeholder="python, pytest, npm, dotnet" />
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
                  :placeholder="store.state?.llm.api_key ? '留空继承全局密钥' : 'sk-...'" /></div>
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
                  placeholder="ghp_... / gitee私人令牌 / glpat-... / gitea token" /></div>
        <div class="f1"><label>账号 + 密码（可选，GitLab / Gitee / Gitea 支持）</label>
          <div class="duo">
            <NInput v-model:value="pDraft.repo_user" placeholder="username" />
            <NInput v-model:value="pDraft.repo_pass" type="password" show-password-on="click"
                    placeholder="password" />
          </div></div>
      </div>
      <div class="hint" style="margin-top:6px">
        认证二选一：token 优先；GitHub 只支持 token。自托管 Gitea / GitLab 请在「平台」里显式选择。绑定后在项目卡片点「拉取 Issues」导入任务，优先级默认 P2（带 P0~P4 标签按标签）。
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
    <label class="checkline" style="margin-top:4px">
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
      <div class="f1"><label>指派（留空任意 Agent）</label><NInput v-model:value="tDraft.assignee" :disabled="tReadonly" /></div>
    </div>
    <label>任务描述（Markdown 正文）</label>
    <NInput v-model:value="tDraft.description" type="textarea" :disabled="tReadonly" :autosize="{ minRows: 7, maxRows: 16 }" />
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
      <NButton size="small" @click="browseTo(bStack?.parent || '')" :disabled="!bStack?.parent">⬆ 上一级</NButton>
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
.geditor { font: 12.5px/1.7 Consolas, "Cascadia Mono", monospace; }
.keditor { font: 12.5px/1.7 Consolas, "Cascadia Mono", monospace; }
.scan-group { margin-bottom: 10px; }
.scan-tool { font-size: 12.5px; font-weight: 600; margin-bottom: 4px; }
.scan-path { font-weight: 400; font-size: 11.5px; color: var(--faint); word-break: break-all; }
.scan-item { display: flex; gap: 8px; align-items: center; padding: 5px 8px; border: 1px solid var(--line); border-radius: 6px; margin-bottom: 4px; cursor: pointer; }
.scan-item:hover { background: rgba(128, 140, 180, 0.12); }
.scan-item input { flex: none; }
.scan-name { font-weight: 600; font-size: 12.5px; flex: none; }
.scan-desc { flex: 1; font-size: 12px; color: var(--dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
