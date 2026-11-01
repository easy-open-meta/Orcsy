<script setup lang="ts">
import { NButton, NConfigProvider, NDialogProvider, NMessageProvider, NTab, NTabs, NTag, dateZhCN, zhCN } from 'naive-ui'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from './api'
import Dialogs from './components/Dialogs.vue'
import DashboardView from './views/DashboardView.vue'
import SettingsView from './views/SettingsView.vue'
import { beginRestartWait, clearPendingUpdate, getPendingUpdate, openProjectNew, refresh, store, ui } from './store'
import SkillsView from './views/SkillsView.vue'
import { cmpVersion } from './utils'
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

// ---- 程序自更新：头部按钮状态机（检查 → 有新版 → 下载安装 → 自动重启） ----
const updPhase = computed(() => store.state?.update?.phase || 'idle')
const updLatest = computed(() => store.state?.update?.latest || '')
const pending = computed(() => {
  void store.state // 建立响应式依赖：轮询更新 store 后重新求值（localStorage 本身非响应式）
  return getPendingUpdate()
})
const updWorking = computed(() =>
  ['checking', 'downloading', 'applying'].includes(updPhase.value) || ui.restarting)
const updLabel = computed(() => {
  if (pending.value) return `正在安装 v${pending.value.target}…（自动重启中）`
  if (ui.restarting) return '重启中…'
  switch (updPhase.value) {
    case 'checking': return '检查中…'
    case 'downloading': return `下载安装中 ${store.state?.update?.percent ?? 0}%`
    case 'applying': return '安装中…'
    case 'available': return `更新到 ${updLatest.value}`
    default: return '检查更新'
  }
})
const updating = ref(false)

async function checkUpdate(): Promise<void> {
  updating.value = true
  try {
    const r = await api.updateCheck()
    if (r.phase === 'latest') window.$message?.(`已是最新版本（v${r.current}）`, 'success')
    else if (r.phase === 'available') askApply(r.latest, r.notes)
  } catch (e: any) { window.$message?.(e.message, 'error') }
  finally { updating.value = false }
}

function askApply(latest: string, notes: string): void {
  window.$dialog?.warning({
    title: `发现新版本 v${latest}`,
    content: (notes || '无更新日志').slice(0, 1500)
      + '\n\n注意：若正在执行任务会被中断；安装完成后应用会自动重启，本页面也将自动刷新并提示更新结果。',
    positiveText: '立即更新',
    negativeText: '稍后再说',
    onPositiveClick() {
      beginRestartWait(latest) // 记录目标版本；页面自动探测应用回归，一上线就 reload 并核对
      api.updateApply().catch(() => { /* 退出瞬间的连接失败属预期（已进入重启等待） */ })
      window.$message?.('正在安装，应用将短暂离线并在数秒后自动重启', 'success')
    },
  })
}

// ---- 更新收尾核对：常驻 watch——挂单存在时核对版本并明确提示成功/失败 ----
// 不自停：清掉挂单后继续守候，下一次更新的挂单才能被核对（曾因自停导致
// 第二次更新的结果永远无人核对、按钮永久卡「安装中」）
onMounted(() => {
  watch(() => store.state, (s) => {
    if (!s) return
    // 上次升级结果（后端一次性曝光）：失败弹窗提示（安装日志已自动打开）；
    // 成功静默——收尾提示由下方版本核对统一给出，不重复打扰
    if (s.update_result && s.update_result.ok === false) {
      window.$dialog?.error({
        title: '升级未完成',
        content: (s.update_result.message || '更新未完成，请重试')
          + '\n\n安装日志已自动打开；如未弹出可到配置目录 logs/install.log 查看。',
      })
    }
    const pending = getPendingUpdate()
    if (!pending) { ui.restarting = false; return }
    if (s.update?.phase === 'error') {
      clearPendingUpdate()
      ui.restarting = false
      window.$message?.(s.update.message || '更新未完成，请重试', 'error')
      return
    }
    // 应用已带新版本回归：版本精确匹配挂单目标（同版本覆盖/升级/降级都以此为准；
    // 不能用 >= 判成功——降级装回旧版时版本低于目标，>= 会把「没换成功」误报为成功）
    if (cmpVersion(s.version, pending.target) === 0) {
      clearPendingUpdate()
      ui.restarting = false
      window.$message?.(`✅ 已成功更新到 v${s.version}`, 'success')
    }
    // 版本还没到目标（安装仍在进行）：继续等轮询带回新版本
  })
})

