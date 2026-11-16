<script setup lang="ts">
import { NButton, NCheckbox, NConfigProvider, NDialogProvider, NDrawer, NDrawerContent, NInput, NMessageProvider, NTab, NTabs, NTag, dateZhCN, zhCN } from 'naive-ui'
import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import { useRouter, RouterView } from 'vue-router'
import { api } from './api'
import Dialogs from './components/Dialogs.vue'
import { auth, beginRestartWait, clearPendingUpdate, doLogin, doLogout, getPendingUpdate, initAuth, needLogin, openProjectNew, refresh, store, ui } from './store'
import { isMobile, onPageVisible, useMobileViewport } from './mobile'
import { cmpVersion } from './utils'
import { naiveTheme, theme, themeIcon, themeLabel, themeOverrides, toggleTheme } from './theme'

const router = useRouter()
useMobileViewport()  // 断点感知：≤768px 启用移动专属布局（顶栏/底部导航/抽屉菜单）
function onTabChange(v: any): void {
  void router.push({ name: String(v) })
}
function go(view: string): void {
  void router.push({ name: view })
  menuOpen.value = false
}

const scanning = shallowRef(false)

// ---- 登录层：鉴权开启且会话无效时全屏接管（App 壳与轮询全部静默） ----
const showLogin = computed(() => needLogin())
const loginPwd = shallowRef('')
const loginBusy = shallowRef(false)
const loginErr = shallowRef('')
const loginRemember = shallowRef(true)  // 手机远程默认记住 30 天：息屏/切网少掉登录

// 「一键开放手机访问」生成的访问密码：向导把它暂存在 sessionStorage（配置里只存
// 密文不可找回），重启 reload 后在登录页横幅再展示一次——复制登录后即清除
const mobilePwdHint = shallowRef('')

function showMobilePwdHint(): void {
  try { mobilePwdHint.value = sessionStorage.getItem('orcsyMobilePwd') || '' } catch { /* 隐私模式等 */ }
}

function dismissMobilePwdHint(): void {
  try { sessionStorage.removeItem('orcsyMobilePwd') } catch { /* 同上 */ }
  mobilePwdHint.value = ''
}

async function submitLogin(): Promise<void> {
  if (!loginPwd.value || loginBusy.value) return
  loginBusy.value = true
  loginErr.value = ''
  try {
    await doLogin(loginPwd.value, loginRemember.value)
    loginPwd.value = ''
    dismissMobilePwdHint()  // 用新密码登录成功：横幅使命完成
  } catch (e: any) {
    loginErr.value = e.message || '登录失败'
  } finally { loginBusy.value = false }
}

async function onLogout(): Promise<void> {
  menuOpen.value = false
  await doLogout()
}

// ---- 移动端专属：右侧抽屉菜单（低频操作收纳）与「＋新建任务」项目选择 ----
const menuOpen = shallowRef(false)
const pickTaskOpen = shallowRef(false)
const menuProjects = computed(() => (store.state?.projects || []).filter(p => p.enabled))
const projectsWithoutTasks = computed(() => menuProjects.value.filter(p => !p.tasks.some(t => t.status === 'in_progress')))

function openMobileNewTask(): void {
  pickTaskOpen.value = true
}
function pickProjectNewTask(name: string): void {
  pickTaskOpen.value = false
  ui.taskDlg = { project: name, id: null }
}

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
// 挂起的人工审批数（>0 时顶栏显示「🔔 待审批」入口；面板在 Dialogs.vue）
const pendingApprovals = computed(() => store.state?.approvals?.length || 0)
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
const updating = shallowRef(false)

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

// ---- 手动重启：右上角重启按钮。同一套「重启等待」UI：遮罩 + 应用回归自动 reload。
// reload 后版本号不变（无版本核对），靠挂单超时（10 分钟）自动失效收尾。
const restartingNow = shallowRef(false)

