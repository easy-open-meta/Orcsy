<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  tasks: { id: string; title: string; status: string; depends_on?: string[] }[]
}>()
const emit = defineEmits<{ (e: 'open', id: string): void }>()

const NODE_W = 168
const NODE_H = 40
const ROW_GAP = 84
const COL_GAP = 28
const PAD = 14

const ST_COLOR: Record<string, string> = {
  open: '#5b8cff',
  in_progress: '#f0a020',
  blocked: '#e05a4e',
  done: '#39ad5a',
  cancelled: '#9aa3b2',
}
const ST_TEXT: Record<string, string> = {
  open: '待办',
  in_progress: '执行中',
  blocked: '受阻',
  done: '已完成',
  cancelled: '已取消',
}

const layout = computed(() => {
  const tasks = props.tasks
  const ids = new Set(tasks.map(t => t.id))
  const deps = new Map(tasks.map(t => [t.id, (t.depends_on || []).filter(d => ids.has(d))]))

  // 层级 = 最长路径（从无依赖的根起）；访问中回边计 0 防环死循环
  const level = new Map<string, number>()
  const visiting = new Set<string>()
  const lvl = (id: string): number => {
    if (level.has(id)) return level.get(id)!
    if (visiting.has(id)) return 0
    visiting.add(id)
    const ds = deps.get(id) || []
    const l = ds.length ? Math.max(...ds.map(d => lvl(d) + 1)) : 0
    visiting.delete(id)
    level.set(id, l)
    return l
  }
  tasks.forEach(t => lvl(t.id))

  const maxL = Math.max(0, ...[...level.values()])
  const rowNext = new Array<number>(maxL + 1).fill(0)
  const nodes = tasks.map(t => {
    const l = level.get(t.id) || 0
    const x = PAD + rowNext[l] * (NODE_W + COL_GAP)
    const y = PAD + l * (NODE_H + ROW_GAP)
    rowNext[l]++
    return { t, x, y }
  })
  const pos = new Map(nodes.map(n => [n.t.id, n]))

  const edges: { x1: number; y1: number; x2: number; y2: number; c: string }[] = []
  for (const t of tasks) {
    for (const d of deps.get(t.id) || []) {
      const a = pos.get(d)
      const b = pos.get(t.id)
      if (!a || !b) continue
      edges.push({ x1: a.x + NODE_W, y1: a.y + NODE_H / 2, x2: b.x, y2: b.y + NODE_H / 2, c: ST_COLOR[b.t.status] || '#888' })
    }
  }

  const w = PAD * 2 + Math.max(1, ...rowNext) * (NODE_W + COL_GAP) - COL_GAP
  const h = PAD * 2 + (maxL + 1) * (NODE_H + ROW_GAP) - ROW_GAP
  return { nodes, edges, w, h }
})

function trunc(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + '…' : s
}
</script>

<template>
  <div v-if="tasks.length" class="dag-wrap">
    <svg :width="layout.w" :height="layout.h" class="dag-svg">
      <defs>
        <marker id="dag-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--dim, #8891a5)" />
        </marker>
      </defs>
      <path v-for="(e, i) in layout.edges" :key="'e' + i" class="dag-edge"
            :d="`M ${e.x1} ${e.y1} C ${e.x1 + 34} ${e.y1}, ${e.x2 - 34} ${e.y2}, ${e.x2} ${e.y2}`"
            :stroke="e.c" marker-end="url(#dag-arrow)" />
      <g v-for="n in layout.nodes" :key="n.t.id" class="dag-node" @click="emit('open', n.t.id)">
        <rect :x="n.x" :y="n.y" :width="NODE_W" :height="NODE_H" rx="8"
              :fill="ST_COLOR[n.t.status] || '#888'" fill-opacity="0.14"
              :stroke="ST_COLOR[n.t.status] || '#888'" stroke-width="1.4" />
        <text :x="n.x + 10" :y="n.y + 17" class="dag-node-title">{{ trunc(n.t.title, 24) }}</text>
        <text :x="n.x + 10" :y="n.y + 32" class="dag-node-meta">{{ n.t.id.slice(0, 26) }} · {{ ST_TEXT[n.t.status] || n.t.status }}</text>
      </g>
    </svg>
    <div class="dag-empty" v-if="!layout.nodes.length">{{ '暂无任务节点' }}</div>
  </div>
  <div v-else class="dag-empty">{{ '暂无任务节点' }}</div>
</template>

<style scoped>
.dag-wrap { overflow: auto; border: 1px solid var(--line, #e2e6ef); border-radius: 8px; background: var(--bg-deep, rgba(127, 145, 180, 0.06)); max-height: 420px; }
.dag-svg { display: block; }
.dag-node { cursor: pointer; }
.dag-node:hover rect { fill-opacity: 0.26; }
.dag-node-title { font-size: 12px; fill: var(--text, #2a3550); font-weight: 600; }
.dag-node-meta { font-size: 10px; fill: var(--dim, #8891a5); }
.dag-edge { fill: none; stroke-width: 1.4; opacity: 0.75; }
.dag-empty { padding: 10px 12px; font-size: 12px; color: var(--dim, #8891a5); }
</style>
