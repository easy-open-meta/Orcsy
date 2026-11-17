<script setup lang="ts">
import { NButton, NCard, NCheckbox, NInput, NPagination, NSelect, NSpin, NSwitch, NTag } from 'naive-ui'
import { computed, reactive, ref, shallowRef } from 'vue'
import { navigateTo, openCommitDlg, openGuide, openProjectEdit, openTaskNew, openTaskView, refresh, store, ui } from '../store'
import { isMobile } from '../mobile'
import { PRI_COLOR, ST_NAME, ST_TYPE } from '../theme'
import { parseLogEntries, relTime } from '../utils'
import type { ProjectT, TaskT } from '../api'
import { api } from '../api'
import { tNow as t, tFmt } from '../lang'

function roleIcon(role: string): string {
  const map: Record<string, string> = {
    pm: 'manage_accounts', backend: 'terminal', frontend: 'palette', qa: 'science',
  }
  return map[role] || 'smart_toy'
}
function roleLabel(role: string): string {
  const map: Record<string, string> = { pm: 'PM', backend: '后端', frontend: '前端', qa: '测试' }
  return map[role] || role
}

// ---- 看板仅展示有正在执行任务的项目（其余项目在「项目」页管理） ----
const runningProjects = computed(() => {
  const list = store.state?.projects || []
  return list.filter(p =>
    p.tasks.some(t => t.status === 'in_progress') ||
    (p.active_roles && Object.keys(p.active_roles).length > 0)
  )
})

// 运行日志按从新到旧展示；💭 思考条目在渲染层折叠成可展开块
const logEntries = computed(() => parseLogEntries(store.state?.log_tail || ''))

// 任务是否有挂起的人工审批（行内「⏸ 待审批」标签；点击顶栏 🔔 处理）
function waitingApproval(project: string, taskId: string): boolean {
  return (store.state?.approvals ?? []).some(
    a => a.project === project && a.task_id === taskId)
}

function stats(p: ProjectT): string {
  const c: Record<string, number> = {}
  for (const t of p.tasks) c[t.status] = (c[t.status] || 0) + 1
  const parts: string[] = []
  if (c.open) parts.push(tFmt('{p0} 待办', { p0: c.open }))
  if (c.in_progress) parts.push(tFmt('{p0} 执行中', { p0: c.in_progress }))
  if (c.blocked) parts.push(tFmt('{p0} 受阻', { p0: c.blocked }))
  return parts.join(' · ') || '无待办'
}

const STATUS_ORD: Record<string, number> = { open: 0, in_progress: 1, blocked: 2, cancelled: 3 }
const sorted = (p: ProjectT) =>
  [...p.tasks].sort((a, b) => {
    // 与 Agent 领取顺序一致：状态分组 → 优先级 → 同 P 内按拖拽 order
    // （无 order 的排在同 P 已排序任务之后）→ id
    const st = (STATUS_ORD[a.status] ?? 9) - (STATUS_ORD[b.status] ?? 9)
    if (st) return st
    if (a.rank !== b.rank) return a.rank - b.rank
    const oa = a.order, ob = b.order
    if (oa != null && ob != null && oa !== ob) return oa - ob
    if (oa != null && ob == null) return -1
    if (oa == null && ob != null) return 1
    return a.id.localeCompare(b.id)
  })

// ---- 拖拽排序：行内 HTML5 DnD，落点画指示线，drop 后整项目顺序落盘 ----
const dragId = shallowRef<string | null>(null)
const dropAt = shallowRef<{ id: string; before: boolean } | null>(null)

function onDragStart(e: DragEvent, t: TaskT): void {
  dragId.value = t.id
  ui.dragTask = t.id           // 暂停轮询，防 5s 刷新冲掉拖拽中的列表
  e.dataTransfer?.setData('text/plain', t.id)  // Firefox 要求 setData 才起拖
  if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move'
}
function onDragOver(e: DragEvent, t: TaskT): void {
  if (!dragId.value || dragId.value === t.id) return
  const el = e.currentTarget as HTMLElement
  const r = el.getBoundingClientRect()
  dropAt.value = { id: t.id, before: e.clientY < r.top + r.height / 2 }
}
function onDragEnd(): void {
  dragId.value = null
  dropAt.value = null
  ui.dragTask = null
}
async function onDrop(p: ProjectT, t: TaskT): Promise<void> {
  const src = dragId.value
  const at = dropAt.value
  onDragEnd()
  if (!src || !at || src === t.id) return
  const all = sorted(p)
  const moved = all.find(x => x.id === src)
  const anchorTask = all.find(x => x.id === at.id)
  if (!moved || !anchorTask) return
  // 排序只在同「状态 + 优先级」分组内有效（跨级拖拽会被优先级重新归位），
  // 明确提示而不是让用户以为拖拽失灵
  if (moved.status !== anchorTask.status || moved.rank !== anchorTask.rank) {
    window.$message?.(t('任务顺序仅在同一优先级分组内生效（跨优先级请先改优先级）'), 'warning')
    return
  }
  // 以当前显示序为基准重排：摘出被拖任务，插到落点行的前/后
  const ids = all.map(x => x.id).filter(id => id !== src)
  const anchor = ids.indexOf(at.id)
  if (anchor < 0) return
  ids.splice(at.before ? anchor : anchor + 1, 0, src)
  try {
    await api.taskReorder(p.name, ids)
  } catch (e: any) { window.$message?.(e.message, 'error') }
  void import('../store').then(m => m.refresh(true))
}

