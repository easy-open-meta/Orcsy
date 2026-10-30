<script setup lang="ts">
import { NButton, NCard, NInput, NSelect, NSwitch, NTag } from 'naive-ui'
import { computed, reactive, ref } from 'vue'
import { openGuide, openProjectEdit, openTaskNew, openTaskView, store, ui } from '../store'
import { PRI_COLOR, ST_NAME, ST_TYPE } from '../theme'
import { parseLogEntries, relTime } from '../utils'
import type { ProjectT, TaskT } from '../api'
import { api } from '../api'

// 运行日志按从新到旧展示；💭 思考条目在渲染层折叠成可展开块
const logEntries = computed(() => parseLogEntries(store.state?.log_tail || ''))

function stats(p: ProjectT): string {
  const c: Record<string, number> = {}
  for (const t of p.tasks) c[t.status] = (c[t.status] || 0) + 1
  const parts: string[] = []
  if (c.open) parts.push(`${c.open} 待办`)
  if (c.in_progress) parts.push(`${c.in_progress} 执行中`)
  if (c.blocked) parts.push(`${c.blocked} 受阻`)
  return parts.join(' · ') || '无待办'
}

const sorted = (p: ProjectT) =>
  [...p.tasks].sort((a, b) =>
    (({ open: 0, in_progress: 1, blocked: 2, cancelled: 3 } as any)[a.status] ?? 9)
    - (({ open: 0, in_progress: 1, blocked: 2, cancelled: 3 } as any)[b.status] ?? 9)
    || a.rank - b.rank || a.id.localeCompare(b.id))

async function toggleProject(p: ProjectT, enabled: boolean): Promise<void> {
  try {
    await api.projectSave({
      name: p.name, path: p.path, enabled,
      tasks_dir: p.tasks_dir, guide_file: p.guide_file,
      max_tasks_per_cycle: p.max_tasks_per_cycle,
      max_agent_turns: p.max_agent_turns,
      allowed_branch: p.allowed_branch ?? undefined,
      allowed_commands: p.allowed_commands ?? undefined,
      original_name: p.name,
    })
    window.$message?.(enabled ? `已启用 ${p.name}` : `已停用 ${p.name}`)
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
    title: '删除任务',
    content: `确定删除任务 ${t.id}？`,
    positiveText: '删除',
    negativeText: '取消',
    async onPositiveClick() {
      try { await api.taskDelete(p.name, t.id); window.$message?.('已删除') }
      catch (e: any) { window.$message?.(e.message, 'error') }
      void import('../store').then(m => m.refresh(true))
    },
  })
}

// 只从系统移除项目（配置移除，磁盘文件与任务全部保留）；彻底删除请手动删目录
async function delProject(p: ProjectT): Promise<void> {
  window.$dialog?.warning({
    title: '移除项目',
    content: `确定从系统移除项目 ${p.name}？（仅移出配置，目录与任务文件不会删除）`,
    positiveText: '移除',
    negativeText: '取消',
    async onPositiveClick() {
      try { await api.projectDelete(p.name); window.$message?.('已移除（文件已保留）') }
      catch (e: any) { window.$message?.(e.message, 'error') }
      void import('../store').then(m => m.refresh(true))
    },
  })
}