function askRestart(): void {
  window.$dialog?.warning({
    title: '重启程序？',
    content: '重启后配置与代码变更即时生效。若正在执行任务会被中断（任务保留为进行中，'
      + '重启完成后重新入队执行）。',
    positiveText: '立即重启',
    negativeText: '取消',
    onPositiveClick() {
      restartingNow.value = true
      beginRestartWait() // 不带目标版本：仅遮罩 + 探测回归后 reload
      api.restartApp().catch(() => { /* 退出瞬间的连接失败属预期（已进入重启等待） */ })
      window.$message?.('正在重启，应用将短暂离线并在数秒后自动回来', 'success')
    },
  })
}

// ---- 更新收尾核对：常驻 watch——挂单存在时核对版本并明确提示成功/失败 ----
// 不自停：清掉挂单后继续守候，下一次更新的挂单才能被核对（曾因自停导致
// 第二次更新的结果永远无人核对、按钮永久卡「安装中」）
onMounted(() => {
  pendingWatch = watch(() => store.state, (s) => {
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
      window.$message?.(`已成功更新到 v${s.version}`, 'success')
    }
    // 版本还没到目标（安装仍在进行）：继续等轮询带回新版本
  })
})

// 遮罩可见性：重启中，或后端确实处于更新周期（刷新后的页面若更新已完成则不遮）
const updateOverlayVisible = computed(() =>
  ui.restarting || (!!getPendingUpdate() &&
  ['downloading', 'applying', 'restarting'].includes(store.state?.update?.phase || '')))

// 版本核对 watch 的停止句柄（组件卸载时停表；曾因未赋值导致 onUnmounted ReferenceError）
let pendingWatch: (() => void) | undefined

// 5s 轮询：页面隐藏、弹窗打开或未登录时跳过（未登录时 refresh 内部静默短路）
let timer: number | undefined
onMounted(async () => {
  await initAuth()          // 先探鉴权：未登录则不拉状态，直接落登录层
  showMobilePwdHint()       // 一键开放手机访问后重启：登录页横幅提示新生成的密码
  if (!needLogin()) void refresh(true)
  timer = window.setInterval(() => {
    if (!document.hidden) void refresh()
  }, 5000)
  // 息屏自愈：手机锁屏/切后台期间定时器被系统挂起，回到前台立即拉一次
  // 最新状态（顺带探活会话），无感重连并恢复轮询
  onPageVisible(() => { if (!needLogin()) void refresh(true) })
})
onUnmounted(() => { window.clearInterval(timer); pendingWatch?.() })
</script>

