<script setup lang="ts">
import { NButton, NCard, NSwitch, NTag } from 'naive-ui'
import { computed, reactive, ref } from 'vue'
import { api, type SkillT } from '../api'
import { openDirBrowser, openSkillEdit, openSkillNew, openSkillScan, refresh, store } from '../store'

const message = () => window.$message

// 导入本地技能：复用目录选择器选定技能目录（须含 SKILL.md），后端整目录复制入库
function importSkill(): void {
  openDirBrowser('', async (path: string) => {
    try {
      const r = await api.skillImport(path)
      message()?.(r.message || '已导入', 'success')
    } catch (e: any) { message()?.(e.message, 'error') }
    void refresh(true)
  })
}

// 删除技能（单个/批量）：清掉技能库中的副本。导入技能的原样本仍在工具目录
// 里可重新扫描找回；自建技能没有别处副本，删除后不可恢复——文案按 origin 区分
const selected = ref<Record<string, boolean>>({})
const checkedNames = computed(() =>
  store.state?.skills.filter(s => selected.value[s.name]).map(s => s.name) ?? [])

// 全选 / 反选：服务于批量移除与批量删除
function selectAll(): void {
  const next: Record<string, boolean> = {}
  for (const s of store.state?.skills ?? []) next[s.name] = true
  selected.value = next
}
function invertSelect(): void {
  const next: Record<string, boolean> = {}
  for (const s of store.state?.skills ?? []) next[s.name] = !selected.value[s.name]
  selected.value = next
}

function removeSkills(names: string[]): void {
  if (!names.length) return
  const chosen = (store.state?.skills ?? []).filter(s => names.includes(s.name))
  const created = chosen.filter(s => s.origin === 'created')
  let content: string
  if (chosen.length === 1) {
    const s = chosen[0]
    content = s.origin === 'created'
      ? `将彻底删除技能 ${s.name}（含附属文件），不可恢复——本程序新建的技能没有别处副本！`
      : `将从技能库删除技能 ${s.name} 的副本？（原样本仍在 AI 工具目录里，随时可重新扫描导入）`
  } else {
    content = `将删除选中的 ${chosen.length} 个技能？外部导入的技能可重新扫描找回`
      + (created.length
        ? `；其中 ${created.length} 个为自建技能（${created.map(s => s.name).join('、')}），将彻底丢失不可恢复！`
        : '。')
  }
  window.$dialog?.warning({
    title: '删除技能',
    content,
    positiveText: '删除',
    negativeText: '取消',
    async onPositiveClick() {
      try {
        const r = await api.skillRemove(names)
        message()?.(r.message || '已删除', 'success')
        selected.value = {}
      } catch (e: any) { message()?.(e.message, 'error') }
      void refresh(true)
    },
  })
}

async function toggleEnabled(s: SkillT, enabled: boolean): Promise<void> {
  try { await api.skillEnabled(s.name, enabled) }
  catch (e: any) { message()?.(e.message, 'error') }
  void refresh(true)
}

async function openFolder(s: SkillT): Promise<void> {
  try { await api.skillOpenFolder(s.name) }
  catch (e: any) { message()?.(e.message, 'error') }
}

// 专属技能绑定的项目已不存在（被删除/移除）时提示失效
function dangling(s: SkillT): boolean {
  return s.scope !== 'global'
    && !store.state?.projects.some(p => p.name === s.scope)
}

const projectCount = computed(() => store.state?.projects.length ?? 0)
</script>

