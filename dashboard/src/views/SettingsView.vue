<script setup lang="ts">
import { NAutoComplete, NButton, NCard, NInput, NInputNumber, NSelect, NSwitch, NTag } from 'naive-ui'
import { computed, reactive, ref, watch } from 'vue'
import { api, type ModelInfo } from '../api'
import { openGuide, openProjectEdit, openProjectNew, openTaskNew, store } from '../store'

const message = () => window.$message

// ---- 草稿模式：首份数据到后回填一次，之后轮询不覆盖未保存的输入 ----
const draft = reactive({
  provider: 'openai' as 'openai' | 'anthropic',
  model: '', base_url: '', api_key: '',
  temperature: 0.2, max_tokens: 8192, max_context_tokens: 131072, timeout_seconds: 300,
  interval: 300, parallel: 1, port: 5116,
})
let inited = false
watch(() => store.state, (s) => {
  if (!s || inited) return
  inited = true
  draft.provider = s.llm.provider
  // 清洗历史 bug 写入的模型名：下拉标注「（977K）」曾被当成 id 存进配置
  draft.model = s.llm.model.replace(/（\d+K）$/, '')
  draft.base_url = s.llm.base_url
  draft.api_key = s.llm.api_key || ''
  draft.temperature = s.llm.temperature
  draft.max_tokens = s.llm.max_tokens
  draft.max_context_tokens = s.llm.max_context_tokens
  draft.timeout_seconds = s.llm.timeout_seconds
  draft.interval = s.scheduler_cfg.interval_seconds
  draft.parallel = s.scheduler_cfg.max_parallel_agents
  draft.port = s.web_cfg.port
})

async function saveLlm(): Promise<void> {
  try {
    await api.config({ llm: {
      provider: draft.provider, model: draft.model, base_url: draft.base_url,
      api_key: draft.api_key, temperature: draft.temperature,
      max_tokens: draft.max_tokens, max_context_tokens: draft.max_context_tokens,
      timeout_seconds: draft.timeout_seconds,
    } })
    message()?.('已保存并生效')
  } catch (e: any) { message()?.(e.message, 'error') }
  void import('../store').then(m => m.refresh(true))
}

async function saveSched(): Promise<void> {
  try {
    await api.config({
      scheduler: { interval_seconds: draft.interval, max_parallel_agents: draft.parallel },
      web: { port: draft.port },
    })
    message()?.('已保存（端口下次启动生效）')
  } catch (e: any) { message()?.(e.message, 'error') }
  void import('../store').then(m => m.refresh(true))
}

async function toggleAutostart(enabled: boolean): Promise<void> {
  try {
    await api.autostart(enabled)
    message()?.('开机自启动已' + (enabled ? '开启' : '关闭'))
  } catch (e: any) { message()?.(e.message, 'error') }
  void import('../store').then(m => m.refresh(true))
}

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

// ---- 模型列表与规格自动填充 ----
const fetching = ref(false)
const modelSpecs = ref<ModelInfo[]>([])
// label 必须与 value 一致：naive 选中后按 label 回填输入框，带标注的 label
// 会被当成模型名存进配置（曾导致「step-5-preview（977K）」404）
const modelOptions = computed(() =>
  modelSpecs.value.map(m => ({ label: m.id, value: m.id })))

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

function onModelPick(v: string): void {
  draft.model = v
  const m = modelSpecs.value.find(x => x.id.toLowerCase() === v.toLowerCase())
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
        <div class="f2"><label>模型 model（点输入框从列表选择，可输入过滤）</label>
          <NAutoComplete v-model:value="draft.model" :options="modelOptions" placeholder="deepseek-chat 或获取列表后选择"
                         @select="onModelPick" @input="onModelPick" /></div>
        <div class="f0"><label>&nbsp;</label>
          <NButton :loading="fetching" @click="fetchModels">获取列表</NButton></div>
      </div>
      <div class="frow">
        <div class="f2"><label>API 地址 base_url</label><NInput v-model:value="draft.base_url" placeholder="https://api.deepseek.com/v1" /></div>
        <div class="f14"><label>API Key</label><NInput v-model:value="draft.api_key" type="password" show-password-on="click" placeholder="sk-..." /></div>
      </div>
      <div class="frow">
        <div class="f1"><label>temperature</label><NInputNumber v-model:value="draft.temperature" :step="0.1" :min="0" :max="2" /></div>
        <div class="f1"><label>最大输出 max_tokens</label><NInputNumber v-model:value="draft.max_tokens" :step="256" :min="256" :max="200000" /></div>
      </div>
      <div class="frow">
        <div class="f1"><label>上下文窗口 max_context_tokens</label><NInputNumber v-model:value="draft.max_context_tokens" :step="1024" :min="4096" :max="2000000" /></div>
        <div class="f1"><label>请求超时（秒）</label><NInputNumber v-model:value="draft.timeout_seconds" :min="30" :max="1800" /></div>
      </div>
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
      <div class="sfoot"><NButton type="primary" @click="saveSched">保存调度配置</NButton></div>
    </NCard>

    <NCard title="⚙️ 应用" size="small" class="scard">
      <template #header-extra><span class="hint">本机应用行为设置，保存后立即生效。</span></template>
      <label class="checkline">
        <NSwitch :value="store.state.app_autostart.enabled" @update:value="toggleAutostart" />
        开机自动启动（写入当前用户注册表）
      </label>
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
        </div>
        <div class="prow-meta">
          {{ p.path }} · 任务目录 {{ p.tasks_dir || 'tasks' }} · 每轮 {{ p.max_tasks_per_cycle || 1 }} 任务
        </div>
        <div class="ops-row">
          <NButton size="tiny" @click="openTaskNew(p.name)">新建任务</NButton>
          <NButton size="tiny" @click="openGuide(p.name)">编辑 AGENT.md</NButton>
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
.hint { font-size: 12px; color: var(--faint); font-weight: 400; }
.frow { display: flex; gap: 10px; margin-bottom: 10px; }
.frow > div { display: flex; flex-direction: column; gap: 4px; }
.frow label, .checkline { font-size: 12.5px; color: var(--dim); }
.f1 { flex: 1; } .f14 { flex: 1.4; } .f2 { flex: 2; } .f0 { flex: none; }
.sfoot { display: flex; justify-content: flex-end; }
.checkline { display: flex; gap: 10px; align-items: center; }
.prow { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; margin-bottom: 8px; }
.prow-head { display: flex; gap: 6px; align-items: center; }
.prow-meta { font-size: 12px; color: var(--dim); margin: 4px 0; word-break: break-all; }
.ops-row { display: flex; gap: 6px; }
.empty { color: var(--faint); font-size: 13px; text-align: center; padding: 18px 0; }
</style>