// ---- 触屏友好排序（移动端）：HTML5 DnD 在触屏上不可用，行内 ▲▼ 按钮替代——
// 同「状态 + 优先级」分组内相邻交换，落盘整项目顺序（与拖拽同一 /api/task/reorder）----
async function moveTask(p: ProjectT, t: TaskT, dir: -1 | 1): Promise<void> {
  const all = sorted(p)
  const idx = all.findIndex(x => x.id === t.id)
  const j = idx + dir
  if (idx < 0 || j < 0 || j >= all.length) return
  const neighbor = all[j]
  if (neighbor.status !== t.status || neighbor.rank !== t.rank) {
    window.$message?.(t('任务顺序仅在同一优先级分组内调整（跨级请先改优先级）'), 'warning')
    return
  }
  const ids = all.map(x => x.id)
  ;[ids[idx], ids[j]] = [ids[j], ids[idx]]
  try {
    await api.taskReorder(p.name, ids)
    void import('../store').then(m => m.refresh(true))
  } catch (e: any) { window.$message?.(e.message, 'error') }
}

// ---- 移动端运行日志：摘要轮询不含 60KB 日志尾部（省流量），打开日志区按需
// 全量拉取 + 手动刷新（不挂自动定时器，蜂窝网络下保持低耗） ----
const mobileLog = shallowRef('')
const mobileLogOpen = shallowRef(false)
const mobileLogLoading = shallowRef(false)
const mobileLogEntries = computed(() => parseLogEntries(mobileLog.value))

async function loadMobileLog(): Promise<void> {
  mobileLogLoading.value = true
  try {
    const s = await api.state(false)
    mobileLog.value = s.log_tail || ''
  } catch (e: any) {
    mobileLog.value = `加载失败：${e.message}`
  } finally { mobileLogLoading.value = false }
}

function toggleMobileLog(): void {
  mobileLogOpen.value = !mobileLogOpen.value
  if (mobileLogOpen.value && !mobileLog.value) void loadMobileLog()
}

// ---- 任务列表分页：批量导入 Issue 后卡片不至于无限长 ----
const TASK_PAGE_SIZE = 10
const taskPage = reactive<Record<string, number>>({})
function taskTotal(p: ProjectT): number {
  return sorted(p).length
}
function totalPages(p: ProjectT): number {
  return Math.max(1, Math.ceil(taskTotal(p) / TASK_PAGE_SIZE))
}
function curPage(p: ProjectT): number {
  // 任务变少（删除/归档）时自动收敛到合法页码，避免停在空页
  return Math.min(taskPage[p.name] || 1, totalPages(p))
}
function pageTasks(p: ProjectT): TaskT[] {
  const all = sorted(p)
  const start = (curPage(p) - 1) * TASK_PAGE_SIZE
  return all.slice(start, start + TASK_PAGE_SIZE)
}
function onPageChange(p: ProjectT, page: number): void {
  taskPage[p.name] = page
}

async function toggleProject(p: ProjectT, enabled: boolean): Promise<void> {
  try {
    await api.projectSave({
      name: p.name, path: p.path, enabled,
      tasks_dir: p.tasks_dir, guide_file: p.guide_file,
      max_tasks_per_cycle: p.max_tasks_per_cycle,
      max_agent_turns: p.max_agent_turns,
      allowed_branch: p.allowed_branch ?? undefined,
      allowed_commands: p.allowed_commands ?? undefined,
      // 绑定类字段必须原样回传：后端把缺失键当「清除」处理，
      // 不带就会把 llm/review_llm/repo/tools/permission_mode 绑定悄悄抹掉
      llm: p.llm ?? null,
      review_enabled: p.review_enabled ?? null,
      review_llm: p.review_llm ?? null,
      review_max_rounds: p.review_max_rounds ?? null,
      debug_allowed_hosts: p.debug_allowed_hosts ?? null,
      repo: p.repo ?? null,
      tools: p.tools ?? null,
      permission_mode: p.permission_mode ?? null,
      commit_and_merge: p.commit_and_merge ?? null,
      team: p.team ?? null,
      original_name: p.name,
    })
    window.$message?.(enabled ? tFmt('已启用 {p0}', { p0: p.name }) : tFmt('已停用 {p0}', { p0: p.name }))
  } catch (e: any) {
    window.$message?.(e.message, 'error')
  }
  void import('../store').then(m => m.refresh(true))
}

