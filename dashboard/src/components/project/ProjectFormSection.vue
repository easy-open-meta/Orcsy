<script setup lang="ts">
defineProps<{
  title: string
  icon: string
  summary?: string
}>()
</script>

<template>
  <details class="project-section">
    <summary class="project-section-head">
      <i class="ms project-section-icon" aria-hidden="true">{{ icon }}</i>
      <span class="project-section-title">{{ title }}</span>
      <span v-if="summary" class="project-section-summary" :title="summary">{{ summary }}</span>
      <i class="ms project-section-chevron" aria-hidden="true">expand_more</i>
    </summary>
    <div class="project-section-body"><slot /></div>
  </details>
</template>

<style scoped>
.project-section { min-width: 0; border: 1px solid var(--line); border-radius: 8px; }
.project-section-head { display: flex; align-items: center; gap: 8px; min-height: 46px;
  box-sizing: border-box; padding: 10px 12px; cursor: pointer; list-style: none; }
.project-section-head::-webkit-details-marker { display: none; }
.project-section-head::before { display: none; }
.project-section-head::marker { content: ''; }
.project-section-head:hover { background: rgba(128, 140, 180, 0.06); }
.project-section-head:focus-visible { outline: 2px solid var(--acc); outline-offset: 2px; border-radius: 6px; }
.project-section-icon, .project-section-chevron { flex: none; font-size: 18px; color: var(--faint); }
.project-section-title { min-width: 0; flex: 1; font-size: 13px; font-weight: 600;
  color: var(--text); overflow-wrap: anywhere; }
.project-section-summary { min-width: 0; max-width: 42%; color: var(--dim); font-size: 12px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.project-section[open] > .project-section-head { border-bottom: 1px solid var(--line); }
.project-section[open] > .project-section-head .project-section-chevron { transform: rotate(180deg); }
.project-section-body { padding: 10px 12px 12px; min-width: 0; }
@media (max-width: 768px) {
  .project-section-head { flex-wrap: wrap; gap: 4px 8px; padding: 10px; }
  .project-section-title { flex-basis: calc(100% - 52px); }
  .project-section-summary { order: 1; max-width: calc(100% - 26px); margin-left: 26px; }
  .project-section-body { padding: 8px 10px 12px; }
}
</style>
