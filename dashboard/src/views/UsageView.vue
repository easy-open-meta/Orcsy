<script setup lang="ts">
// 模型用量页：总体 + 各项目 + 近 14 天趋势 + 最近任务记录（任务/审查分类 Tab）。
// 数据来自 /api/state 的 usage 聚合（台账 logs/usage.jsonl，任务结束即记）。
import { NCard, NTag } from 'naive-ui'
import { computed, ref, shallowRef } from 'vue'
import { api } from '../api'
import { store } from '../store'
import { fmtTokens, type TokenUnit } from '../utils'
import { tNow as t, tFmt } from '../lang'

const usage = computed(() => store.state?.usage)

// tokens 单位口径：中文（万/亿）或英文（K/M/B）；选择写入 localStorage，
// 只影响用量页展示，精确值仍在卡片副标题与 tooltip 中可见。
const UNIT_KEY = 'orcsy-usage-unit'
function loadUnit(): TokenUnit {
  try {
    const saved = localStorage.getItem(UNIT_KEY)
    if (saved === 'en' || saved === 'zh') return saved
  } catch { /* 隐私模式忽略 */ }
  return 'zh'
}
const unit = ref<TokenUnit>(loadUnit())
function setUnit(v: TokenUnit): void {
  unit.value = v
  try { localStorage.setItem(UNIT_KEY, v) } catch { /* 隐私模式忽略 */ }
}

// fmtTokens 对 0 返回空串（规格展示语义）；用量页要显式看到 0
function fmt(v: number | undefined): string {
  return fmtTokens(v, unit.value) || String(v ?? 0)
}
function fmtFull(v: number | undefined): string {
  return (v ?? 0).toLocaleString('zh-CN')
}

// 标签随语言切换更新（t=tNow 一次性取值，须包 computed）
const OUT_TAG = computed<Record<string, { type: 'success' | 'warning' | 'default' | 'error' | 'info'; name: string }>>(() => ({
  done: { type: 'success', name: t('完成') },
  open: { type: 'info', name: t('待续') },
  blocked: { type: 'warning', name: t('受阻') },
  cancelled: { type: 'default', name: t('取消') },
}))

// 审查记录（kind=review）的结果列用另一套词汇：badge 颜色区分 + 🔍 前缀，
// 与任务结果（完成/受阻）同框但一眼可辨，不再混用同一列语义
const REVIEW_TAG = computed<Record<string, { type: 'success' | 'warning' | 'default' | 'error' | 'info'; name: string }>>(() => ({
  pass: { type: 'success', name: t('审查通过') },
  fail: { type: 'error', name: t('审查不通过') },
  error: { type: 'warning', name: t('审查失败') },
  skipped: { type: 'default', name: t('免审查') },
}))

// 台账旧记录缺 kind 字段视为任务记录
function isReview(r: { kind?: string }): boolean {
  return (r.kind || 'task') === 'review'
}
type TagType = 'success' | 'warning' | 'default' | 'error' | 'info'
function tagOf(r: { kind?: string; outcome: string }): { type: TagType; name: string } {
  if (isReview(r)) return REVIEW_TAG.value[r.outcome] || { type: 'default', name: r.outcome }
  return OUT_TAG.value[r.outcome] || { type: 'default', name: r.outcome }
}

// ---- 最近记录分类 Tab：全部 / 任务 / 审查 ----
type RecTab = 'all' | 'task' | 'review'
const recTab = ref<RecTab>('all')
const recentRows = computed(() => usage.value?.recent ?? [])
const taskCount = computed(() => recentRows.value.filter(r => !isReview(r)).length)
const reviewCount = computed(() => recentRows.value.filter(r => isReview(r)).length)
const filteredRecent = computed(() =>
  recTab.value === 'all' ? recentRows.value
    : recentRows.value.filter(r => isReview(r) === (recTab.value === 'review')))

const roleRunsRole = shallowRef('')
const roleRunsLoading = shallowRef(false)
const roleRunsError = shallowRef('')
const roleRuns = shallowRef<{ run_id: string; task_id: string; outcome: string; turns: number; tokens?: Record<string, number> }[]>([])