function clickTask(project: string, t: TaskT): void {
  // open 可编辑；其余状态（执行中/受阻/取消）与归档任务只读查看
  if (t.status === 'open') {
    ui.taskDlg = { project, id: t.id }
  } else {
    openTaskView(project, t.id)
  }
}

async function delTask(p: ProjectT, t: TaskT): Promise<void> {
  window.$dialog?.warning({
    title: t('删除任务'),
    content: tFmt('确定删除任务 {p0}？', { p0: t.id }),
    positiveText: t('删除'),
    negativeText: t('取消'),
    async onPositiveClick() {
      try { await api.taskDelete(p.name, t.id); window.$message?.('已删除') }
      catch (e: any) { window.$message?.(e.message, 'error') }
      void import('../store').then(m => m.refresh(true))
    },
  })
}

// 强制停止执行中的任务：置位停止信号，执行器在工具边界收尾为 cancelled
function stopTask(p: ProjectT, t: TaskT): void {
  window.$dialog?.warning({
    title: t('强制停止任务'),
    content: `确定立即停止正在执行的任务 ${t.id}？\n\n`
      + '当前工具调用（含正在跑的命令）会被中止，worktree 里未提交的改动将被丢弃，'
      + t('任务标记为「已取消」——之后可点「重新入队」再跑。'),
    positiveText: t('强制停止'),
    negativeText: t('取消'),
    async onPositiveClick() {
      try {
        const r = await api.taskStop(p.name, t.id)
        window.$message?.(r.message || t('已发送停止信号'), 'success')
      } catch (e: any) { window.$message?.(e.message, 'error') }
      void import('../store').then(m => m.refresh(true))
    },
  })
}

// 只从系统移除项目（配置移除，磁盘文件与任务全部保留）；彻底删除请手动删目录
async function delProject(p: ProjectT): Promise<void> {
  window.$dialog?.warning({
    title: t('移除项目'),
    content: tFmt('确定从系统移除项目 {p0}？（仅移出配置，目录与任务文件不会删除）', { p0: p.name }),
    positiveText: t('移除'),
    negativeText: t('取消'),
      async onPositiveClick() {
      try { await api.projectDelete(p.name); window.$message?.('已移除（文件已保留）') }
      catch (e: any) { window.$message?.(e.message, 'error') }
      void import('../store').then(m => m.refresh(true))
    },
  })
}

// ---- 拉取 Issues：项目绑定远程仓库后，把 open Issue 导入任务队列 ----
const syncing = shallowRef('')
async function syncIssues(p: { name: string }): Promise<void> {
  syncing.value = p.name
  try {
    const r = await api.issuesSync(p.name)
    window.$message?.(r.message || t('同步完成'), 'success')
  } catch (e: any) { window.$message?.(e.message, 'error') }
  finally { syncing.value = '' }
  void import('../store').then(m => m.refresh(true))
}

// ---- 刷新仓库信息：手动 init git 后点这里立即重取 git 状态 ----
const gitRefreshing = shallowRef('')
async function refreshGit(p: { name: string }): Promise<void> {
  gitRefreshing.value = p.name
  try {
    await refresh(true)
    const cur = store.state?.projects.find(x => x.name === p.name)
    window.$message?.(cur?.is_git_repo ? t('已识别为 git 仓库') : t('仍不是 git 仓库'), 'success')
  } catch (e: any) {
    window.$message?.(e.message, 'error')
  } finally { gitRefreshing.value = '' }
}

