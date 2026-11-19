<script setup lang="ts">
// 项目页：只显示全部受管项目；页码分页懒加载（每页渲染少数卡片，翻页才渲染）。
// 卡片交互（启停/任务列表/拖拽/批量操作/归档查看）与看板页同款，此处不重复
// 看板顶部的调度状态条与运行日志。
import { NButton, NCard, NCheckbox, NEmpty, NInput, NPagination, NSelect, NSwitch, NTag } from 'naive-ui'
import { computed, reactive, ref, shallowRef } from 'vue'
import { api, type ProjectT, type TaskT } from '../api'
import { openCommitDlg, openGuide, openProjectEdit, openTaskNew, openTaskView, refresh, store, ui } from '../store'
import { PRI_COLOR, ST_NAME, ST_TYPE } from '../theme'
import { tNow as it, tFmt } from '../lang'

// ---- 项目分页懒加载：翻到哪页渲染哪页（数据全量在 store，渲染按页切片） ----
const PAGE_SIZE = 6
const page = shallowRef(1)
const projects = computed<ProjectT[]>(() => store.state?.projects ?? [])
const total = computed(() => projects.value.length)
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
// 数据变少（移除项目）时收敛到合法页码，避免停在空页
const safePage = computed(() => Math.min(page.value, pageCount.value))
const pagedProjects = computed(() =>
  projects.value.slice((safePage.value - 1) * PAGE_SIZE, safePage.value * PAGE_SIZE))
function onPageChange(pg: number): void {
  page.value = pg
}

function roleBadgeText(role: string): string {
  const map: Record<string, string> = {
    pm: 'PM', backend: it('后端'), frontend: it('前端'), qa: it('测试'),
  }
  return map[role] || role
}

// ---- 任务排序与卡片内分页（与看板页同规则：状态分组 → 优先级 → 拖拽 order） ----
const STATUS_ORD: Record<string, number> = { open: 0, in_progress: 1, blocked: 2, cancelled: 3 }
const sorted = (p: ProjectT) =>
  [...p.tasks].sort((a, b) => {
    const st = (STATUS_ORD[a.status] ?? 9) - (STATUS_ORD[b.status] ?? 9)
    if (st) return st
    if (a.rank !== b.rank) return a.rank - b.rank
    const oa = a.order, ob = b.order
    if (oa != null && ob != null && oa !== ob) return oa - ob
    if (oa != null && ob == null) return -1
    if (oa == null && ob != null) return 1
    return a.id.localeCompare(b.id)
  })

function stats(p: ProjectT): string {
  const c: Record<string, number> = {}
  for (const t of p.tasks) c[t.status] = (c[t.status] || 0) + 1
  const parts: string[] = []
  if (c.open) parts.push(tFmt('{p0} 待办', { p0: c.open }))
  if (c.in_progress) parts.push(tFmt('{p0} 执行中', { p0: c.in_progress }))
  if (c.blocked) parts.push(tFmt('{p0} 受阻', { p0: c.blocked }))
  return parts.join(' · ') || it('无待办')
}

// 任务是否有挂起的人工审批（行内「⏸ 待审批」标签）
function waitingApproval(project: string, taskId: string): boolean {
  return (store.state?.approvals ?? []).some(
    a => a.project === project && a.task_id === taskId)
}

// ---- 拖拽排序：行内 HTML5 DnD，与看板页同语义（同状态+优先级分组内生效） ----
const dragId = shallowRef<string | null>(null)
const dropAt = shallowRef<{ id: string; before: boolean } | null>(null)

function onDragStart(e: DragEvent, t: TaskT): void {
  dragId.value = t.id
  ui.dragTask = t.id
  e.dataTransfer?.setData('text/plain', t.id)
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
  if (moved.status !== anchorTask.status || moved.rank !== anchorTask.rank) {
    window.$message?.(it('任务顺序仅在同一优先级分组内生效（跨优先级请先改优先级）'), 'warning')
    return
  }
  const ids = all.map(x => x.id).filter(id => id !== src)
  const anchor = ids.indexOf(at.id)
  if (anchor < 0) return
  ids.splice(at.before ? anchor : anchor + 1, 0, src)
  try {
    await api.taskReorder(p.name, ids)
  } catch (e: any) { window.$message?.(e.message, 'error') }
  void import('../store').then(m => m.refresh(true))
}