// 用系统文件管理器打开项目根目录（后端调起，浏览器自身无此权限）
async function openFolder(p: ProjectT): Promise<void> {
  try {
    await api.projectOpenFolder(p.name)
    window.$message?.(`已打开 ${p.path}`)
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
    window.$message?.(`任务已创建：${r.id}`)
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
        {{ store.state.scheduler.running ? '扫描执行中…' : '调度空闲' }}
      </NTag>
      <NTag round size="large">已完成周期 {{ store.state.scheduler.cycles_done }}</NTag>
      <NTag round size="large">上次扫描 {{ relTime(store.state.scheduler.last_cycle_at) }}</NTag>
      <NTag round size="large">扫描间隔 {{ store.state.scheduler.interval_seconds }}s</NTag>
      <NTag v-for="(v, k) in store.state.scheduler.last_cycle_stats" :key="k" round size="large">
        {{ k }}：处理 {{ v }} 任务
      </NTag>
    </div>

    <div class="grid">
      <NCard v-for="p in store.state?.projects || []" :key="p.name" class="proj"
             :title="p.name" size="small">
        <template #header-extra>
          <div class="phead">
            <NSwitch size="small" :value="p.enabled" @update:value="(v: boolean) => toggleProject(p, v)" />
            <NTag size="small" :type="p.is_git_repo ? 'default' : 'warning'" :bordered="false">
              {{ p.is_git_repo ? 'git' : '非 git' }}
            </NTag>
            <NTag v-if="!p.guide_exists" size="small" type="error" :bordered="false">缺指引</NTag>
          </div>
        </template>
        <template #footer>
          <div class="ops-row">
            <NButton size="tiny" @click="openFolder(p)">打开文件夹</NButton>
            <NButton size="tiny" @click="openTaskNew(p.name)">新建任务</NButton>
            <NButton size="tiny" @click="openGuide(p.name)">AGENT.md</NButton>
            <NButton size="tiny" @click="openProjectEdit(p.name)">设置</NButton>
            <NButton size="tiny" quaternary type="error" @click="delProject(p)">移除</NButton>
          </div>
        </template>

        <div class="path">{{ p.path }} <span class="counts">{{ stats(p) }}</span></div>

        <div class="quickadd">
          <NInput v-model:value="q(p.name).title" size="small" placeholder="新任务标题，回车快速添加"
                  @keydown.enter="quickAdd(p)" />
          <NSelect v-model:value="q(p.name).pri" size="small" class="qpri" :options="priOptions" />
          <NButton size="small" type="primary" :loading="adding" @click="quickAdd(p)">添加</NButton>
        </div>

        <div class="tasks">
          <div v-for="e in p.parse_errors" :key="'e' + e.id" class="task err">
            <div class="t-main">
              <NTag size="small" type="error">格式</NTag>
              <span class="title">{{ e.id }}</span><NTag size="small" type="error">解析失败</NTag>
            </div>
            <div class="t-meta">{{ e.error }}</div>
          </div>

          <div v-for="t in sorted(p)" :key="t.id" class="task">
            <div class="t-main clickable"
                 :title="t.status === 'open' ? '点击编辑任务内容' : '点击查看任务内容（只读）'"
                 @click="clickTask(p.name, t)">
              <NTag size="small" :color="{ color: PRI_COLOR[t.priority] || '#64748f', textColor: '#fff' }"
                    :bordered="false">{{ t.priority }}</NTag>
              <span class="title" :title="t.title">{{ t.title }}</span>
              <NTag size="small" :type="ST_TYPE[t.status] || 'default'" :bordered="false">
                {{ ST_NAME[t.status] || t.status }}
              </NTag>
            </div>
            <div class="t-meta">
              {{ t.id }}<template v-if="t.labels.length"> · {{ t.labels.join(', ') }}</template><template v-if="t.assignee"> · 指派 {{ t.assignee }}</template>
            </div>
            <details v-if="t.description" class="tdesc">
              <summary>描述</summary>
              <pre>{{ t.description }}</pre>
            </details>
            <div class="ops-row">
              <NButton v-if="t.status !== 'open' && t.status !== 'cancelled'" size="tiny"
                       @click="setStatus(p, t, 'open')">重新入队</NButton>
              <NButton v-if="t.status === 'open' || t.status === 'in_progress'" size="tiny"
                       @click="setStatus(p, t, 'done')">标记完成</NButton>
              <NButton size="tiny" quaternary type="error" @click="delTask(p, t)">删除</NButton>
            </div>
          </div>

          <div v-if="!p.tasks.length && !p.parse_errors.length" class="empty">
            暂无任务 · 点项目卡下方「新建任务」添加
          </div>
        </div>

        <details v-if="p.archived.length" class="arch">
          <summary>已归档（{{ p.archived.length }}）· 点击查看详情</summary>
          <div v-for="t in [...p.archived].reverse()" :key="t.id" class="task">
            <div class="t-main clickable" title="点击查看任务详情（只读）" @click="openTaskView(p.name, t.id)">
              <NTag size="small" :color="{ color: PRI_COLOR[t.priority] || '#64748f', textColor: '#fff' }" :bordered="false">{{ t.priority }}</NTag>
              <span class="title">{{ t.title }}</span>
              <NTag size="small" type="success" :bordered="false">已完成</NTag>
            </div>
            <div class="t-meta">{{ t.id }}</div>
          </div>
        </details>
      </NCard>
    </div>

    <details v-if="store.state" class="logsec" open>
      <summary>运行日志（从新到旧，点击收起）</summary>
      <div class="logbox">
        <div v-if="!logEntries.length" class="logempty">（暂无日志）</div>
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
  </div>
</template>

<style scoped>
.dash { padding: 0 20px; }
.stats { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 14px; }
.phead { display: flex; gap: 6px; align-items: center; }
.path { font-size: 12px; color: var(--dim); margin-bottom: 8px; word-break: break-all; }
.counts { float: right; }
.quickadd { display: flex; gap: 6px; margin-bottom: 10px; }
.quickadd .qpri { width: 88px; flex: none; }
.tasks { display: flex; flex-direction: column; gap: 8px; }
.task { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; }
.task.err { border-color: var(--n-error-color); }
.t-main { display: flex; gap: 8px; align-items: center; }
.t-main.clickable { cursor: pointer; }
.task .title { flex: 1; font-weight: 600; font-size: 13.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.t-meta { font-size: 12px; color: var(--dim); margin-top: 3px; word-break: break-all; }
.tdesc { margin-top: 4px; font-size: 12px; }
.tdesc summary { cursor: pointer; color: var(--dim); }
.tdesc pre { margin: 4px 0 0; white-space: pre-wrap; word-break: break-word; font: inherit; opacity: 0.85; }
.ops-row { display: flex; gap: 6px; margin-top: 6px; }
.empty { color: var(--faint); font-size: 13px; text-align: center; padding: 18px 0; }
.arch { margin-top: 10px; font-size: 13px; }
.arch summary { cursor: pointer; color: var(--dim); }
.logsec { margin: 16px 0; font-size: 13px; }
.logsec summary { cursor: pointer; color: var(--dim); }
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
</style>