// 遮罩可见性：重启中，或后端确实处于更新周期（刷新后的页面若更新已完成则不遮）
const updateOverlayVisible = computed(() =>
  ui.restarting || (!!getPendingUpdate() &&
  ['downloading', 'applying', 'restarting'].includes(store.state?.update?.phase || '')))

// 5s 轮询：页面隐藏或弹窗打开时跳过（弹窗内表单不被覆盖）
let timer: number | undefined
onMounted(() => {
  void refresh(true)
  timer = window.setInterval(() => {
    if (!document.hidden) void refresh()
  }, 5000)
})
onUnmounted(() => { window.clearInterval(timer); pendingWatch?.() })
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
            <h1>Orcsy 任务编排中心</h1>
            <div class="sub" v-if="store.state">
              v{{ store.state.version }} · 模型 {{ store.state.llm.model }}（{{ store.state.llm.provider }}）{{ store.state.llm.key_configured ? '' : ' · 未配置 api_key' }} · 配置 {{ store.state.config_path }}
            </div>
          </div>
        </div>
        <div class="actions">
          <NTabs type="segment" :value="store.view" @update:value="(v: any) => store.view = v" class="navseg">
            <NTab name="dash">看板</NTab>
            <NTab name="settings">项目与设置</NTab>
            <NTab name="skills">技能</NTab>
          </NTabs>
          <NTag v-if="updPhase === 'available'" size="small" type="error" :bordered="false"
                title="发现新版本，点击立即更新">可更新</NTag>
          <NButton size="small" :loading="updWorking || updating" @click="checkUpdate"
                   :title="updPhase === 'available' ? '下载并安装新版本' : '检查是否有新版本'">
            {{ updLabel }}
          </NButton>
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
      <SkillsView v-show="store.view === 'skills'" />
      <Dialogs />
      <!-- 自更新进行中：全屏状态提示（禁止点击；应用重启后本页自动 reload 加载新版页面） -->
      <div v-if="updateOverlayVisible" class="upd-overlay">
        <div class="upd-box">
          <div class="upd-title">应用正常升级中....</div>
          <div class="upd-sub">升级在后台静默进行（全程无 UAC 弹窗），应用将自动重启，
            本页将在升级完成后自动刷新以加载新版本页面（请勿关闭本页）</div>
          <pre v-if="store.state?.update?.install_log_tail" class="upd-log">{{ store.state.update.install_log_tail }}</pre>
        </div>
      </div>
    </NDialogProvider></NMessageProvider>
  </NConfigProvider>
</template>

<style>
.upd-overlay { position: fixed; inset: 0; background: rgba(8, 12, 22, 0.55); z-index: 2000;
  display: flex; align-items: center; justify-content: center; }
.upd-box { background: var(--card, #1a2235); border: 1px solid var(--line, #253048);
  border-radius: 12px; padding: 26px 34px; text-align: center; max-width: 480px;
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.35); }
.upd-title { font-size: 17px; font-weight: 700; margin-bottom: 8px; }
.upd-sub { font-size: 13px; color: var(--dim, #94a2be); line-height: 1.6; }
.upd-log { max-height: 180px; overflow: auto; text-align: left; font-size: 11.5px;
  color: var(--dim, #94a2be); background: rgba(128, 128, 160, 0.12);
  border-radius: 6px; padding: 8px 10px; margin-top: 10px; white-space: pre-wrap;
  word-break: break-all; }
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
.navseg { width: 300px; max-width: 46vw; }
@media (max-width: 640px) { .brand .sub { display: none; } .navseg { width: auto; } }
</style>
