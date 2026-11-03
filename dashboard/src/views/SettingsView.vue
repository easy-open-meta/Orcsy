<script setup lang="ts">
import { NButton, NCard, NInput, NInputNumber, NSelect, NSwitch, NTag } from 'naive-ui'
import { computed, reactive, ref, watch } from 'vue'
import { api, type MirrorRowT, type ModelInfo } from '../api'
import { beginRestartWait, openDirBrowser, openGuide, openProjectEdit, openProjectNew, openTaskNew, store } from '../store'
import { cmpVersion, fmtTokens } from '../utils'

const message = () => window.$message

// ---- 草稿模式：首份数据到后回填一次，之后轮询不覆盖未保存的输入 ----
const draft = reactive({
  provider: 'openai' as 'openai' | 'anthropic',
  model: '', base_url: '', api_key: '',
  temperature: 0.2, max_tokens: 8192, max_context_tokens: 131072,
  enable_thinking: false, timeout_seconds: 300,
  interval: 300, parallel: 1, port: 5116, retention: 7, max_total_mb: 200,
})
// ---- 软件更新配置草稿 ----
const updDraft = reactive({ repo: '', token: '', auto_check: true })
// ---- Agent 工具开关草稿（全局默认；只上报关闭项，未列出 = 启用） ----
const toolsCatalog = ref<{ name: string; description: string }[]>([])
const toolsDraft = reactive<Record<string, boolean>>({})
// ---- 命令白名单开关草稿（全局默认；只上报禁用项，未列出 = 放行） ----
const commandsCatalog = ref<string[]>([])
const commandsDraft = reactive<Record<string, boolean>>({})
// ---- Agent 子进程环境变量草稿（多行 KEY=VALUE，代理/国内镜像） ----
const envDraft = ref('')
const toolsSaving = ref(false)
// ---- 访问权限草稿（全局默认；项目可在对话框里逐个覆盖） ----
const permOptions = [
  { label: '变更前询问（发生变更时询问：写/改/删/移文件、执行命令都挂起等批准）', value: 'ask_on_change' },
  { label: '自主模式（有风险时询问：仅 delete_file / move_file / run_command）', value: 'autonomous' },
  { label: '完全访问（减少确认次数：一律不询问，与历史行为一致）', value: 'full_access' },
]
const permDraft = reactive({ mode: 'full_access' as string, timeout_minutes: 30 })
// ---- 镜像源管理与测速选源（草稿态：自定义源可增删、内置/自定义皆可禁用；
//      测速结果是服务端数据，随轮询即时刷新——Agent 自测后看板也能看到） ----
const mirrorTesting = ref(false)
const mirrorTestingCat = ref<string | null>(null)
const mirrorApplying = ref(false)
const mirrorsSaving = ref(false)
const mirrorCustom = ref<Record<string, string[]>>({})   // 草稿：类别 → 用户添加 URL
const mirrorDisabled = ref<string[]>([])                 // 草稿：禁用的源 URL
const mirrorSpeeds = ref<Record<string, number | null>>({})
const mirrorTestedAt = ref('')
const addCat = ref('goproxy')
const addUrl = ref('')
let inited = false
watch(() => store.state, (s) => {
  if (!s || inited) return
  inited = true
  updDraft.repo = s.update_cfg?.repo ?? ''
  updDraft.token = s.update_cfg?.token ?? ''
  updDraft.auto_check = s.update_cfg?.auto_check ?? true
  toolsCatalog.value = s.tools_catalog ?? []
  // finish 只是展示行（常开且禁用），不进草稿、不参与保存
  for (const t of toolsCatalog.value) {
    if (t.name === 'finish') continue
    toolsDraft[t.name] = s.tools_cfg?.[t.name] ?? true
  }
  commandsCatalog.value = s.commands_catalog ?? []
  for (const c of commandsCatalog.value) commandsDraft[c] = s.commands_cfg?.[c] ?? true
  envDraft.value = Object.entries(s.env_cfg ?? {})
    .map(([k, v]) => `${k}=${v}`).join('\n')
  permDraft.mode = s.permissions_cfg?.mode ?? 'full_access'
  permDraft.timeout_minutes = s.permissions_cfg?.timeout_minutes ?? 30
  syncMirrorDraft()
  mirrorSpeeds.value = s.mirror_speeds?.results ?? {}
  mirrorTestedAt.value = s.mirror_speeds?.tested_at ?? ''
  draft.provider = s.llm.provider
  // 清洗历史 bug 写入的模型名：下拉标注「（977K）」曾被当成 id 存进配置
  draft.model = s.llm.model.replace(/（\d+K）$/, '')
  draft.base_url = s.llm.base_url
  draft.api_key = s.llm.api_key || ''
  draft.temperature = s.llm.temperature
  draft.max_tokens = s.llm.max_tokens
  draft.max_context_tokens = s.llm.max_context_tokens
  draft.enable_thinking = s.llm.enable_thinking
  draft.timeout_seconds = s.llm.timeout_seconds
  draft.interval = s.scheduler_cfg.interval_seconds
  draft.parallel = s.scheduler_cfg.max_parallel_agents
  draft.port = s.web_cfg.port
  draft.retention = s.logging?.retention_days ?? 7
  draft.max_total_mb = s.logging?.max_total_mb ?? 200
})