// ---- 批量删除任务：勾选行 → 批量操作条 → 确认删除；选择跨页保留 ----
const checked = reactive<Record<string, string[]>>({})
const batchDeleting = shallowRef('')
const checkedIds = (name: string): string[] => checked[name] ?? []
function isTaskChecked(name: string, id: string): boolean {
  return checkedIds(name).includes(id)
}
function toggleCheck(name: string, id: string, on: boolean): void {
  const cur = checkedIds(name).filter(x => x !== id)
  if (on) cur.push(id)
  checked[name] = cur
}
function clearChecked(name: string): void {
  delete checked[name]
}
function pageAllChecked(p: ProjectT): boolean {
  const ids = pageTasks(p).map(t => t.id)
  return ids.length > 0 && ids.every(id => checkedIds(p.name).includes(id))
}
function pageSomeChecked(p: ProjectT): boolean {
  const ids = pageTasks(p).map(t => t.id)
  const hit = ids.filter(id => checkedIds(p.name).includes(id)).length
  return hit > 0 && hit < ids.length
}
function checkAllPage(p: ProjectT, on: boolean): void {
  const pageIds = pageTasks(p).map(t => t.id)
  const rest = checkedIds(p.name).filter(id => !pageIds.includes(id))
  checked[p.name] = on ? [...rest, ...pageIds] : rest
}
async function batchDel(p: ProjectT): Promise<void> {
  const ids = [...checkedIds(p.name)]
  if (!ids.length) return
  window.$dialog?.warning({
    title: t('批量删除任务'),
    content: tFmt('确定删除选中的 {p0} 个任务？任务文件将被删除，不可恢复。', { p0: ids.length }),
    positiveText: t('删除'),
    negativeText: t('取消'),
    async onPositiveClick() {
      batchDeleting.value = p.name
      try {
        const r = await api.taskDeleteBatch(p.name, ids)
        window.$message?.(r.message || tFmt('已删除 {p0} 个', { p0: r.deleted }), 'success')
      } catch (e: any) { window.$message?.(e.message, 'error') }
      finally { batchDeleting.value = ''; clearChecked(p.name) }
      void import('../store').then(m => m.refresh(true))
    },
  })
}

// ---- 一键清空：删除全部活动任务（不含已归档） ----
const clearing = shallowRef('')
async function clearTasks(p: ProjectT): Promise<void> {
  window.$dialog?.warning({
    title: t('清空任务'),
    content: `确定清空项目 ${p.name} 的全部 ${p.tasks.length} 个任务？`
      + t('已归档（done）的任务不受影响；任务文件将被删除，不可恢复。'),
    positiveText: t('清空'),
    negativeText: t('取消'),
    async onPositiveClick() {
      clearing.value = p.name
      try {
        const r = await api.taskClear(p.name)
        window.$message?.(r.message || tFmt('已清空 {p0} 个', { p0: r.cleared }), 'success')
      } catch (e: any) { window.$message?.(e.message, 'error') }
      finally { clearing.value = ''; clearChecked(p.name) }
      void import('../store').then(m => m.refresh(true))
    },
  })
}

// 用系统文件管理器打开项目根目录（后端调起，浏览器自身无此权限）
async function openFolder(p: ProjectT): Promise<void> {
  try {
    await api.projectOpenFolder(p.name)
    window.$message?.(tFmt('已打开 {p0}', { p0: p.path }))
  } catch (e: any) { window.$message?.(e.message, 'error') }
}

async function setStatus(p: ProjectT, t: TaskT, st: string): Promise<void> {
  try { await api.taskStatus(p.name, t.id, st); window.$message?.('已更新') }
  catch (e: any) { window.$message?.(e.message, 'error') }
  void import('../store').then(m => m.refresh(true))
}

// ---- 快速添加任务：标题 + 优先级，ID 由后端自动生成（P{n}-标题）----
const quick = reactive<Record<string, { title: string; pri: string }>>({})
const q = (name: string) => quick[name] ?? (quick[name] = reactive({ title: '', pri: 'P2' }))
const priOptions = ['P0', 'P1', 'P2', 'P3', 'P4'].map(v => ({ label: v, value: v }))
const adding = ref(false)

async function quickAdd(p: ProjectT): Promise<void> {
  const st = q(p.name)
  const title = st.title.trim()
  if (!title) { window.$message?.('请输入任务标题', 'warning'); return }
  adding.value = true
  try {
    const r = await api.taskSave({
      project: p.name, id: '', title, priority: st.pri, status: 'open',
      labels: [], assignee: '', description: '',
    })
    window.$message?.(tFmt('任务已创建：{p0}', { p0: r.id }))
    taskPage[p.name] = 1 // 新任务立即可见
    st.title = ''
  } catch (e: any) {
    window.$message?.(e.message, 'error')
  } finally {
    adding.value = false
  }
  void import('../store').then(m => m.refresh(true))
}
</script>