<template>
  <div class="skills" v-if="store.state">
    <NCard title="🧩 技能库" size="small" class="scard">
      <template #header-extra>
        <div class="head-ops">
          <NButton size="small" @click="openSkillScan">🔍 扫描 AI 工具</NButton>
          <NButton size="small" @click="importSkill">⬆ 导入本地技能</NButton>
          <NButton type="primary" size="small" @click="openSkillNew">＋ 新建技能</NButton>
          <NButton v-if="checkedNames.length" size="small" type="error"
                   @click="removeSkills(checkedNames)">删除选中（{{ checkedNames.length }}）</NButton>
        </div>
      </template>
      <div class="hint">
        技能集中存放在 {{ store.state.skills_dir }}，绝不写入项目目录。生效方式：系统提示词只注入
        「名称+描述」索引，Agent 执行任务时按需用 read_skill 工具读取全文。
        新建技能可选全局或绑定单个项目；当前共 {{ projectCount }} 个受管项目。
      </div>
      <div v-if="store.state.skills.length" class="sel-bar">
        <NButton size="tiny" quaternary @click="selectAll">全选</NButton>
        <NButton size="tiny" quaternary @click="invertSelect">反选</NButton>
        <span class="sel-hint">已选 {{ checkedNames.length }} / {{ store.state.skills.length }}</span>
      </div>
      <div v-for="s in store.state.skills" :key="s.name" class="srow">
        <div class="srow-head">
          <input v-model="selected[s.name]" type="checkbox" class="pick"
                 :title="`选中 ${s.name} 以便批量移除/删除`" />
          <NTag size="small" :bordered="false" :type="s.enabled ? 'info' : 'default'">{{ s.name }}</NTag>
          <NTag size="small" :bordered="false" :type="s.origin === 'created' ? 'primary' : 'default'"
                :title="s.origin === 'created' ? '本程序新建：可移除或彻底删除' : '外部导入：只能移除（保留文件）'">
            {{ s.origin === 'created' ? '自建' : '导入' }}
          </NTag>
          <NTag size="small" :bordered="false"
                :type="s.scope === 'global' ? 'success' : (dangling(s) ? 'warning' : 'primary')">
            {{ s.scope === 'global' ? '全局' : `专属·${s.scope}` }}
          </NTag>
          <NTag v-if="dangling(s)" size="small" type="warning" :bordered="false">项目不存在</NTag>
          <NTag v-if="s.error" size="small" type="error" :bordered="false">解析失败</NTag>
          <span class="spacer" />
          <label class="swline" :title="s.enabled ? '已启用，点击停用' : '已停用，点击启用'">
            <NSwitch size="small" :value="s.enabled" @update:value="(v: boolean) => toggleEnabled(s, v)" />
          </label>
        </div>
        <div class="srow-meta">
          {{ s.description || '（无描述）' }}
        </div>
        <div class="srow-path">{{ s.path }}</div>
        <div class="ops-row">
          <NButton size="tiny" @click="openSkillEdit(s.name)">编辑</NButton>
          <NButton size="tiny" @click="openFolder(s)">打开文件夹</NButton>
          <NButton v-if="s.origin === 'created'" size="tiny" quaternary type="error"
                   @click="removeSkills([s.name])">删除</NButton>
          <NButton v-else size="tiny" quaternary type="warning"
                   @click="removeSkills([s.name])">移除</NButton>
        </div>
      </div>
      <div v-if="!store.state.skills.length" class="empty">
        还没有技能：点右上「导入本地技能」选择含 SKILL.md 的目录，或「新建技能」从零开始
      </div>
    </NCard>
  </div>
</template>

<style scoped>
.skills { padding: 0 20px; max-width: 1080px; }
.head-ops { display: flex; gap: 8px; }
.hint { font-size: 12px; color: var(--faint); margin-bottom: 12px; word-break: break-all; }
.sel-bar { display: flex; gap: 6px; align-items: center; margin-bottom: 8px; }
.sel-hint { font-size: 12px; color: var(--faint); }
.srow { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; margin-bottom: 8px; }
.srow-head { display: flex; gap: 6px; align-items: center; }
.srow-head .pick { flex: none; cursor: pointer; }
.spacer { flex: 1; }
.swline { display: flex; align-items: center; }
.srow-meta { font-size: 12.5px; color: var(--dim); margin: 4px 0; }
.srow-path { font-size: 11.5px; color: var(--faint); margin-bottom: 4px; word-break: break-all; }
.ops-row { display: flex; gap: 6px; }
.empty { color: var(--faint); font-size: 13px; text-align: center; padding: 18px 0; }
</style>