// ---- 卡片内任务分页 ----
const TASK_PAGE_SIZE = 10
const taskPage = reactive<Record<string, number>>({})
function taskTotal(p: ProjectT): number {
  return sorted(p).length
}
function totalPages(p: ProjectT): number {
  return Math.max(1, Math.ceil(taskTotal(p) / TASK_PAGE_SIZE))
}
function curPage(p: ProjectT): number {
  return Math.min(taskPage[p.name] || 1, totalPages(p))
}
function pageTasks(p: ProjectT): TaskT[] {
  const all = sorted(p)
  const start = (curPage(p) - 1) * TASK_PAGE_SIZE
  return all.slice(start, start + TASK_PAGE_SIZE)
}
function onTaskPageChange(p: ProjectT, pg: number): void {
  taskPage[p.name] = pg
}

// ---- 任务列表展开/收起：默认收起节省卡片高度；空列表同样可展开查看提示 ----
const taskListOpen = reactive<Record<string, boolean>>({})
function isTaskListOpen(p: ProjectT): boolean {
  return !!taskListOpen[p.name]
}
function toggleTaskList(p: ProjectT): void {
  taskListOpen[p.name] = !taskListOpen[p.name]
}

// ---- 项目启停：绑定类字段原样回传（后端把缺失键当「清除」处理） ----
async function toggleProject(p: ProjectT, enabled: boolean): Promise<void> {
  try {
    await api.projectSave({
      name: p.name, path: p.path, enabled,
      tasks_dir: p.tasks_dir, guide_file: p.guide_file,
      max_tasks_per_cycle: p.max_tasks_per_cycle,
      max_agent_turns: p.max_agent_turns,
      allowed_branch: p.allowed_branch ?? undefined,
      allowed_commands: p.allowed_commands ?? undefined,
      llm: p.llm ?? null,
      review_enabled: p.review_enabled ?? null,
      review_llm: p.review_llm ?? null,
      review_max_rounds: p.review_max_rounds ?? null,
      repo: p.repo ?? null,
      tools: p.tools ?? null,
      permission_mode: p.permission_mode ?? null,
      debug_allowed_hosts: p.debug_allowed_hosts ?? null,
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

// ---- 熔断恢复（V1-R4）：人工解除项目暂停 ----
async function resumePaused(name: string): Promise<void> {
  try {
    await api.resumeProject(name)
    window.$message?.(tFmt('项目 {p0} 已恢复', { p0: name }), 'success')
  } catch (e: any) {
    window.$message?.(e.message, 'error')
  }
  void import('../store').then(m => m.refresh(true))
}

function clickTask(project: string, t: TaskT): void {
  if (t.status === 'open') {
    ui.taskDlg = { project, id: t.id }
  } else {
    openTaskView(project, t.id)
  }
}

async function delTask(p: ProjectT, t: TaskT): Promise<void> {
  window.$dialog?.warning({
    title: it('删除任务'),
    content: tFmt('确定删除任务 {p0}？', { p0: t.id }),
    positiveText: it('删除'),
    negativeText: it('取消'),
    async onPositiveClick() {
      try { await api.taskDelete(p.name, t.id); window.$message?.('已删除') }
      catch (e: any) { window.$message?.(e.message, 'error') }
      void import('../store').then(m => m.refresh(true))
    },
  })
}

function stopTask(p: ProjectT, t: TaskT): void {
  window.$dialog?.warning({
    title: it('强制停止任务'),
    content: `确定立即停止正在执行的任务 ${t.id}？\n\n`
      + '当前工具调用（含正在跑的命令）会被中止，worktree 里未提交的改动将被丢弃，'
      + it('任务标记为「已取消」——之后可点「重新入队」再跑。'),
    positiveText: it('强制停止'),
    negativeText: it('取消'),
    async onPositiveClick() {
      try {
        const r = await api.taskStop(p.name, t.id)
        window.$message?.(r.message || it('已发送停止信号'), 'success')
      } catch (e: any) { window.$message?.(e.message, 'error') }
      void import('../store').then(m => m.refresh(true))
    },
  })
}

// 只从系统移除项目（配置移除，磁盘文件与任务全部保留）
async function delProject(p: ProjectT): Promise<void> {
  window.$dialog?.warning({
    title: it('移除项目'),
    content: tFmt('确定从系统移除项目 {p0}？（仅移出配置，目录与任务文件不会删除）', { p0: p.name }),
    positiveText: it('移除'),
    negativeText: it('取消'),
    async onPositiveClick() {
      try { await api.projectDelete(p.name); window.$message?.('已移除（文件已保留）') }
      catch (e: any) { window.$message?.(e.message, 'error') }
      void import('../store').then(m => m.refresh(true))
    },
  })
}

const syncing = shallowRef('')
async function syncIssues(p: { name: string }): Promise<void> {
  syncing.value = p.name
  try {
    const r = await api.issuesSync(p.name)
    window.$message?.(r.message || it('同步完成'), 'success')
  } catch (e: any) { window.$message?.(e.message, 'error') }
  finally { syncing.value = '' }
  void import('../store').then(m => m.refresh(true))
}

const gitRefreshing = shallowRef('')
async function refreshGit(p: { name: string }): Promise<void> {
  gitRefreshing.value = p.name
  try {
    await refresh(true)
    const cur = store.state?.projects.find(x => x.name === p.name)
    window.$message?.(cur?.is_git_repo ? it('已识别为 git 仓库') : it('仍不是 git 仓库'), 'success')
  } catch (e: any) {
    window.$message?.(e.message, 'error')
  } finally { gitRefreshing.value = '' }
}

// ---- 批量删除任务（选择跨页保留） ----
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
    title: it('批量删除任务'),
    content: tFmt('确定删除选中的 {p0} 个任务？任务文件将被删除，不可恢复。', { p0: ids.length }),
    positiveText: it('删除'),
    negativeText: it('取消'),
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

const clearing = shallowRef('')
async function clearTasks(p: ProjectT): Promise<void> {
  window.$dialog?.warning({
    title: it('清空任务'),
    content: `确定清空项目 ${p.name} 的全部 ${p.tasks.length} 个任务？`
      + it('已归档（done）的任务不受影响；任务文件将被删除，不可恢复。'),
    positiveText: it('清空'),
    negativeText: it('取消'),
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

// ---- 快速添加任务 ----
const quick = reactive<Record<string, { title: string; pri: string }>>({})
const q = (name: string) => quick[name] ?? (quick[name] = reactive({ title: '', pri: 'P2' }))
const priOptions = ['P0', 'P1', 'P2', 'P3', 'P4'].map(v => ({ label: v, value: v }))
const adding = shallowRef(false)

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
    taskPage[p.name] = 1
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
  <div class="projpage">
    <div class="head" v-if="store.state">
      <span class="hint">{{ tFmt('共 {p0} 个受管项目 · 每页 {p1} 个，翻页按需渲染', { p0: total, p1: PAGE_SIZE }) }}</span>
    </div>
    <div v-if="store.state && !total" class="empty">
      <NEmpty :description="it('还没有项目，点右上「＋ 新建项目」添加')" />
    </div>

    <div class="grid">
      <NCard v-for="p in pagedProjects" :key="p.name" class="proj"
             :title="p.name" size="small">
        <template #header-extra>
          <div class="phead">
            <NSwitch size="small" :value="p.enabled" @update:value="(v: boolean) => toggleProject(p, v)" />
            <NTag size="small" :type="p.is_git_repo ? 'default' : 'warning'" :bordered="false">
              {{ p.is_git_repo ? 'git' : it('非 git') }}
            </NTag>
            <NTag v-if="p.nested_gitlinks?.length" size="small" type="info" :bordered="false"
                  ":title="tFmt('含嵌套 Git 仓库：{p0}；已回退主工作区串行模式，改动需在子仓库内提交', { p0: p.nested_gitlinks.join('、') })">
              {{ it('嵌套仓库') }} ({{ p.nested_gitlinks.length }})
            </NTag>
            <NTag v-if="p.budget_paused" size="small" type="warning" :bordered="false"
                  :title="it('项目今日 token 预算已用尽，暂停领取新任务；调高预算或等次日恢复')">
              {{ it('预算暂停') }}
            </NTag>
            <NTag v-if="p.health?.paused" size="small" type="error" :bordered="false"
                  :title="p.health?.pause_reason || it('项目已熔断暂停')">
              {{ it('已熔断') }}
            </NTag>
            <NButton v-if="p.health?.paused" size="tiny" type="warning" secondary
                     @click="resumePaused(p.name)">{{ it('恢复') }}</NButton>
            <NButton size="tiny" quaternary class="git-refresh" :loading="gitRefreshing === p.name"
                     :title="it('刷新仓库信息（在文件夹里手动 init git 后点这里立即生效）')"
                     @click="refreshGit(p)"><i class="ms sm">refresh</i></NButton>
            <NTag v-if="!p.guide_exists" size="small" type="error" :bordered="false">{{ it('缺指引') }}</NTag>
            <NTag v-if="p.team_enabled" size="small" type="info" :bordered="false"
                  :title="it('已启用项目小组：') + (p.team_members_effective?.map(m => m.name).join('、') || '')">
              <i class="ms sm">group</i> {{ it('小组') }} ({{ p.team_members_effective?.length || 0 }})
            </NTag>
            <NButton v-if="p.pending_git_changes && p.pending_git_changes.count > 0"
                     size="tiny" type="warning" secondary
                     :title="it('主工作区有已通过测试但未提交的变更，点此人工把关确认提交')"
                     @click="openCommitDlg(p.name)">
              {{ it('待人工提交') }} ({{ p.pending_git_changes.count }})
            </NButton>
            <NTag v-if="p.review?.warning" size="small" type="error" :bordered="false"
                  :title="p.review.warning">{{ it('审查模型冲突') }}</NTag>
            <NTag v-else-if="p.review?.model_configured && p.review?.enabled_effective"
                  size="small" type="success" :bordered="false"
                  :title="tFmt('审查模型 {p0} · 返工上限 {p1} 次', { p0: p.review.effective_model, p1: p.review.effective_max_rounds })">
              {{ it('审查开启') }}</NTag>
            <NTag v-else-if="p.review_enabled === false || (p.review?.enabled_effective === false && p.review?.model_configured)"
                  size="small" :bordered="false"
                  :title="it('本项目已关闭审查门（跟随关闭优先于全局开关）')">{{ it('审查关闭') }}</NTag>
            <NTag v-if="p.issue_auto_pull" size="small" :bordered="false"
                  :type="p.issue_auto_pull.last_error ? 'error' : 'success'"
                  :title="p.issue_auto_pull.last_error || undefined">
              {{ it('自动拉取') }} {{ Math.round(p.issue_auto_pull.interval / 60) }}min
            </NTag>
          </div>
        </template>
        <template #footer>
          <div class="card-footer-ops">
            <NButton size="tiny" @click="openFolder(p)">{{ it('打开文件夹') }}</NButton>
            <NButton size="tiny" @click="openTaskNew(p.name)">{{ it('新建任务') }}</NButton>
            <NButton size="tiny" @click="openGuide(p.name)">AGENT.md</NButton>
            <NButton size="tiny" :loading="syncing === p.name" @click="syncIssues(p)">{{ it('拉取 Issues') }}</NButton>
            <NButton size="tiny" @click="openProjectEdit(p.name)">{{ it('设置') }}</NButton>
            <NButton size="tiny" quaternary type="error" class="del-btn" @click="delProject(p)">{{ it('移除') }}</NButton>
          </div>
        </template>

        <div class="pathline">
          <span class="path">{{ p.path }}</span>
          <span class="counts">{{ stats(p) }}</span>
        </div>

        <div class="quickadd">
          <NInput v-model:value="q(p.name).title" size="small" :placeholder="it('新任务标题，回车快速添加')"
                  @keydown.enter="quickAdd(p)" />
          <NSelect v-model:value="q(p.name).pri" size="small" class="qpri" :options="priOptions" />
          <NButton size="small" type="primary" :loading="adding" @click="quickAdd(p)">{{ it('添加') }}</NButton>
        </div>

        <button type="button" class="tasklist-toggle" :aria-expanded="isTaskListOpen(p)"
                :title="isTaskListOpen(p) ? it('收起任务列表') : it('展开任务列表')"
                @click="toggleTaskList(p)">
          <i class="ms sm tasklist-chev" :class="{ open: isTaskListOpen(p) }">{{ isTaskListOpen(p) ? 'expand_less' : 'expand_more' }}</i>
          <span class="tasklist-title">{{ it('任务列表') }}</span>
          <span class="tasklist-count">
            {{ it('待办') }} {{ p.tasks.length }}<template v-if="p.parse_errors.length"> · {{ it('异常') }} {{ p.parse_errors.length }}</template>
          </span>
          <span class="tasklist-hint">{{ isTaskListOpen(p) ? it('收起') : it('展开') }}</span>
        </button>

        <template v-if="isTaskListOpen(p)">
        <div v-if="p.tasks.length || p.parse_errors.length" class="batchbar">
          <NCheckbox size="small" :checked="pageAllChecked(p)" :indeterminate="pageSomeChecked(p)"
                     @update:checked="(v: boolean) => checkAllPage(p, v)">{{ it('全选本页') }}</NCheckbox>
          <template v-if="checkedIds(p.name).length">
            <span class="hint">{{ it('已选') }} {{ checkedIds(p.name).length }} {{ it('个') }}</span>
            <NButton size="tiny" type="error" :loading="batchDeleting === p.name"
                     @click="batchDel(p)">{{ it('批量删除') }}</NButton>
            <NButton size="tiny" quaternary @click="clearChecked(p.name)">{{ it('取消选择') }}</NButton>
          </template>
          <span class="batchbar-spacer" />
          <NButton size="tiny" type="warning" :loading="clearing === p.name"
                   @click="clearTasks(p)">{{ it('清空任务') }}（{{ p.tasks.length }}）</NButton>
        </div>

        <div class="tasks">
          <div v-for="e in p.parse_errors" :key="'e' + e.id" class="task err">
            <div class="t-main">
              <NTag size="small" type="error">{{ it('格式') }}</NTag>
              <span class="title">{{ e.id }}</span><NTag size="small" type="error">{{ it('解析失败') }}</NTag>
            </div>
            <div class="t-meta">{{ e.error }}</div>
          </div>

          <div v-for="tk in pageTasks(p)" :key="tk.id" class="task"
               :class="{ 'drop-before': dropAt?.id === tk.id && dropAt.before,
                         'drop-after': dropAt?.id === tk.id && !dropAt.before,
                         dragging: dragId === tk.id }"
               draggable="true" :title="it('拖拽调整顺序（同一优先级内生效，同时决定 Agent 领取顺序）')"
               @dragstart="onDragStart($event, tk)" @dragend="onDragEnd()"
               @dragover.prevent="onDragOver($event, tk)" @drop.prevent="onDrop(p, tk)">
            <div class="t-main clickable"
                 :title="tk.status === 'open' ? it('点击编辑任务内容') : it('点击查看任务内容（只读）')"
                 @click="clickTask(p.name, tk)">
              <span class="tcheck" @click.stop>
                <NCheckbox size="small" :checked="isTaskChecked(p.name, tk.id)"
                           @update:checked="(v: boolean) => toggleCheck(p.name, tk.id, v)" />
              </span>
              <NTag size="small" :color="{ color: PRI_COLOR[tk.priority] || '#64748f', textColor: '#fff' }"
                    :bordered="false">{{ tk.priority }}</NTag>
              <span class="title" :title="tk.title">{{ tk.title }}</span>
              <NTag size="small" :type="ST_TYPE[tk.status] || 'default'" :bordered="false">
                {{ ST_NAME[tk.status] || tk.status }}
              </NTag>
              <NTag v-if="tk.assignee" size="small" type="info" :bordered="false">
                {{ roleBadgeText(tk.assignee) }}
              </NTag>
              <NTag v-if="tk.dep_state === 'waiting'" size="small" :bordered="false"
                    :title="it('等待前置依赖任务完成')">
                <i class="ms sm">hourglass_top</i> {{ it('等待前置') }} ({{ tk.dep_done_count }}/{{ tk.dep_total_count }})
              </NTag>
              <NTag v-else-if="tk.dep_state === 'upstream_blocked'" size="small" type="error" :bordered="false"
                    :title="it('前置依赖任务存在受阻/取消')">
                <i class="ms sm">warning</i> {{ it('前置受阻') }}
              </NTag>
              <NTag v-if="(tk.qa_round || 0) > 0" size="small" type="warning" :bordered="false">
                <i class="ms sm">autorenew</i> {{ it('回归') }} #{{ tk.qa_round }}
              </NTag>
              <NTag v-if="tk.review_pending" size="small" type="info" :bordered="false"
                    :title="it('任务已完成，独立审查进行中或等待补审；审查通过后合并归档')">
                <i class="ms sm">rate_review</i> {{ it('待审查') }}
              </NTag>
              <NTag v-else-if="(tk.review_round || 0) > 0 && tk.status !== 'done' && tk.status !== 'cancelled'"
                    size="small" type="warning" :bordered="false"
                    :title="tFmt('已被审查退回 {p0} 次（返工轮）', { p0: tk.review_round })">
                <i class="ms sm">rate_review</i> {{ it('返工') }} ×{{ tk.review_round }}
              </NTag>
              <NTag v-if="waitingApproval(p.name, tk.id)" size="small" type="warning"
                    :bordered="false" :title="it('Agent 有操作挂起等待人工审批，点顶栏「待审批」处理')">
                <i class="ms sm">pause_circle</i> {{ it('待审批') }}
              </NTag>
            </div>
            <div class="t-meta">
              {{ tk.id }}<template v-if="tk.labels.length"> · {{ tk.labels.join(', ') }}</template><template v-if="tk.assignee"> · {{ it('角色') }} {{ tk.assignee }}</template><template v-if="tk.depends_on && tk.depends_on.length"> · {{ it('前置') }} {{ tk.depends_on.join(', ') }}</template>
            </div>
            <details v-if="tk.description" class="tdesc">
              <summary>{{ it('描述') }}</summary>
              <pre>{{ tk.description }}</pre>
            </details>
            <div class="ops-row">
              <NButton v-if="tk.status !== 'open' && tk.status !== 'cancelled'" size="tiny"
                       @click="setStatus(p, tk, 'open')">{{ it('重新入队') }}</NButton>
              <NButton v-if="tk.status === 'open' || tk.status === 'in_progress'" size="tiny"
                       @click="setStatus(p, tk, 'done')">{{ it('标记完成') }}</NButton>
              <NButton v-if="tk.status === 'in_progress'" size="tiny" type="warning" secondary
                       :title="it('立即中止正在执行的 Agent 循环（丢弃 worktree 改动，可重新入队）')"
                       @click="stopTask(p, tk)">{{ it('强制停止') }}</NButton>
              <NButton size="tiny" quaternary type="error" @click="delTask(p, tk)">{{ it('删除') }}</NButton>
            </div>
          </div>

          <div v-if="!p.tasks.length && !p.parse_errors.length" class="empty">
            {{ it('暂无任务 · 点项目卡下方「新建任务」添加') }}
          </div>
          <NPagination v-if="taskTotal(p) > TASK_PAGE_SIZE" size="small"
                       :page="curPage(p)" :page-count="totalPages(p)"
                       @update:page="(pg: number) => onTaskPageChange(p, pg)" />
        </div>
        </template>

        <details v-if="hasArchived(p)" class="arch"
                 @toggle="onArchToggle($event, p)">
          <summary>
            <span class="arch-title">{{ it('已归档') }}</span>
            <span class="arch-count">{{ archivedCount(p) }} {{ it('个任务') }}</span>
            <span class="arch-hint" />
          </summary>
          <div v-if="!p.archived.length" class="arch-loading">{{ it('加载归档列表…') }}</div>
          <div v-for="tk in [...archivedList(p)].reverse()" :key="tk.id" class="task">
            <div class="t-main clickable" :title="it('点击查看任务详情（只读）')" @click="openTaskView(p.name, tk.id)">
              <NTag size="small" :color="{ color: PRI_COLOR[tk.priority] || '#64748f', textColor: '#fff' }" :bordered="false">{{ tk.priority }}</NTag>
              <span class="title">{{ tk.title }}</span>
              <NTag size="small" type="success" :bordered="false">{{ it('已完成') }}</NTag>
            </div>
            <div class="t-meta">{{ tk.id }}</div>
          </div>
        </details>
      </NCard>
    </div>

    <div class="pager" v-if="pageCount > 1">
      <NPagination :page="safePage" :page-count="pageCount"
                   @update:page="onPageChange" />
    </div>
  </div>
</template>

<style scoped>
.projpage { padding: 0 20px; overflow-x: clip; }
.head { margin-bottom: 12px; }
.hint { font-size: 12px; color: var(--faint); }
.empty { color: var(--faint); font-size: 13px; text-align: center; padding: 40px 0; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(400px, 100%), 1fr)); gap: 14px; }
.phead { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.git-refresh { padding: 0 6px; font-size: 13px; }
/* 路径与任务统计同行两端对齐：路径过长自行折行，统计钉在右上角不随路径折行漂移 */
.pathline { display: flex; gap: 10px; align-items: baseline; justify-content: space-between; margin-bottom: 8px; }
.path { font-size: 12px; color: var(--dim); word-break: break-all; min-width: 0; }
.counts { flex: none; font-size: 12px; color: var(--dim); }
.quickadd { display: flex; gap: 6px; margin-bottom: 10px; }
.quickadd .qpri { width: 88px; flex: none; }
/* 任务列表折叠条：整行可点，空列表也显示；默认收起以节约卡片高度 */
.tasklist-toggle { width: 100%; display: flex; align-items: center; gap: 8px;
  margin: 0 0 8px; padding: 6px 9px; border: 1px solid var(--line);
  border-radius: 8px; background: rgba(128, 140, 180, 0.07);
  color: var(--text); font: inherit; font-size: 12.5px; text-align: left;
  cursor: pointer; }
.tasklist-toggle:hover { border-color: var(--acc, #5b8cff); }
.tasklist-chev { color: var(--faint); transition: color .15s ease; }
.tasklist-chev.open { color: var(--acc, #5b8cff); }
.tasklist-title { font-weight: 600; white-space: nowrap; }
.tasklist-count { color: var(--faint); font-size: 11.5px; min-width: 0; }
.tasklist-hint { margin-left: auto; color: var(--faint); font-size: 11.5px; white-space: nowrap; }
.tasks { display: flex; flex-direction: column; gap: 8px; }
.batchbar { display: flex; gap: 10px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }
.batchbar-spacer { flex: 1; }
.batchbar .hint { margin-bottom: 0; }
.tcheck { display: flex; align-items: center; }
.task { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; }
.task.err { border-color: var(--n-error-color); }
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
.arch { margin-top: 8px; font-size: 13px; }
/* 归档折叠条：与「任务列表」折叠条同款盒子/字号/箭头（expand_more↔expand_less） */
.arch summary { display: flex; align-items: center; gap: 8px; cursor: pointer;
  padding: 6px 9px; border: 1px solid var(--line); border-radius: 8px;
  background: rgba(128, 140, 180, 0.07); color: var(--text);
  font-size: 12.5px; }
.arch summary::before { content: 'expand_more'; font-family: 'Material Icons Round';
  font-size: 16px; line-height: 1; margin-right: 0; color: var(--faint);
  vertical-align: middle; -webkit-font-feature-settings: 'liga';
  font-feature-settings: 'liga'; transition: transform .15s ease; }
.arch[open] > summary::before { transform: rotate(180deg); color: var(--acc, #5b8cff); }
.arch summary:hover { border-color: var(--acc, #5b8cff); }
.arch-title { font-weight: 600; white-space: nowrap; }
.arch-count { color: var(--faint); font-size: 11.5px; }
.arch-hint { margin-left: auto; color: var(--faint); font-size: 11.5px; white-space: nowrap; }
.arch-hint::after { content: '展开'; }
.arch[open] > summary .arch-hint::after { content: '收起'; }
.arch > .task { margin-top: 8px; }
.pager { display: flex; justify-content: center; margin-top: 18px; }
</style>