async function toggleRoleRuns(role: string): Promise<void> {
  if (roleRunsRole.value === role) {
    roleRunsRole.value = ''
    roleRuns.value = []
    return
  }
  roleRunsRole.value = role
  roleRunsLoading.value = true
  roleRuns.value = []
  try {
    const r = await api.runs({ limit: 200 })
    roleRuns.value = (r.runs || [])
      .filter(x => (x.role || '') === (role === 'solo' ? '' : role))
      .slice(0, 30)
  } catch (e: any) {
    roleRunsError.value = e.message
  } finally {
    roleRunsLoading.value = false
  }
}

function roleLabel(role: string): string {
  const map: Record<string, string> = {
    pm: t('项目经理 (PM)'),
    backend: t('后端开发工程师'),
    frontend: t('前端开发工程师'),
    qa: t('测试工程师 (QA)'),
    devops: t('运维/DevOps 工程师'),
    ui_critic: t('UI 视觉审查专家'),
    security: t('安全合规审计工程师'),
    review: t('任务审查 (Review Agent)'),
    solo: t('全栈单兵 / 传统模式'),
  }
  return map[role] || role
}

// ---- GitHub 贡献图风格热力图：列=周（周一起列）、行=星期、色深=当日 tokens ----
interface HeatCell { date: string; tasks: number; tokens: number; level: number; title: string }
const heat = computed(() => {
  const days = usage.value?.days ?? []
  const max = Math.max(1, ...days.map(d => d.prompt_tokens + d.completion_tokens))
  const rowOf = (date: string) => (new Date(date + 'T00:00:00').getDay() + 6) % 7  // 周一=0
  const cols: { cells: (HeatCell | null)[]; month: string }[] = []
  let col: { cells: (HeatCell | null)[]; month: string } | null = null
  for (const d of days) {
    const r = rowOf(d.date)
    if (r === 0 || !col) {   // 新的一周开新列；列首按星期补齐空位
      col = { cells: Array<HeatCell | null>(r).fill(null), month: d.date.slice(0, 7) }
      cols.push(col)
    }
    const tokens = d.prompt_tokens + d.completion_tokens
    const ratio = tokens / max
    const level = tokens <= 0 ? 0 : ratio <= 0.25 ? 1 : ratio <= 0.5 ? 2 : ratio <= 0.75 ? 3 : 4
    col.cells.push({
      date: d.date, tasks: d.tasks, tokens, level,
      title: tFmt('{p0} · {p1} 个任务 · {p2} tokens', { p0: d.date, p1: d.tasks, p2: fmt(tokens) }),
    })
    if (r === 6) col = null
  }
  if (col) while (col.cells.length < 7) col.cells.push(null)  // 末列补满一周
  // 月份标签：列首日期换月时显示（首列也显示）
  const months = cols.map((c, i) =>
    (i === 0 || c.month !== cols[i - 1].month) ? tFmt('{p0}月', { p0: Number(c.month.slice(5)) }) : '')
  return { cols, months }
})
</script>