<template>
  <div class="dash">
    <div class="stats" v-if="store.state">
      <NTag round :type="store.state.scheduler.running ? 'warning' : 'success'" size="large">
        {{ store.state.scheduler.running ? t('扫描执行中…') : t('调度空闲') }}
      </NTag>
      <NTag round size="large">{{ t('已完成周期') }} {{ store.state.scheduler.cycles_done }}</NTag>
      <NTag round size="large">{{ t('上次扫描') }} {{ relTime(store.state.scheduler.last_cycle_at) }}</NTag>
      <NTag round size="large">{{ t('扫描间隔') }} {{ store.state.scheduler.interval_seconds }}s</NTag>
      <NTag v-for="(v, k) in store.state.scheduler.last_cycle_stats" :key="k" round size="large">
        {{ k }}：{{ t('处理 {p0} 任务').replace('{p0}', String(v)) }}
      </NTag>
    </div>

    <div v-if="runningProjects.length" class="grid">
      <NCard v-for="p in runningProjects" :key="p.name" class="proj"
             :title="p.name" size="small">
        <template #header-extra>
          <div class="phead">
            <NSwitch size="small" :value="p.enabled" @update:value="(v: boolean) => toggleProject(p, v)" />
            <NTag size="small" :type="p.is_git_repo ? 'default' : 'warning'" :bordered="false">
              {{ p.is_git_repo ? 'git' : '非 git' }}
            </NTag>
            <NButton size="tiny" quaternary class="git-refresh" :loading="gitRefreshing === p.name"
                     :title="t('刷新仓库信息（在文件夹里手动 init git 后点这里立即生效）')"
                     @click="refreshGit(p)"><i class="ms sm">refresh</i></NButton>
            <NTag v-if="!p.guide_exists" size="small" type="error" :bordered="false">{{ t('缺指引') }}</NTag>
            <NTag v-if="p.team_enabled" size="small" type="info" :bordered="false"
                  :title="'已启用项目小组：' + (p.team_members_effective?.map(m => m.name).join('、') || '')">
              <i class="ms sm">group</i> 小组 ({{ p.team_members_effective?.length || 0 }})
            </NTag>
            <NButton v-if="p.pending_git_changes && p.pending_git_changes.count > 0"
                     size="tiny" type="warning" secondary
                     :title="t('主工作区有已通过测试但未提交的变更，点此人工把关确认提交')"
                     @click="openCommitDlg(p.name)">
              待人工提交 ({{ p.pending_git_changes.count }})
            </NButton>
            <NTag v-if="p.review?.warning" size="small" type="error" :bordered="false"
                  :title="p.review.warning">{{ t('审查模型冲突') }}</NTag>
            <NTag v-else-if="p.review?.model_configured && p.review?.enabled_effective"
                  size="small" type="success" :bordered="false"
                  :title="`审查模型 ${p.review.effective_model} · 返工上限 ${p.review.effective_max_rounds} 次`">
              审查开启</NTag>
            <NTag v-else-if="p.review_enabled === false || (p.review?.enabled_effective === false && p.review?.model_configured)"
                  size="small" :bordered="false"
                  :title="t('本项目已关闭审查门（跟随关闭优先于全局开关）')">{{ t('审查关闭') }}</NTag>
            <NTag v-if="p.issue_auto_pull" size="small" :bordered="false"
                  :type="p.issue_auto_pull.last_error ? 'error' : 'success'"
                  :title="p.issue_auto_pull.last_error || undefined">
              自动拉取 {{ Math.round(p.issue_auto_pull.interval / 60) }}min
            </NTag>
          </div>
        </template>
        <template #footer>
          <div class="card-footer-ops">
            <NButton size="tiny" @click="openFolder(p)">{{ t('打开文件夹') }}</NButton>
            <NButton size="tiny" @click="openTaskNew(p.name)">{{ t('新建任务') }}</NButton>
            <NButton size="tiny" @click="openGuide(p.name)">AGENT.md</NButton>
            <NButton size="tiny" :loading="syncing === p.name" @click="syncIssues(p)">{{ t('拉取 Issues') }}</NButton>
            <NButton size="tiny" @click="openProjectEdit(p.name)">{{ t('设置') }}</NButton>
            <NButton size="tiny" quaternary type="error" class="del-btn" @click="delProject(p)">{{ t('移除') }}</NButton>
          </div>
        </template>

        <div class="pathline">
          <span class="path">{{ p.path }}</span>
          <span class="counts">{{ stats(p) }}</span>
        </div>

        <div class="quickadd">
          <NInput v-model:value="q(p.name).title" size="small" :placeholder="t('新任务标题，回车快速添加')"
                  @keydown.enter="quickAdd(p)" />
          <NSelect v-model:value="q(p.name).pri" size="small" class="qpri" :options="priOptions" />
          <NButton size="small" type="primary" :loading="adding" @click="quickAdd(p)">{{ t('添加') }}</NButton>
        </div>

        <div v-if="p.tasks.length || p.parse_errors.length" class="batchbar">
          <NCheckbox size="small" :checked="pageAllChecked(p)" :indeterminate="pageSomeChecked(p)"
                     @update:checked="(v: boolean) => checkAllPage(p, v)">{{ t('全选本页') }}</NCheckbox>
          <template v-if="checkedIds(p.name).length">
            <span class="hint">已选 {{ checkedIds(p.name).length }} 个</span>
            <NButton size="tiny" type="error" :loading="batchDeleting === p.name"
                     @click="batchDel(p)">{{ t('批量删除') }}</NButton>
            <NButton size="tiny" quaternary @click="clearChecked(p.name)">{{ t('取消选择') }}</NButton>
          </template>
          <span class="batchbar-spacer" />
          <NButton size="tiny" type="warning" :loading="clearing === p.name"
                   @click="clearTasks(p)">清空任务（{{ p.tasks.length }}）</NButton>
        </div>

        <div class="tasks">
          <div v-for="e in p.parse_errors" :key="'e' + e.id" class="task err">
            <div class="t-main">
              <NTag size="small" type="error">{{ t('格式') }}</NTag>
              <span class="title">{{ e.id }}</span><NTag size="small" type="error">{{ t('解析失败') }}</NTag>
            </div>
            <div class="t-meta">{{ e.error }}</div>
          </div>

          <div v-for="t in pageTasks(p)" :key="t.id" class="task"
               :class="{ 'drop-before': dropAt?.id === t.id && dropAt.before,
                         'drop-after': dropAt?.id === t.id && !dropAt.before,
                         dragging: dragId === t.id }"
               draggable="true" :title="t('拖拽调整顺序（同一优先级内生效，同时决定 Agent 领取顺序）')"
               @dragstart="onDragStart($event, t)" @dragend="onDragEnd()"
               @dragover.prevent="onDragOver($event, t)" @drop.prevent="onDrop(p, t)">
            <div class="t-main clickable"
                 :title="t.status === 'open' ? '点击编辑任务内容' : '点击查看任务内容（只读）'"
                 @click="clickTask(p.name, t)">
              <span class="tcheck" @click.stop>
                <NCheckbox size="small" :checked="isTaskChecked(p.name, t.id)"
                           @update:checked="(v: boolean) => toggleCheck(p.name, t.id, v)" />
              </span>
              <NTag size="small" :color="{ color: PRI_COLOR[t.priority] || '#64748f', textColor: '#fff' }"
                    :bordered="false">{{ t.priority }}</NTag>
              <span class="title" :title="t.title">{{ t.title }}</span>
              <NTag size="small" :type="ST_TYPE[t.status] || 'default'" :bordered="false">
                {{ ST_NAME[t.status] || t.status }}
              </NTag>
              <NTag v-if="t.assignee" size="small" type="info" :bordered="false">
                <i class="ms sm">{{ roleIcon(t.assignee) }}</i> {{ roleLabel(t.assignee) }}
              </NTag>
              <NTag v-if="t.dep_state === 'waiting'" size="small" :bordered="false"
                    :title="t('等待前置依赖任务完成')">
                <i class="ms sm">hourglass_top</i> 等待前置 ({{ t.dep_done_count }}/{{ t.dep_total_count }})
              </NTag>
              <NTag v-else-if="t.dep_state === 'upstream_blocked'" size="small" type="error" :bordered="false"
                    :title="t('前置依赖任务存在受阻/取消')">
                <i class="ms sm">warning</i> 前置受阻
              </NTag>
              <NTag v-if="(t.qa_round || 0) > 0" size="small" type="warning" :bordered="false">
                <i class="ms sm">autorenew</i> 回归 #{{ t.qa_round }}
              </NTag>
              <NTag v-if="t.review_pending" size="small" type="info" :bordered="false"
                    :title="t('任务已完成，独立审查进行中或等待补审；审查通过后合并归档')">
                <i class="ms sm">rate_review</i> 待审查
              </NTag>
              <NTag v-else-if="(t.review_round || 0) > 0 && t.status !== 'done' && t.status !== 'cancelled'"
                    size="small" type="warning" :bordered="false"
                    :title="`已被审查退回 ${t.review_round} 次（返工轮）`">
                <i class="ms sm">rate_review</i> 返工 ×{{ t.review_round }}
              </NTag>
              <NTag v-if="waitingApproval(p.name, t.id)" size="small" type="warning"
                    :bordered="false" :title="t('Agent 有操作挂起等待人工审批，点顶栏「待审批」处理')">
                <i class="ms sm">pause_circle</i> 待审批
              </NTag>
            </div>
            <div class="t-meta">
              {{ t.id }}<template v-if="t.labels.length"> · {{ t.labels.join(', ') }}</template><template v-if="t.assignee"> · 角色 {{ t.assignee }}</template><template v-if="t.depends_on && t.depends_on.length"> · 前置 {{ t.depends_on.join(', ') }}</template>
            </div>
            <details v-if="t.description" class="tdesc">
              <summary>{{ t('描述') }}</summary>
              <pre>{{ t.description }}</pre>
            </details>
            <div class="ops-row">
              <!-- 触屏排序（仅移动端显示）：同优先级分组内上移/下移 -->
              <template v-if="isMobile">
                <NButton size="tiny" :title="t('上移（同一优先级分组内）')"
                         @click="moveTask(p, t, -1)"><i class="ms sm">keyboard_arrow_up</i></NButton>
                <NButton size="tiny" :title="t('下移（同一优先级分组内）')"
                         @click="moveTask(p, t, 1)"><i class="ms sm">keyboard_arrow_down</i></NButton>
              </template>
              <NButton v-if="t.status !== 'open' && t.status !== 'cancelled'" size="tiny"
                       @click="setStatus(p, t, 'open')">{{ t('重新入队') }}</NButton>
              <NButton v-if="t.status === 'open' || t.status === 'in_progress'" size="tiny"
                       @click="setStatus(p, t, 'done')">{{ t('标记完成') }}</NButton>
              <NButton v-if="t.status === 'in_progress'" size="tiny" type="warning" secondary
                       :title="t('立即中止正在执行的 Agent 循环（丢弃 worktree 改动，可重新入队）')"
                       @click="stopTask(p, t)">{{ t('强制停止') }}</NButton>
              <NButton size="tiny" quaternary type="error" @click="delTask(p, t)">{{ t('删除') }}</NButton>
            </div>
          </div>

          <div v-if="!p.tasks.length && !p.parse_errors.length" class="empty">
            暂无任务 · 点项目卡下方「新建任务」添加
          </div>
          <NPagination v-if="taskTotal(p) > TASK_PAGE_SIZE" size="small"
                       :page="curPage(p)" :page-count="totalPages(p)"
                       @update:page="(pg: number) => onPageChange(p, pg)" />
        </div>

          <details v-if="p.archived.length" class="arch">
            <summary>已归档（{{ p.archived.length }}）· 点击查看详情</summary>
            <div v-for="t in [...p.archived].reverse()" :key="t.id" class="task">
              <div class="t-main clickable" :title="t('点击查看任务详情（只读）')" @click="openTaskView(p.name, t.id)">
                <NTag size="small" :color="{ color: PRI_COLOR[t.priority] || '#64748f', textColor: '#fff' }" :bordered="false">{{ t.priority }}</NTag>
                <span class="title">{{ t.title }}</span>
                <NTag size="small" type="success" :bordered="false">{{ t('已完成') }}</NTag>
              </div>
              <div class="t-meta">{{ t.id }}</div>
            </div>
          </details>
        </NCard>
      </div>
    <div v-else class="dash-empty">
      <div style="font-size: 14px; font-weight: 500; color: var(--text); margin-bottom: 6px;">
        <i class="ms sm">local_cafe</i> {{ t('当前没有正在执行任务的项目 · 调度器空闲中') }}
      </div>
      <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap; justify-content:center;">
        <span class="hint">{{ t('全量项目与任务请在顶部「项目」页查看与管理') }}</span>
        <NButton size="small" secondary @click="navigateTo('projects')">{{ t('前往「项目」页') }} <i class="ms sm">arrow_forward</i></NButton>
      </div>
    </div>

    <!-- 运行日志：桌面端随 5s 轮询全量刷新；移动端摘要轮询省流量，
         打开日志区按需全量拉取 + 手动刷新（低耗优先） -->
    <details v-if="store.state && !isMobile" class="logsec" open>
      <summary>{{ t('运行日志（从新到旧，点击收起）') }}</summary>
      <div class="logbox">
        <div v-if="!logEntries.length" class="logempty">{{ t('（暂无日志）') }}</div>
        <template v-for="(e, i) in logEntries" :key="i">
          <!-- 模型思考条目：默认折叠，展开查看完整思考过程 -->
          <details v-if="e.thinking" class="think">
            <summary>{{ e.head }}</summary>
            <pre>{{ e.body || '（无正文）' }}</pre>
          </details>
          <div v-else class="logentry">{{ e.head }}<template v-if="e.body">{{ '\n' + e.body }}</template></div>
        </template>
      </div>
    </details>
    <details v-else-if="store.state" class="logsec" :open="mobileLogOpen">
      <summary @click.prevent="toggleMobileLog">
        <span class="logsum-title">运行日志{{ mobileLogOpen ? '（点击收起）' : '' }}</span>
        <NButton v-if="mobileLogOpen" size="tiny" quaternary :loading="mobileLogLoading"
                 class="log-refresh" @click.stop="loadMobileLog">
          <i class="ms sm">refresh</i> 刷新
        </NButton>
      </summary>
      <template v-if="mobileLogOpen">
        <div class="logbox">
          <div v-if="!mobileLogEntries.length" class="logempty">{{ t('（暂无日志）') }}</div>
          <template v-for="(e, i) in mobileLogEntries" :key="i">
            <details v-if="e.thinking" class="think">
              <summary>{{ e.head }}</summary>
              <pre>{{ e.body || '（无正文）' }}</pre>
            </details>
            <div v-else class="logentry">{{ e.head }}<template v-if="e.body">{{ '\n' + e.body }}</template></div>
          </template>
        </div>
      </template>
    </details>
  </div>