async function saveLlm(): Promise<void> {
  try {
    await api.config({ llm: {
      provider: draft.provider, model: draft.model, base_url: draft.base_url,
      api_key: draft.api_key, temperature: draft.temperature,
      max_tokens: draft.max_tokens, max_context_tokens: draft.max_context_tokens,
      enable_thinking: draft.enable_thinking,
      timeout_seconds: draft.timeout_seconds,
    } })
    message()?.('已保存并生效')
  } catch (e: any) { message()?.(e.message, 'error') }
  void import('../store').then(m => m.refresh(true))
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
const authPwd = ref('')
const authPwdTouched = ref(false)
const authPwdClear = ref(false)
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
    // 只上报关闭项：后端语义「未列出 = 默认启用」，config.yaml 保持干净
    const disabled = Object.fromEntries(
      Object.entries(toolsDraft).filter(([, v]) => !v))
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
      tools: disabled,
      commands: cmdsOff,
      env: { vars: envVars },
      permissions: { mode: permDraft.mode, timeout_minutes: permDraft.timeout_minutes },
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

async function delProject(name: string): Promise<void> {
  window.$dialog?.warning({
    title: '移除项目',
    content: `确定从系统移除项目 ${name}？（项目目录与任务文件不会被删除）`,
    positiveText: '移除',
    negativeText: '取消',
    async onPositiveClick() {
      try { await api.projectDelete(name); message()?.('已移除') }
      catch (e: any) { message()?.(e.message, 'error') }
      void import('../store').then(m => m.refresh(true))
    },
  })
}

// 用系统文件管理器打开项目根目录（后端调起，浏览器自身无此权限）
async function openFolder(p: { name: string; path: string }): Promise<void> {
  try {
    await api.projectOpenFolder(p.name)
    message()?.(`已打开 ${p.path}`)
  } catch (e: any) { message()?.(e.message, 'error') }
}

// ---- 软件更新：保存更新源 / 手动检查 / 手动导入本地安装包 ----
const updSaving = ref(false)
async function saveUpdate(): Promise<void> {
  updSaving.value = true
  try {
    await api.config({ update: {
      repo: updDraft.repo.trim(), token: updDraft.token, auto_check: updDraft.auto_check,
    } })
    message()?.('更新配置已保存')
  } catch (e: any) { message()?.(e.message, 'error') }
  finally { updSaving.value = false }
}

const updChecking = ref(false)
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
const localPkg = ref('')
const pkgInstalling = ref(false)
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


// ---- 拉取 Issues：项目绑定远程仓库后，把 open Issue 导入任务队列 ----
const syncing = ref('')
async function syncIssues(p: { name: string }): Promise<void> {
  syncing.value = p.name
  try {
    const r = await api.issuesSync(p.name)
    message()?.(r.message || '同步完成', 'success')
  } catch (e: any) { message()?.(e.message, 'error') }
  finally { syncing.value = '' }
  void import('../store').then(m => m.refresh(true))
}

// ---- 模型列表与规格自动填充 ----
const fetching = ref(false)
const testing = ref(false)
const modelSpecs = ref<ModelInfo[]>([])
// label 必须与 value 一致：naive 选中后按 label 回填输入框，带标注的 label
// 会被当成模型名存进配置（曾导致「step-5-preview（977K）」404）
const modelOptions = computed(() =>
  modelSpecs.value.map(m => ({ label: m.id, value: m.id })))
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
    <NCard title="🤖 大模型接入" size="small" class="scard">
      <template #header-extra><span class="hint">OpenAI 兼容协议覆盖 DeepSeek / GLM / Kimi / 通义 / Ollama 等；保存后立即生效。</span></template>
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
        <div class="f14"><label>API Key</label><NInput v-model:value="draft.api_key" type="password" show-password-on="click" placeholder="sk-..." /></div>
      </div>
      <div class="hint" v-if="draft.provider === 'anthropic'">
        Anthropic 协议：直接填官方/兼容服务公布的根地址即可，程序自动补 /v1（已含 /v1 则不重复）；多数兼容服务不提供 /models 列表端点，拉取 404 时直接手填模型名即可。
      </div>
      <div class="frow">
        <div class="f1"><label>采样温度 temperature</label><NInputNumber v-model:value="draft.temperature" :step="0.1" :min="0" :max="2" /></div>
        <div class="f1"><label>最大输出 max_tokens</label><NInputNumber v-model:value="draft.max_tokens" :step="256" :min="256" :max="200000" /></div>
      </div>
      <div class="frow">
        <div class="f1"><label>上下文窗口 max_context_tokens</label><NInputNumber v-model:value="draft.max_context_tokens" :step="1024" :min="4096" :max="2000000" /></div>
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

    <NCard title="⏱ 调度与看板" size="small" class="scard">
      <template #header-extra><span class="hint">扫描间隔、并行数保存后立即生效；看板端口下次启动生效。</span></template>
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

    <NCard title="🛠 Agent 工具与访问权限" size="small" class="scard">
      <template #header-extra><span class="hint">全局默认：项目未单独覆盖时按这里生效；保存后立即生效。</span></template>
      <div class="frow">
        <div class="f2"><label>访问权限模式（敏感操作是否挂起等待看板批准）</label>
          <NSelect v-model:value="permDraft.mode" :options="permOptions" /></div>
        <div class="f1"><label>审批超时（分钟，1~1440，无人响应自动拒绝）</label>
          <NInputNumber v-model:value="permDraft.timeout_minutes" :min="1" :max="1440" /></div>
      </div>
      <div class="hint pwd-hint">
        询问 = 任务挂起，顶栏出现「🔔 待审批」入口，批准/拒绝后继续；项目对话框可按项目覆盖模式。
      </div>
      <div class="tools-grid">
        <label v-for="t in toolsCatalog" :key="t.name" class="checkline tool-line">
          <!-- finish 为协议工具：常开且禁用的展示行，不可配置 -->
          <NSwitch v-if="t.name === 'finish'" size="small" :value="true" disabled />
          <NSwitch v-else size="small" v-model:value="toolsDraft[t.name]" />
          <span class="tool-name">{{ t.name }}</span>
          <span class="hint tool-desc">{{ t.description }}</span>
        </label>
      </div>
      <div class="hint tool-foot">finish 为任务结束的协议工具，始终可用（开关仅为展示）。</div>

      <label class="subhead">命令白名单（默认放行项，逐命令开关）</label>
      <div class="cmds-flow">
        <label v-for="c in commandsCatalog" :key="c" class="cmd-chip">
          <NSwitch size="small" v-model:value="commandsDraft[c]" />
          <span class="cmd-name">{{ c }}</span>
        </label>
      </div>
      <div class="hint tool-foot">
        关闭的命令从默认白名单移除（Agent 调 run_command 会被直接拒绝）；
        项目对话框的「命令白名单」仍可整表替换默认。
      </div>

      <label class="subhead">Agent 子进程环境变量（代理 / 国内镜像）</label>
      <NInput v-model:value="envDraft" type="textarea" :rows="3"
              placeholder="每行一个 KEY=VALUE（也接受 KEY: VALUE），例如：&#10;GOPROXY=https://goproxy.cn,direct&#10;HTTP_PROXY=http://127.0.0.1:7890" />
      <div class="hint tool-foot">
        注入 run_command 子进程：国内网络拉依赖失败（unexpected EOF / 超时）时配 GOPROXY、
        npm registry（HTTP_PROXY）、PIP_INDEX_URL 即可；不允许覆盖 PATH/SYSTEMROOT 等系统变量。
      </div>

      <div class="sfoot">
        <NButton type="primary" :loading="toolsSaving" @click="saveTools">保存工具与权限</NButton>
      </div>
    </NCard>

    <NCard title="🪞 镜像源管理与测速选源" size="small" class="scard">
      <template #header-extra><span class="hint">Agent 装环境/拉依赖前测速，哪个快用哪个。</span></template>
      <div class="mirror-ops">
        <NButton size="small" :loading="mirrorTesting" @click="testMirrors()">测速全部源</NButton>
        <NButton size="small" type="primary" :loading="mirrorApplying" @click="applyMirrors">
          应用最快源到环境变量</NButton>
        <span class="hint" v-if="mirrorTestedAt">上次测速：{{ mirrorTestedAt }}</span>
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

    <NCard title="⚙️ 应用" size="small" class="scard">
      <template #header-extra><span class="hint">本机应用行为设置，保存后立即生效。</span></template>
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
          <NInput v-model:value="updDraft.token" type="password" show-password-on="click" placeholder="可选" /></div>
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

    <NCard title="📁 受管项目" size="small" class="scard">
      <template #header-extra>
        <NButton type="primary" size="small" @click="openProjectNew">＋ 新建项目</NButton>
      </template>
      <div v-for="p in store.state.projects" :key="p.name" class="prow">
        <div class="prow-head">
          <NTag size="small" :bordered="false" :type="p.enabled ? 'info' : 'default'">{{ p.name }}</NTag>
          <NTag size="small" :bordered="false" :type="p.is_git_repo ? 'default' : 'warning'">{{ p.is_git_repo ? 'git' : '非 git' }}</NTag>
          <NTag v-if="!p.guide_exists" size="small" type="error" :bordered="false">缺指引</NTag>
          <NTag v-if="p.repo" size="small" :bordered="false" type="success">issues</NTag>
          <NTag v-if="p.issue_auto_pull" size="small" :bordered="false"
                :type="p.issue_auto_pull.last_error ? 'error' : 'success'">
            自动拉取 {{ Math.round(p.issue_auto_pull.interval / 60) }}min</NTag>
        </div>
        <div class="prow-meta">
          {{ p.path }} · 任务目录 {{ p.tasks_dir || 'tasks' }} · 每轮 {{ p.max_tasks_per_cycle || 1 }} 任务
        </div>
        <div class="ops-row">
          <NButton size="tiny" @click="openFolder(p)">打开文件夹</NButton>
          <NButton size="tiny" @click="openTaskNew(p.name)">新建任务</NButton>
          <NButton size="tiny" @click="openGuide(p.name)">编辑 AGENT.md</NButton>
          <NButton size="tiny" :loading="syncing === p.name" @click="syncIssues(p)">拉取 Issues</NButton>
          <NButton size="tiny" @click="openProjectEdit(p.name)">设置</NButton>
          <NButton size="tiny" quaternary type="error" @click="delProject(p.name)">移除</NButton>
        </div>
      </div>
      <div v-if="!store.state.projects.length" class="empty">还没有项目，点右上「新建项目」</div>
    </NCard>
  </div>
</template>

<style scoped>
.settings { padding: 0 20px; max-width: 1080px; display: flex; flex-direction: column; gap: 14px; }
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
</style>
