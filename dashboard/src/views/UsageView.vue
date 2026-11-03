<script setup lang="ts">
// 模型用量页：总体 + 各项目 + 近 14 天趋势 + 最近任务记录。
// 数据来自 /api/state 的 usage 聚合（台账 logs/usage.jsonl，任务结束即记）。
import { NCard, NTag } from 'naive-ui'
import { computed } from 'vue'
import { store } from '../store'
import { fmtTokens } from '../utils'

const usage = computed(() => store.state?.usage)

// fmtTokens 对 0 返回空串（规格展示语义）；用量页要显式看到 0
function fmt(v: number | undefined): string {
  return fmtTokens(v) || String(v ?? 0)
}
function fmtFull(v: number | undefined): string {
  return (v ?? 0).toLocaleString('zh-CN')
}

const OUT_TAG: Record<string, { type: 'success' | 'warning' | 'default' | 'error'; name: string }> = {
  done: { type: 'success', name: '完成' },
  blocked: { type: 'warning', name: '受阻' },
  cancelled: { type: 'default', name: '取消' },
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
      title: `${d.date} · ${d.tasks} 个任务 · ${fmt(tokens)} tokens`,
    })
    if (r === 6) col = null
  }
  if (col) while (col.cells.length < 7) col.cells.push(null)  // 末列补满一周
  // 月份标签：列首日期换月时显示（首列也显示）
  const months = cols.map((c, i) =>
    (i === 0 || c.month !== cols[i - 1].month) ? `${Number(c.month.slice(5))}月` : '')
  return { cols, months }
})
</script>

<template>
  <div class="usage" v-if="store.state">
    <div class="ucards">
      <div class="ucard"><div class="ulabel">总任务数</div>
        <div class="uval">{{ usage?.total.tasks ?? 0 }}</div>
        <div class="usub">台账累计（含受阻/取消）</div></div>
      <div class="ucard"><div class="ulabel">总轮数</div>
        <div class="uval">{{ fmtFull(usage?.total.turns) }}</div>
        <div class="usub">Agent 工具循环轮次</div></div>
      <div class="ucard"><div class="ulabel">输入 tokens</div>
        <div class="uval">{{ fmt(usage?.total.prompt_tokens) }}</div>
        <div class="usub">精确值 {{ fmtFull(usage?.total.prompt_tokens) }}</div></div>
      <div class="ucard"><div class="ulabel">输出 tokens</div>
        <div class="uval">{{ fmt(usage?.total.completion_tokens) }}</div>
        <div class="usub">精确值 {{ fmtFull(usage?.total.completion_tokens) }}</div></div>
    </div>

    <NCard title="📊 各项目用量" size="small" class="scard">
      <template #header-extra>
        <span class="hint">按总 tokens 降序；台账文件 logs/usage.jsonl，任务结束即追加</span>
      </template>
      <table class="utable" v-if="usage?.projects.length">
        <thead><tr>
          <th>项目</th><th>任务数</th><th>轮数</th>
          <th>输入 tokens</th><th>输出 tokens</th><th>合计</th><th>最近使用</th>
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
      <div v-else class="uempty">还没有用量记录——跑一轮任务后这里会按项目累计 tokens。</div>
    </NCard>

    <NCard title="🔥 用量热力图（近 12 周）" size="small" class="scard">
      <template #header-extra><span class="hint">色深 = 当日输入+输出 tokens；悬停看单日明细</span></template>
      <div class="hm" v-if="heat.cols.length">
        <div class="hm-months">
          <span class="hm-rl-spacer"></span>
          <span v-for="(m, i) in heat.months" :key="i" class="hm-m">{{ m }}</span>
        </div>
        <div class="hm-body">
          <div class="hm-rl">
            <span v-for="(lb, i) in ['周一', '', '周三', '', '周五', '', '']" :key="i">{{ lb }}</span>
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
          少
          <i class="hm-cell hm-l0"></i><i class="hm-cell hm-l1"></i><i class="hm-cell hm-l2"></i>
          <i class="hm-cell hm-l3"></i><i class="hm-cell hm-l4"></i>
          多
        </div>
      </div>
      <div v-else class="uempty">暂无用量数据。</div>
    </NCard>

    <NCard title="🕘 最近任务记录" size="small" class="scard">
      <template #header-extra><span class="hint">最新 20 条</span></template>
      <table class="utable" v-if="usage?.recent.length">
        <thead><tr>
          <th>时间</th><th>项目</th><th>任务</th><th>结果</th>
          <th>轮数</th><th>输入 / 输出</th><th>模型</th>
        </tr></thead>
        <tbody>
          <tr v-for="(r, i) in usage.recent" :key="i">
            <td class="dim">{{ r.at }}</td>
            <td class="pname">{{ r.project }}</td>
            <td class="tid">{{ r.task_id }}</td>
            <td><NTag size="small" :bordered="false"
                  :type="(OUT_TAG[r.outcome] || { type: 'default' }).type">
                {{ (OUT_TAG[r.outcome] || { name: r.outcome }).name }}</NTag></td>
            <td>{{ r.turns }}</td>
            <td>{{ fmt(r.prompt_tokens) }} / {{ fmt(r.completion_tokens) }}</td>
            <td class="dim">{{ r.model || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="uempty">暂无任务记录。</div>
    </NCard>
  </div>
</template>

<style scoped>
.usage { padding: 0 20px; max-width: 1080px; display: flex; flex-direction: column; gap: 14px; }
.hint { font-size: 12px; color: var(--faint); font-weight: 400; }
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