</template>

<style scoped>
.dash { padding: 0 20px; }
.dash-empty {
  border: 1px dashed var(--line);
  border-radius: 8px;
  padding: 32px 20px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  background: var(--card);
  text-align: center;
  margin-bottom: 16px;
}
.stats { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(400px, 1fr)); gap: 14px; }
.phead { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.git-refresh { padding: 0 6px; font-size: 13px; }
/* 路径与任务统计同行两端对齐：路径过长自行折行，统计钉在右上角不随路径折行漂移 */
.pathline { display: flex; gap: 10px; align-items: baseline; justify-content: space-between; margin-bottom: 8px; }
.path { font-size: 12px; color: var(--dim); word-break: break-all; min-width: 0; }
.counts { flex: none; font-size: 12px; color: var(--dim); }
.quickadd { display: flex; gap: 6px; margin-bottom: 10px; }
.quickadd .qpri { width: 88px; flex: none; }
.tasks { display: flex; flex-direction: column; gap: 8px; }
.batchbar { display: flex; gap: 10px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }
.batchbar-spacer { flex: 1; }
.batchbar .hint { margin-bottom: 0; }
.tcheck { display: flex; align-items: center; }
.task { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; }
.task.err { border-color: var(--n-error-color); }
/* 拖拽排序：落点指示线 + 被拖行半透明 */
.task.drop-before { box-shadow: 0 -2px 0 0 var(--acc, #5b8cff); }
.task.drop-after { box-shadow: 0 2px 0 0 var(--acc, #5b8cff); }
.task.dragging { opacity: .45; }
.t-main { display: flex; gap: 8px; align-items: center; }
.t-main.clickable { cursor: pointer; }
.task .title { flex: 1; font-weight: 600; font-size: 13.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.t-meta { font-size: 12px; color: var(--dim); margin-top: 3px; word-break: break-all; }
.tdesc { margin-top: 4px; font-size: 12px; }
.tdesc summary { cursor: pointer; color: var(--dim); }
.tdesc pre { margin: 4px 0 0; white-space: pre-wrap; word-break: break-word; font: inherit; opacity: 0.85; }
.ops-row { display: flex; gap: 6px; margin-top: 6px; }
.card-footer-ops { display: flex; gap: 6px 8px; margin-top: 6px; flex-wrap: wrap; padding-right: 14px; align-items: center; }
.card-footer-ops .del-btn { margin-right: 4px; }
.empty { color: var(--faint); font-size: 13px; text-align: center; padding: 18px 0; }
.arch { margin-top: 10px; font-size: 13px; }
.arch summary { cursor: pointer; color: var(--dim); }
.logsec { margin: 16px 0; font-size: 13px; }
.logsec summary { cursor: pointer; color: var(--dim); display: flex; align-items: center; gap: 6px; }
.logsec summary .log-refresh { margin-left: auto; min-width: 78px; }
/* loading 转圈出现/消失时按钮宽度恒定，标题行不跳动 */
.logbox {
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 8px; padding: 10px 12px; max-height: 380px; overflow: auto;
  font: 12px/1.6 Consolas, "Cascadia Mono", monospace; white-space: pre-wrap; word-break: break-all;
}
.logempty { color: var(--faint); }
.logentry { margin-bottom: 2px; }
.think { margin: 4px 0; }
.think summary { cursor: pointer; color: #a78bfa; user-select: none; }
.think pre {
  margin: 4px 0 0; font: inherit; white-space: pre-wrap; word-break: break-word;
  opacity: 0.85; border-left: 2px solid var(--line2); padding-left: 8px;
}
/* ---- 移动端适配（≤768px）：单列卡片、任务标题可折行、按钮换行不溢出 ---- */
@media (max-width: 768px) {
  .dash { padding: 0 12px; }
  .grid { grid-template-columns: 1fr; }
  .stats { gap: 6px; margin-bottom: 10px; }
  .stats .n-tag { font-size: 12px; }
  .t-main { flex-wrap: wrap; }
  .task .title { white-space: normal; }
  .ops-row { flex-wrap: wrap; }
  .card-footer-ops { flex-wrap: wrap; }
  .quickadd { flex-wrap: wrap; }
  .quickadd .qpri { width: 76px; }
  .pathline { flex-direction: column; align-items: flex-start; gap: 2px; }
}
</style>