<template>
  <NConfigProvider :theme="naiveTheme" :theme-overrides="themeOverrides" :locale="zhCN" :date-locale="dateZhCN">
    <NMessageProvider><NDialogProvider>
      <!-- 桌面端顶栏（≤768px 隐藏，移动端用极简吸顶顶栏 + 底部导航） -->
      <!-- 未登录时隐藏顶栏：登录层视觉已遮住，但键盘 Tab 不应能触达后面的按钮 -->
      <header class="top" v-show="!showLogin && !isMobile">
        <div class="brand">
          <img class="logo" src="./assets/logo.png" alt="Orcsy" draggable="false">
          <div>
            <h1>Orcsy 任务编排中心</h1>
            <div class="sub" v-if="store.state">
              v{{ store.state.version }} · 模型 {{ store.state.llm.model }}（{{ store.state.llm.provider }}）{{ store.state.llm.key_configured ? '' : ' · 未配置 api_key' }} · 配置 {{ store.state.config_path }}
            </div>
          </div>
        </div>
        <div class="actions">
          <NTabs type="segment" :value="store.view" @update:value="onTabChange" class="navseg">
            <NTab name="dash">看板</NTab>
            <NTab name="projects">项目</NTab>
            <NTab name="settings">设置</NTab>
            <NTab name="skills">技能</NTab>
            <NTab name="usage">用量</NTab>
          </NTabs>
          <NTag v-if="updPhase === 'available'" size="small" type="error" :bordered="false"
                title="发现新版本，点击立即更新"><i class="ms sm">update</i> 可更新</NTag>
          <NButton v-if="pendingApprovals > 0" size="small" type="error" secondary
                   :loading="false" title="Agent 有操作挂起等待人工审批，点击处理"
                   @click="ui.approvalDlg = true">
            <i class="ms sm">notifications</i> 待审批 {{ pendingApprovals }}
          </NButton>
          <NButton :loading="updWorking || updating" @click="checkUpdate"
                   :title="updPhase === 'available' ? '下载并安装新版本' : '检查是否有新版本'">
            <i class="ms sm">update</i> {{ updLabel }}
          </NButton>
          <NButton type="primary" :loading="scanning" @click="doScan"><i class="ms sm">play_arrow</i> 立即扫描</NButton>
          <NButton :loading="restartingNow" title="重启程序：配置与代码变更即时生效"
                   @click="askRestart"><i class="ms sm">refresh</i> 重启</NButton>
          <NButton @click="openProjectNew"><i class="ms sm">add</i> 新建项目</NButton>
          <NButton quaternary circle title="手机连接：局域网地址与扫码二维码" @click="ui.connectDlg = true"><i class="ms">smartphone</i></NButton>
          <NButton quaternary circle title="刷新" @click="refresh(true)"><i class="ms">refresh</i></NButton>
          <NButton quaternary circle :title="'主题：' + themeLabel + '（点击切换）'" @click="toggleTheme">
            <i class="ms">{{ themeIcon }}</i>
          </NButton>
          <NButton v-if="auth.enabled && auth.authed" quaternary size="small"
                   title="退出登录" @click="onLogout">退出</NButton>
        </div>
      </header>
      <!-- 移动端极简吸顶顶栏：Logo + 待审批徽标 + 刷新 + 抽屉菜单入口 -->
      <header class="mtop" v-show="!showLogin && isMobile">
        <img class="logo" src="./assets/logo.png" alt="Orcsy" draggable="false">
        <div class="mtitle">
          Orcsy
          <span class="msub" v-if="store.view === 'dash'">看板</span>
          <span class="msub" v-else-if="store.view === 'projects'">项目</span>
          <span class="msub" v-else-if="store.view === 'settings'">设置</span>
          <span class="msub" v-else-if="store.view === 'skills'">技能</span>
          <span class="msub" v-else-if="store.view === 'usage'">用量</span>
          <span class="msub" v-else>…</span>
        </div>
        <div class="mspace" />
        <NTag v-if="updPhase === 'available'" size="small" type="error" :bordered="false"
              @click="checkUpdate"><i class="ms sm">update</i> 可更新</NTag>
        <button v-if="pendingApprovals > 0" class="mbtn bell" title="待人工审批"
                @click="ui.approvalDlg = true"><i class="ms">notifications</i><i>{{ pendingApprovals }}</i></button>
        <button class="mbtn" title="刷新" @click="refresh(true)"><i class="ms">refresh</i></button>
      </header>
      <RouterView v-slot="{ Component }">
        <KeepAlive>
          <component :is="Component" />
        </KeepAlive>
      </RouterView>
      <Dialogs />
      <!-- 移动端右侧抽屉菜单：导航 + 低频操作收纳（顶栏只留最高频三个入口） -->
      <NDrawer v-model:show="menuOpen" placement="right" :width="300">
        <NDrawerContent title="Orcsy 菜单" closable>
          <div class="menu-nav">
            <button class="menu-item" :class="{ on: store.view === 'dash' }" @click="go('dash')"><i class="ms">dashboard</i> 看板</button>
            <button class="menu-item" :class="{ on: store.view === 'projects' }" @click="go('projects')"><i class="ms">folder</i> 项目</button>
            <button class="menu-item" :class="{ on: store.view === 'settings' }" @click="go('settings')"><i class="ms">settings</i> 设置</button>
            <button class="menu-item" :class="{ on: store.view === 'skills' }" @click="go('skills')"><i class="ms">extension</i> 技能</button>
            <button class="menu-item" :class="{ on: store.view === 'usage' }" @click="go('usage')"><i class="ms">analytics</i> 用量</button>
          </div>
          <div class="menu-sep" />
          <div class="menu-nav">
            <button class="menu-item" @click="menuOpen = false; openMobileNewTask()"><i class="ms">add</i> 新建任务</button>
            <button class="menu-item" @click="menuOpen = false; openProjectNew()"><i class="ms">add</i> 新建项目</button>
            <button class="menu-item" @click="ui.connectDlg = true; menuOpen = false"><i class="ms">smartphone</i> 手机连接</button>
            <button class="menu-item" :disabled="scanning" @click="doScan"><i class="ms">play_arrow</i> 立即扫描</button>
            <button class="menu-item" :disabled="updWorking || updating" @click="checkUpdate">
              <i class="ms">update</i> {{ updLabel }}
            </button>
            <button class="menu-item" :disabled="restartingNow" @click="askRestart"><i class="ms">refresh</i> 重启程序</button>
            <button class="menu-item" @click="toggleTheme">
              <i class="ms">{{ themeIcon }}</i> 主题：{{ themeLabel }}
            </button>
            <button v-if="auth.enabled && auth.authed" class="menu-item" @click="onLogout"><i class="ms">logout</i> 退出登录</button>
          </div>
          <div class="menu-ver" v-if="store.state">v{{ store.state.version }}</div>
        </NDrawerContent>
      </NDrawer>
      <!-- 移动端底部导航：看板 / 项目 / ＋新建（呼出项目选择） / 更多 -->
      <nav class="mbottom" v-show="!showLogin && isMobile">
        <button :class="{ on: store.view === 'dash' }" @click="go('dash')">
          <span class="mi"><i class="ms">dashboard</i></span><span class="ml">看板</span>
        </button>
        <button :class="{ on: store.view === 'projects' }" @click="go('projects')">
          <span class="mi"><i class="ms">folder</i></span><span class="ml">项目</span>
        </button>
        <button class="fab" @click="openMobileNewTask" title="新建任务">
          <span class="mi"><i class="ms">add</i></span>
        </button>
        <button @click="menuOpen = true">
          <span class="mi"><i class="ms">menu</i></span><span class="ml">更多</span>
        </button>
      </nav>
      <!-- ＋新建：项目选择抽屉（底部滑入，点项目即呼出新建任务抽屉） -->
      <NDrawer v-model:show="pickTaskOpen" placement="bottom" :height="380">
        <NDrawerContent title="选择项目新建任务" closable>
          <div class="pick-list">
            <button v-for="p in projectsWithoutTasks" :key="p.name" class="pick-item"
                    @click="pickProjectNewTask(p.name)">
              <span class="pick-name"><i class="ms sm">folder</i> {{ p.name }}</span>
              <span class="pick-meta">{{ p.tasks.filter(t => t.status === 'open').length }} 待办 · {{ p.path }}</span>
            </button>
            <button v-for="p in menuProjects.filter(x => x.tasks.some(t => t.status === 'in_progress'))"
                    :key="'r' + p.name" class="pick-item running" @click="pickProjectNewTask(p.name)">
              <span class="pick-name"><i class="ms sm">settings</i> {{ p.name }}（执行中）</span>
              <span class="pick-meta">{{ p.path }}</span>
            </button>
            <div v-if="!menuProjects.length" class="menu-ver">暂无启用的项目，请先在「项目」页新建</div>
          </div>
        </NDrawerContent>
      </NDrawer>
      <!-- 自更新进行中：全屏状态提示（禁止点击；应用重启后本页自动 reload 加载新版页面） -->
      <div v-if="updateOverlayVisible" class="upd-overlay">
        <div class="upd-box">
          <div class="upd-title">应用正常升级中....</div>
          <div class="upd-sub">升级在后台静默进行（全程无 UAC 弹窗），应用将自动重启，
            本页将在升级完成后自动刷新以加载新版本页面（请勿关闭本页）</div>
          <pre v-if="store.state?.update?.install_log_tail" class="upd-log">{{ store.state.update.install_log_tail }}</pre>
        </div>
      </div>
      <!-- 登录层：鉴权开启且未登录时全屏接管（覆盖 App 壳，轮询已静默短路） -->
      <div v-if="showLogin" class="login-overlay">
        <div class="login-box">
          <img class="login-logo" src="./assets/logo.png" alt="Orcsy" draggable="false">
          <div class="login-title">Orcsy 任务编排中心</div>
          <div class="login-sub">看板已开启访问鉴权，请输入登录密码</div>
          <NInput v-model:value="loginPwd" type="password" show-password-on="click"
                  placeholder="登录密码" :disabled="loginBusy"
                  @keyup.enter="submitLogin" />
          <!-- 一键开放手机访问生成的新密码：重启后唯一一次回显，复制登录即清除 -->
          <div v-if="mobilePwdHint" class="login-pwd-hint">
            <span>本次开启的访问密码：<code>{{ mobilePwdHint }}</code>（请保存，可稍后在设置页修改）</span>
            <span class="pwd-hint-ops">
              <NButton size="tiny" @click="navigator.clipboard.writeText(mobilePwdHint).then(() => window.$message?.('已复制密码'), () => window.$message?.('复制失败，请手动抄写'))">复制</NButton>
              <NButton size="tiny" quaternary @click="dismissMobilePwdHint">知道了</NButton>
            </span>
          </div>
          <NCheckbox v-model:checked="loginRemember" size="small" style="margin-top: 10px">
            记住这台设备（30 天免重复登录）
          </NCheckbox>
          <div v-if="loginErr" class="login-err">{{ loginErr }}</div>
          <NButton type="primary" block :loading="loginBusy" :disabled="!loginPwd"
                   style="margin-top: 12px" @click="submitLogin">登 录</NButton>
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
/* 登录层：全屏不透明接管（未登录时不该透出看板骨架），双主题 token 上色 */
.login-overlay { position: fixed; inset: 0; background: var(--bg, #0b0f17); z-index: 3000;
  display: flex; align-items: center; justify-content: center; }
.login-box { background: var(--panel, #131a2a); border: 1px solid var(--line, #253048);
  border-radius: 12px; padding: 30px 34px; width: 340px; max-width: 90vw;
  text-align: center; box-shadow: 0 10px 40px rgba(0, 0, 0, 0.35); }
.login-logo { width: 34px; height: 34px; margin-bottom: 6px; display: block; margin-left: auto; margin-right: auto; }
.login-title { font-size: 17px; font-weight: 700; color: var(--text, #e5eaf6); }
.login-sub { font-size: 12.5px; color: var(--dim, #94a2be); margin: 4px 0 16px; }
.login-err { font-size: 12.5px; color: #e88080; margin-top: 8px; text-align: left; }
/* 一键开放手机访问后重启：登录页回显新生成的访问密码（一次性，登录成功即清除） */
.login-pwd-hint {
  margin-top: 10px; padding: 8px 10px; border-radius: 8px; text-align: left;
  font-size: 12px; color: var(--text, #e5eaf6);
  background: rgba(91, 140, 255, 0.12); border: 1px solid var(--acc, #5b8cff);
  display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
}
.login-pwd-hint code {
  font-family: Consolas, monospace; font-weight: 700; font-size: 13px;
  background: rgba(128, 128, 160, 0.18); border-radius: 4px; padding: 1px 6px;
  user-select: all;
}
.pwd-hint-ops { display: flex; gap: 4px; flex: none; }
/* 双主题 token：Naive 只给自家组件上色，裸 HTML 元素（标题/说明/路径/日志等）
   必须用这里的变量，否则暗色主题下黑字黑底不可见 */
:root[data-theme="dark"]{
  --bg:#0b0f17; --panel:#131a2a; --card:#1a2235; --line:#253048; --line2:#31405f;
  --text:#e5eaf6; --dim:#94a2be; --faint:#64748f;
  /* 用量热力图色阶（蓝系，与主色同源） */
  --hm0:rgba(128,140,180,.14); --hm1:#16305c; --hm2:#1e4a97; --hm3:#3568d4; --hm4:#5b8cff;
}
:root[data-theme="light"]{
  --bg:#f2f4f9; --panel:#ffffff; --card:#f7f8fc; --line:#e4e9f3; --line2:#cbd5ea;
  --text:#1d2637; --dim:#5c6b85; --faint:#8e9bb3;
  --hm0:rgba(29,38,55,.08); --hm1:#cfe0ff; --hm2:#9dc0fb; --hm3:#5f93f7; --hm4:#2f6bff;
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
.brand .logo { width: 30px; height: 30px; flex: none; display: block; border-radius: 6px; }
.brand h1 { margin: 0; font-size: 17px; line-height: 1.3; color: var(--text); }
.brand .sub { font-size: 12px; color: var(--dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.actions { display: flex; gap: 8px; align-items: center; flex: none; flex-wrap: wrap; justify-content: flex-end; }
.navseg { width: 360px; max-width: 52vw; }
@media (max-width: 640px) { .brand .sub { display: none; } .navseg { width: auto; } }

/* ---- 移动端专属布局（≤768px）：极简吸顶顶栏 + 底部导航 + 抽屉菜单 ---- */
.mtop {
  position: sticky; top: 0; z-index: 20;
  display: flex; gap: 10px; align-items: center;
  padding: 8px 12px; padding-top: calc(8px + env(safe-area-inset-top, 0px));
  background: var(--panel); border-bottom: 1px solid var(--line);
}
.mtop .logo { width: 26px; height: 26px; border-radius: 6px; flex: none; }
.mtop .mtitle { font-size: 16px; font-weight: 700; color: var(--text); display: flex; gap: 8px; align-items: baseline; min-width: 0; }
.mtop .msub { font-size: 12.5px; font-weight: 400; color: var(--dim); }
.mtop .mspace { flex: 1; }
.mbtn {
  appearance: none; border: none; background: transparent; color: var(--text);
  font-size: 18px; line-height: 1; padding: 6px 8px; border-radius: 8px; cursor: pointer;
  position: relative;
}
.mbtn:active { background: rgba(128, 128, 160, 0.18); }
.mbtn.bell i {
  position: absolute; top: 0; right: 0; font-style: normal;
  font-size: 10px; background: #e88080; color: #fff; border-radius: 999px;
  padding: 0 4px; line-height: 14px;
}
.mbottom {
  position: fixed; left: 0; right: 0; bottom: 0; z-index: 30;
  display: grid; grid-template-columns: repeat(4, 1fr);
  padding: 6px 8px calc(6px + env(safe-area-inset-bottom, 0px));
  background: var(--panel); border-top: 1px solid var(--line);
}
.mbottom button {
  appearance: none; border: none; background: transparent; cursor: pointer;
  display: flex; flex-direction: column; align-items: center; gap: 2px;
  color: var(--dim); font: inherit; padding: 4px 0;
}
.mbottom button.on { color: var(--acc, #5b8cff); }
.mbottom .mi { font-size: 18px; line-height: 1.2; }
.mbottom .ml { font-size: 11px; }
.mbottom .fab {
  /* 圆心对齐导航栏上缘：出血量 = FAB 半径（52/2） */
  margin: -26px auto 0; width: 52px; height: 52px; border-radius: 50%;
  background: var(--acc, #5b8cff); color: #fff; font-size: 26px;
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 4px 14px rgba(91, 140, 255, 0.45);
}
.mbottom .fab .mi { font-size: 28px; line-height: 1; }
.mbottom .fab .ms { font-size: 30px; margin: 0; }
/* 移动端：正文给底部导航让位（桌面 padding-bottom 不变） */
@media (max-width: 768px) {
  body { padding-bottom: calc(64px + env(safe-area-inset-bottom, 0px)); }
  .top { display: none; }
}
.menu-nav { display: flex; flex-direction: column; gap: 4px; }
.menu-item {
  appearance: none; border: none; background: transparent; cursor: pointer;
  text-align: left; font: inherit; font-size: 14px; color: var(--text);
  padding: 11px 12px; border-radius: 8px;
}
.menu-item:active, .menu-item.on { background: rgba(128, 128, 160, 0.15); }
.menu-item:disabled { opacity: 0.5; cursor: default; }
.menu-sep { height: 1px; background: var(--line); margin: 10px 0; }
.menu-ver { font-size: 11.5px; color: var(--faint); text-align: center; margin-top: 12px; }
.pick-list { display: flex; flex-direction: column; gap: 8px; }
.pick-item {
  appearance: none; cursor: pointer; text-align: left; font: inherit;
  border: 1px solid var(--line); background: var(--card); border-radius: 10px;
  padding: 10px 14px; display: flex; flex-direction: column; gap: 3px;
}
.pick-item:active { border-color: var(--acc, #5b8cff); }
.pick-name { font-size: 14px; font-weight: 600; color: var(--text); }
.pick-meta { font-size: 12px; color: var(--dim); word-break: break-all; }

/* ---- 移动端通用防溢出（≤768px，跨视图 + Naive 组件内部） ----
   各视图自己的 scoped 样式带 data-v 选择器优先级更高，故覆盖项用 !important */
@media (max-width: 768px) {
  /* Naive 卡片头部：标题与右侧操作区换行——修复「技能库」标题被按钮
     行挤成竖排、操作按钮溢出屏幕的问题 */
  .n-card-header { flex-wrap: wrap; row-gap: 6px; justify-content: space-between; }
  /* 标题列禁止收缩：CJK 文本可按字收缩，会被长说明挤成一字一行（用量页等） */
  .n-card .n-card-header .n-card-header__main { flex: 0 0 auto !important; }
  /* 各视图的操作按钮行/选择条允许换行 */
  .head-ops, .sel-bar, .frow, .appr-head, .checkline { flex-wrap: wrap; }
  /* 项目网格：400px 最小列宽在手机上必然横向溢出，改单列 */
  .grid { grid-template-columns: 1fr !important; }
  /* 弹窗表单的双列布局在手机上改单列 */
  .duo { flex-direction: column; gap: 6px; }
  /* 表单双列行（.frow）同样改单列：窄屏 50/50 会把标签挤成竖排 */
  .frow { flex-direction: column; gap: 6px; }
  .frow > div { width: 100%; }
  /* 页面留白与设置页（24px）统一；顶部给吸顶栏留出呼吸空间 */
  .skills, .usage, .projpage { padding: 0 24px !important; }
  .dash, .settings, .skills, .usage, .projpage, .commitpage { padding-top: 14px !important; }
  /* 输入控件字号 ≥16px：防止 iOS 聚焦时页面自动放大（PWA 移动端规范） */
  input, textarea, .n-input__input-el, .n-input__textarea-el { font-size: 16px !important; }
}

/* ---- details/summary 折叠指示：原生三角 → Material chevron（展开旋转 90°）---- */
summary { list-style: none; cursor: pointer; }
summary::-webkit-details-marker { display: none; }
summary::before {
  content: 'chevron_right';
  font-family: 'Material Icons Round';
  font-size: 15px;
  line-height: 1;
  display: inline-block;
  margin-right: 6px;
  vertical-align: -2px;
  color: var(--dim, #64748f);
  transition: transform .15s ease;
  -webkit-font-feature-settings: 'liga';
  font-feature-settings: 'liga';
}
details[open] > summary::before {
  transform: rotate(90deg);
  color: var(--acc, #5b8cff);
}

/* ---- Google Material Icons（自托管 woff2，零外部请求）----
   用法：<i class="ms">icon_name</i>（图标名即连字，如 dashboard / folder） */
@font-face {
  font-family: 'Material Icons Round';
  font-style: normal;
  font-weight: 400;
  src: url(./assets/mi-round.woff2) format('woff2');
}
.ms {
  font-family: 'Material Icons Round';
  font-weight: normal;
  font-style: normal;
  font-size: 20px;
  line-height: 1;
  display: inline-block;
  white-space: nowrap;
  word-wrap: normal;
  direction: ltr;
  letter-spacing: normal;
  text-transform: none;
  vertical-align: middle;
  -webkit-font-feature-settings: 'liga';
  font-feature-settings: 'liga';
  -webkit-font-smoothing: antialiased;
}
.ms.sm { font-size: 16px; }
.ms.lg { font-size: 26px; }
/* 图标与相邻文字的间距：标题/按钮/菜单内生效 */
.n-card-header .ms, .n-collapse-item__header .ms, .checkline .ms,
.menu-item .ms, .pick-name .ms { margin-right: 6px; }
.n-button .ms, .n-tag .ms { margin-right: 4px; font-size: 16px; }

</style>
