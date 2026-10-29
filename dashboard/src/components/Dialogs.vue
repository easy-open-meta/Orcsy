<script setup lang="ts">
import { NButton, NInput, NInputNumber, NModal, NSelect, NSwitch, useDialog, useMessage } from 'naive-ui'
import { computed, reactive, ref, watch } from 'vue'
import { api, type DirListing } from '../api'
import { refresh, ui, store } from '../store'

// 把 useMessage/useDialog 桥接到 window，供任意组件调用。
// bridge 兼容两种用法：window.$message('文本') 默认 info；window.$message.success('文本') 等。
const messageApi = useMessage()
const messageBridge = ((content: string, type: 'success' | 'warning' | 'error' | 'info' = 'info') => {
  (messageApi as any)[type]?.(content)
}) as any
Object.assign(messageBridge, messageApi)
window.$message = messageBridge
window.$dialog = useDialog() as any

// ---------- 项目对话框 ----------
const pDraft = reactive({
  name: '', path: '', tasks_dir: 'tasks', guide_file: 'AGENT.md',
  max_tasks: 1, max_turns: 40, branch: '', cmds: '', enabled: true,
  err: '',
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
})

function browsePath(): void {
  ui.browserPick = (path: string) => { pDraft.path = path }
  ui.browserStart = pDraft.path
  ui.browserDlg = true
}

async function saveProject(): Promise<void> {
  if (!ui.projectDlg) return
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
      original_name: ui.projectDlg.original ?? undefined,
    })
    message.success(ui.projectDlg.original ? '已修改项目' : '已新建项目')
    ui.projectDlg = null
    store.view = 'settings'
  } catch (e: any) { pDraft.err = e.message }
  void refresh(true)
}

// ---------- 任务对话框（编辑仅 open 可进，在打开入口已拦截） ----------
const tVisible = computed({
  get: () => !!ui.taskDlg,
  set: (v: boolean) => { if (!v) ui.taskDlg = null },
})
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
  tDraft.err = ''
  tDraft.id = dlg.id ?? ''
  tDraft.idLocked = dlg.id != null
  tDraft.title = ''; tDraft.priority = 'P2'; tDraft.labels = ''
  tDraft.assignee = ''; tDraft.description = ''
  if (dlg.id) {
    // 先用看板缓存填，再拉全文覆盖（/api/state 描述截断 2000 字）
    const p = store.state?.projects.find(x => x.name === dlg.project)
    const t = p?.tasks.find(x => x.id === dlg.id)
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
  if (!ui.taskDlg) return
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

// ---------- 目录选择器 ----------
const bVisible = computed({
  get: () => ui.browserDlg,
  set: (v: boolean) => { ui.browserDlg = v },
})
const bStack = ref<DirListing | null>(null)
const bManual = ref('')
const bErr = ref('')

async function browseTo(path: string): Promise<void> {
  try {
    bStack.value = await api.browse(path)
    bErr.value = ''
  } catch (e: any) { bErr.value = e.message }
}
watch(bVisible, (v) => {
  if (v) { bManual.value = ui.browserStart; void browseTo(ui.browserStart || '') }
})
function pickPath(): void {
  const v = bManual.value.trim() || bStack.value?.path || ''
  if (v) ui.browserPick?.(v)
  ui.browserDlg = false
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
    <label class="checkline" style="margin-top:4px">
      <NSwitch v-model:value="pDraft.enabled" />
      启用该项目（停用后调度器跳过，配置保留）
    </label>
    <div class="dlg-actions">
      <NButton @click="ui.projectDlg = null">取消</NButton>
      <NButton type="primary" @click="saveProject">保存</NButton>
    </div>
  </NModal>

  <!-- 任务 编辑/新建 -->
  <NModal v-model:show="tVisible" preset="card" :title="ui.taskDlg ? `任务 · ${ui.taskDlg.project}/${ui.taskDlg.id || '新建'}` : '任务'"
          style="width: min(560px, 92vw)">
    <div class="hint">仅待办（open）状态的任务可编辑；执行中/受阻的任务请等本轮结束后再改。</div>
    <div class="err" v-if="tDraft.err">{{ tDraft.err }}</div>
    <div class="duo">
      <div class="f1"><label>任务 ID（留空自动生成：优先级-标题）</label><NInput v-model:value="tDraft.id" :disabled="tDraft.idLocked" placeholder="fix-login-timeout" /></div>
      <div class="f1"><label>优先级</label>
        <NSelect v-model:value="tDraft.priority" :options="priOptions" /></div>
    </div>
    <label>标题</label>
    <NInput v-model:value="tDraft.title" placeholder="一句话描述要做什么" />
    <div class="duo">
      <div class="f1"><label>标签（逗号分隔）</label><NInput v-model:value="tDraft.labels" placeholder="bug, ui" /></div>
      <div class="f1"><label>指派（留空任意 Agent）</label><NInput v-model:value="tDraft.assignee" /></div>
    </div>
    <label>任务描述（Markdown 正文）</label>
    <NInput v-model:value="tDraft.description" type="textarea" :autosize="{ minRows: 7, maxRows: 16 }" />
    <div class="dlg-actions">
      <NButton @click="ui.taskDlg = null">取消</NButton>
      <NButton type="primary" @click="saveTask">保存</NButton>
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

  <!-- 目录选择器 -->
  <NModal v-model:show="bVisible" preset="card" title="选择本地目录" style="width: min(560px, 92vw)">
    <div class="brow-crumb">
      <NButton size="small" @click="browseTo(bStack?.parent || '')" :disabled="!bStack?.parent">⬆ 上一级</NButton>
      <span class="bcur">{{ bStack?.path || '（请选择盘符 / 常用目录）' }}</span>
    </div>
    <div class="blist">
      <div v-for="d in bStack?.dirs || []" :key="d" class="bitem" @click="browseTo(d)">{{ d }}</div>
      <div v-if="bStack && !bStack.dirs.length" class="bempty">{{ bStack.note || '（没有子目录，可直接用下面的路径框）' }}</div>
    </div>
    <div class="pathline">
      <NInput v-model:value="bManual" placeholder="或直接粘贴完整路径" @keydown.enter="pickPath" />
      <NButton type="primary" @click="pickPath">使用此目录</NButton>
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
</style>
