<script setup lang="ts">
import { NButton, NConfigProvider, NDialogProvider, NMessageProvider, NTab, NTabs, dateZhCN, zhCN } from 'naive-ui'
import { onMounted, onUnmounted, ref } from 'vue'
import { api } from './api'
import Dialogs from './components/Dialogs.vue'
import DashboardView from './views/DashboardView.vue'
import SettingsView from './views/SettingsView.vue'
import { openProjectNew, refresh, store } from './store'
import { naiveTheme, theme, themeOverrides, toggleTheme } from './theme'

const scanning = ref(false)

async function doScan(): Promise<void> {
  scanning.value = true
  try {
    const r = await api.scan()
    window.$message?.(r.message || '已触发扫描')
  } catch (e: any) {
    window.$message?.(e.message, 'error')
  } finally {
    // 防连点：保持禁用 1.2s（与旧版一致）
    setTimeout(() => { scanning.value = false }, 1200)
  }
  void refresh(true)
}

// 5s 轮询：页面隐藏或弹窗打开时跳过（弹窗内表单不被覆盖）
let timer: number | undefined
onMounted(() => {
  void refresh(true)
  timer = window.setInterval(() => {
    if (!document.hidden) void refresh()
  }, 5000)
})
onUnmounted(() => window.clearInterval(timer))
</script>

<template>
  <NConfigProvider :theme="naiveTheme" :theme-overrides="themeOverrides" :locale="zhCN" :date-locale="dateZhCN">
    <NMessageProvider><NDialogProvider>
      <header class="top">
        <div class="brand">
          <svg class="logo" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <rect x="3" y="5" width="4.5" height="14" rx="1.2"/><rect x="9.75" y="5" width="4.5" height="9" rx="1.2" opacity=".6"/><rect x="16.5" y="5" width="4.5" height="6" rx="1.2" opacity=".35"/>
          </svg>
          <div>
            <h1>任务编排中心</h1>
            <div class="sub" v-if="store.state">
              v{{ store.state.version }} · 模型 {{ store.state.llm.model }}（{{ store.state.llm.provider }}）{{ store.state.llm.key_configured ? '' : ' · 未配置 api_key' }} · 配置 {{ store.state.config_path }}
            </div>
          </div>
        </div>
        <div class="actions">
          <NTabs type="segment" :value="store.view" @update:value="(v: any) => store.view = v" class="navseg">
            <NTab name="dash">看板</NTab>
            <NTab name="settings">项目与设置</NTab>
          </NTabs>
          <NButton type="primary" :loading="scanning" @click="doScan">▶ 立即扫描</NButton>
          <NButton @click="openProjectNew">＋ 新建项目</NButton>
          <NButton quaternary circle title="刷新" @click="refresh(true)">↻</NButton>
          <NButton quaternary circle :title="theme === 'dark' ? '切换浅色' : '切换深色'" @click="toggleTheme">
            {{ theme === 'dark' ? '☀' : '☾' }}
          </NButton>
        </div>
      </header>
      <DashboardView v-show="store.view === 'dash'" />
      <SettingsView v-show="store.view === 'settings'" />
      <Dialogs />
    </NDialogProvider></NMessageProvider>
  </NConfigProvider>
</template>

<style>
/* 双主题 token：Naive 只给自家组件上色，裸 HTML 元素（标题/说明/路径/日志等）
   必须用这里的变量，否则暗色主题下黑字黑底不可见 */
:root[data-theme="dark"]{
  --bg:#0b0f17; --panel:#131a2a; --card:#1a2235; --line:#253048; --line2:#31405f;
  --text:#e5eaf6; --dim:#94a2be; --faint:#64748f;
}
:root[data-theme="light"]{
  --bg:#f2f4f9; --panel:#ffffff; --card:#f7f8fc; --line:#e4e9f3; --line2:#cbd5ea;
  --text:#1d2637; --dim:#5c6b85; --faint:#8e9bb3;
}
html { background: var(--bg); scrollbar-color: rgba(128, 128, 160, 0.4) transparent; }
body {
  margin: 0; padding-bottom: 40px;
  background: var(--bg); color: var(--text);
  font: 14px/1.65 "Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing: antialiased;
}

.top {
  position: sticky; top: 0; z-index: 20;
  display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; justify-content: space-between;
  padding: 10px 20px; margin-bottom: 14px;
  background: var(--panel);
  border-bottom: 1px solid var(--line);
}
.brand { display: flex; gap: 12px; align-items: center; min-width: 0; flex: 1 1 260px; }
/* 关键：内层 div 也要允许收缩，否则 .sub 的 nowrap 撑破父级、文字溢出到页签下 */
.brand > div { min-width: 0; }
.brand .logo { width: 30px; height: 30px; color: #5b8cff; flex: none; }
.brand h1 { margin: 0; font-size: 17px; line-height: 1.3; color: var(--text); }
.brand .sub { font-size: 12px; color: var(--dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.actions { display: flex; gap: 8px; align-items: center; flex: none; flex-wrap: wrap; justify-content: flex-end; }
.navseg { width: 230px; max-width: 40vw; }
@media (max-width: 640px) { .brand .sub { display: none; } .navseg { width: auto; } }
</style>