<template>
  <div class="usage" v-if="store.state">
    <div class="ubar">
      <span class="uhint">{{ t('tokens 单位') }}</span>
      <div class="useg" role="group" :aria-label="t('tokens 单位')">
        <button type="button" :class="{ on: unit === 'zh' }"
                :aria-pressed="unit === 'zh'" @click="setUnit('zh')">{{ t('万 / 亿') }}</button>
        <button type="button" :class="{ on: unit === 'en' }"
                :aria-pressed="unit === 'en'" @click="setUnit('en')">K / M / B</button>
      </div>
    </div>
    <div class="ucards">
      <div class="ucard"><div class="ulabel">{{ t('总任务数') }}</div>
        <div class="uval">{{ usage?.total.tasks ?? 0 }}</div>
        <div class="usub">{{ t('台账累计（含受阻/取消）') }}</div></div>
      <div class="ucard"><div class="ulabel">{{ t('总轮数') }}</div>
        <div class="uval">{{ fmtFull(usage?.total.turns) }}</div>
        <div class="usub">{{ t('Agent 工具循环轮次') }}</div></div>
      <div class="ucard"><div class="ulabel">{{ t('输入 tokens') }}</div>
        <div class="uval">{{ fmt(usage?.total.prompt_tokens) }}</div>
        <div class="usub">{{ t('精确值') }} {{ fmtFull(usage?.total.prompt_tokens) }}</div></div>
      <div class="ucard"><div class="ulabel">{{ t('输出 tokens') }}</div>
        <div class="uval">{{ fmt(usage?.total.completion_tokens) }}</div>
        <div class="usub">{{ t('精确值') }} {{ fmtFull(usage?.total.completion_tokens) }}</div></div>
    </div>

    <NCard size="small" class="scard">
      <template #header><i class="ms">analytics</i> {{ t('各项目用量') }}</template>
      <template #header-extra>
        <span class="hint">{{ t('按总 tokens 降序；台账文件 logs/usage.jsonl，任务结束即追加') }}</span>
      </template>
      <table class="utable" v-if="usage?.projects.length">
        <thead><tr>
          <th>{{ t('项目') }}</th><th>{{ t('任务数') }}</th><th>{{ t('轮数') }}</th>
          <th>{{ t('输入 tokens') }}</th><th>{{ t('输出 tokens') }}</th><th>{{ t('合计') }}</th><th>{{ t('最近使用') }}</th>
        </tr></thead>
        <tbody>
          <tr v-for="p in usage.projects" :key="p.name">
            <td class="pname">{{ p.name }}</td>
            <td>{{ p.tasks }}</td>
            <td>{{ fmtFull(p.turns) }}</td>
            <td>{{ fmt(p.prompt_tokens) }}</td>
            <td>{{ fmt(p.completion_tokens) }}</td>
            <td class="sum">{{ fmt(p.prompt_tokens + p.completion_tokens) }}</td>
            <td class="dim">{{ p.last_at || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="uempty">{{ t('还没有用量记录——跑一轮任务后这里会按项目累计 tokens。') }}</div>
    </NCard>

    <NCard v-if="usage?.roles?.length" size="small" class="scard">
        <template #header><i class="ms">group</i> {{ t('按 Agent 角色用量') }}</template>
      <template #header-extra>
        <span class="hint">{{ t('按总 tokens 降序；小组模式展开成员明细（执行 + 触发的审查）') }}</span>
      </template>
      <table class="utable">
        <thead><tr>
          <th>{{ t('角色 / 成员') }}</th><th>{{ t('任务数') }}</th><th>{{ t('轮数') }}</th>
          <th>{{ t('输入 tokens') }}</th><th>{{ t('输出 tokens') }}</th><th>{{ t('合计') }}</th>
        </tr></thead>
        <tbody>
          <template v-for="r in usage.roles" :key="r.role">
            <tr class="role-click" :class="{ open: roleRunsRole === r.role }"
                :title="t('点击展开/收起该角色的最近运行')"
                @click="toggleRoleRuns(r.role)">
              <td class="pname">{{ roleLabel(r.role) }} <i class="ms sm">{{ roleRunsRole === r.role ? 'expand_less' : 'expand_more' }}</i></td>
              <td>{{ r.tasks }}</td>
              <td>{{ fmtFull(r.turns) }}</td>
              <td>{{ fmt(r.prompt_tokens) }}</td>
              <td>{{ fmt(r.completion_tokens) }}</td>
              <td class="sum">{{ fmt(r.prompt_tokens + r.completion_tokens) }}</td>
            </tr>
            <tr v-if="roleRunsRole === r.role && roleRunsLoading" class="member-row">
              <td colspan="6" class="hint">{{ t('加载中…') }}</td>
            </tr>
            <tr v-if="roleRunsRole === r.role && roleRunsError" class="member-row">
              <td colspan="6" class="hint">{{ roleRunsError }}</td>
            </tr>
            <tr v-for="run in roleRuns" v-show="roleRunsRole === r.role" :key="run.run_id" class="member-row">
              <td class="mname">⌙ {{ run.task_id.slice(0, 34) }}</td>
              <td>{{ run.outcome || '-' }}</td>
              <td>{{ fmtFull(run.turns) }}</td>
              <td>{{ fmt(run.tokens?.prompt_tokens || 0) }}</td>
              <td>{{ fmt(run.tokens?.completion_tokens || 0) }}</td>
              <td class="sum">{{ fmt((run.tokens?.prompt_tokens || 0) + (run.tokens?.completion_tokens || 0)) }}</td>
            </tr>
            <tr v-if="roleRunsRole === r.role && !roleRunsLoading && !roleRuns.length" class="member-row">
              <td colspan="6" class="hint">{{ t('该角色暂无运行记录') }}</td>
            </tr>
            <!-- 小组成员明细子行：执行 + 归属到该成员的审查消耗 -->
            <tr v-for="m in r.members || []" :key="r.role + '|' + m.agent" class="member-row">
              <td class="mname">└ {{ m.agent }}</td>
              <td>{{ m.tasks }}</td>
              <td>{{ fmtFull(m.turns) }}</td>
              <td>{{ fmt(m.prompt_tokens) }}</td>
              <td>{{ fmt(m.completion_tokens) }}</td>
              <td class="sum">{{ fmt(m.prompt_tokens + m.completion_tokens) }}</td>
            </tr>
          </template>
        </tbody>
      </table>
    </NCard>

    <NCard size="small" class="scard">
        <template #header><i class="ms">local_fire_department</i> {{ t('用量热力图（近 12 周）') }}</template>
      <template #header-extra><span class="hint">{{ t('色深 = 当日输入+输出 tokens；悬停看单日明细') }}</span></template>
      <div class="hm" v-if="heat.cols.length">
        <div class="hm-months">
          <span class="hm-rl-spacer"></span>
          <span v-for="(m, i) in heat.months" :key="i" class="hm-m">{{ m }}</span>
        </div>
        <div class="hm-body">
          <div class="hm-rl">
            <span v-for="(lb, i) in [t('周一'), '', t('周三'), '', t('周五'), '', '']" :key="i">{{ lb }}</span>
          </div>
          <div class="hm-grid">
            <div v-for="(c, i) in heat.cols" :key="i" class="hm-col">
              <div v-for="(cell, j) in c.cells" :key="j"
                   :class="['hm-cell', cell ? 'hm-l' + cell.level : 'hm-null']"
                   :title="cell ? cell.title : undefined"></div>
            </div>
          </div>
        </div>
        <div class="hm-legend">
          {{ t('少') }}
          <i class="hm-cell hm-l0"></i><i class="hm-cell hm-l1"></i><i class="hm-cell hm-l2"></i>
          <i class="hm-cell hm-l3"></i><i class="hm-cell hm-l4"></i>
          {{ t('多') }}
        </div>
      </div>
      <div v-else class="uempty">{{ t('暂无用量数据。') }}</div>
    </NCard>

    <NCard size="small" class="scard">
      <template #header><i class="ms">history</i> {{ t('最近任务记录') }}</template>
      <template #header-extra>
        <div class="rec-tabs">
          <button class="rtab" :class="{ on: recTab === 'all' }" @click="recTab = 'all'">
            {{ t('全部') }} {{ recentRows.length }}</button>
          <button class="rtab" :class="{ on: recTab === 'task' }" @click="recTab = 'task'">
            {{ t('任务') }} {{ taskCount }}</button>
          <button class="rtab" :class="{ on: recTab === 'review' }" @click="recTab = 'review'">
            {{ t('审查') }} {{ reviewCount }}</button>
        </div>
      </template>
      <table class="utable" v-if="filteredRecent.length">
        <thead><tr>
          <th>{{ t('时间') }}</th><th>{{ t('项目') }}</th><th>{{ t('任务') }}</th><th>{{ t('结果') }}</th>
          <th>{{ t('轮数') }}</th><th>{{ t('输入 / 输出') }}</th><th>{{ t('模型') }}</th>
        </tr></thead>
        <tbody>
          <tr v-for="(r, i) in filteredRecent" :key="i"
              :class="{ 'rev-row': isReview(r) }">
            <td class="dim">{{ r.at }}</td>
            <td class="pname">{{ r.project }}</td>
            <td class="tid">{{ r.task_id }}</td>
            <td><NTag size="small" :bordered="false" :type="tagOf(r).type">
                {{ tagOf(r).name }}</NTag></td>
            <td>{{ r.turns }}</td>
            <td>{{ fmt(r.prompt_tokens) }} / {{ fmt(r.completion_tokens) }}</td>
            <td class="dim">{{ r.model || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="uempty">{{ t('该分类下暂无记录。') }}</div>
    </NCard>
  </div>
</template>

<style scoped>
.usage { padding: 0 20px; max-width: 1080px; display: flex; flex-direction: column; gap: 14px; }
.hint { font-size: 12px; color: var(--faint); font-weight: 400; }
/* tokens 单位中英切换：中文万/亿 ↔ 英文 K/M/B */
.ubar { display: flex; align-items: center; justify-content: flex-end; gap: 8px;
  flex-wrap: wrap; margin-bottom: -4px; }
.uhint { font-size: 12px; color: var(--faint); }
.useg { display: inline-flex; gap: 2px; padding: 2px; border: 1px solid var(--line);
  border-radius: 14px; background: var(--card); }
.useg button { border: 0; border-radius: 12px; padding: 3px 10px; cursor: pointer;
  background: transparent; color: var(--dim); font: inherit; font-size: 12px;
  font-variant-numeric: tabular-nums; }
.useg button:hover { color: var(--text); }
.useg button.on { background: var(--primary, #3574f0); color: #fff; }
.ucards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.ucard { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 14px 16px; }
.ulabel { font-size: 12px; color: var(--dim); }
.uval { font-size: 24px; font-weight: 700; margin: 2px 0; color: var(--text);
  font-variant-numeric: tabular-nums; }
.usub { font-size: 11.5px; color: var(--faint); }
.utable { width: 100%; border-collapse: collapse; font-size: 13px; }
.utable th, .utable td { border-bottom: 1px solid var(--line); padding: 7px 10px;
  text-align: left; color: var(--dim); font-variant-numeric: tabular-nums; }
.utable th { color: var(--text); font-weight: 600; font-size: 12.5px; }
.utable .pname { color: var(--text); font-weight: 600; }
.utable .sum { color: var(--text); font-weight: 600; }
.utable .dim, .dim { color: var(--faint); }
.tid { font-family: Consolas, monospace; font-size: 12px; }
/* 审查记录行整行降饱和：任务记录为主角，审查是附属证据 */
.rev-row { opacity: 0.78; }
/* 角色表成员明细子行：缩进 + 弱化，从属于角色行 */
.member-row td { color: var(--dim); padding-top: 4px; padding-bottom: 4px; }
.mname { color: var(--dim); font-weight: 500; padding-left: 22px !important; }
/* ---- 最近记录分类 Tab（卡片头部分段按钮） ---- */
.rec-tabs { display: flex; gap: 4px; }
.rtab { border: 1px solid var(--line); background: transparent; color: var(--dim);
  font-size: 12px; padding: 3px 10px; border-radius: 12px; cursor: pointer;
  font-variant-numeric: tabular-nums; transition: all .15s; }
.rtab:hover { color: var(--text); border-color: var(--dim); }
.rtab.on { background: var(--primary, #3574f0); border-color: var(--primary, #3574f0);
  color: #fff; }
.uempty { color: var(--faint); font-size: 13px; text-align: center; padding: 20px 0; }
/* ---- 热力图（git 提交热力图风格：周列 × 星期行） ---- */
.hm-months { display: flex; gap: 3px; margin-bottom: 4px; }
.hm-rl-spacer { width: 34px; flex: none; }
.hm-m { width: 13px; flex: none; font-size: 11px; color: var(--faint);
  white-space: nowrap; }
.hm-body { display: flex; gap: 8px; }
.hm-rl { display: flex; flex-direction: column; gap: 3px; width: 34px; flex: none; }
.hm-rl span { height: 13px; font-size: 10.5px; line-height: 13px; color: var(--faint); }
.hm-grid { display: flex; gap: 3px; overflow-x: auto; padding-bottom: 4px; }
.hm-col { display: flex; flex-direction: column; gap: 3px; }
.hm-cell { width: 13px; height: 13px; border-radius: 3px; background: var(--hm0); flex: none; }
.hm-null { background: transparent; }
.hm-l0 { background: var(--hm0); }
.hm-l1 { background: var(--hm1); }
.hm-l2 { background: var(--hm2); }
.hm-l3 { background: var(--hm3); }
.hm-l4 { background: var(--hm4); }
.hm-legend { display: flex; gap: 4px; align-items: center; margin-top: 8px;
  font-size: 11.5px; color: var(--faint); }
.hm-legend .hm-cell { width: 11px; height: 11px; }
.scard { background: var(--panel); }
@media (max-width: 860px) { .ucards { grid-template-columns: repeat(2, 1fr); } }
</style>
