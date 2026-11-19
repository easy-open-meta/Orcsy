// 全局共享状态：/api/state 数据 + 当前视图 + 弹窗开关（任一弹窗打开时轮询暂停）
import { computed, reactive, ref, shallowRef } from 'vue'
import { api, clearAuthToken, setAuthToken, setUnauthorizedHandler, type AppStateT } from './api'
import { isMobile } from './mobile'

export const store = reactive({
  state: null as AppStateT | null,
  view: 'dash' as 'dash' | 'projects' | 'settings' | 'skills' | 'usage',
})

// 设置页当前展开的功能分区（手风琴 key）；跨视图跳转定位用（openSettingsSection）
export const settingsSection = shallowRef('llm')

// ---- 登录鉴权状态：enabled=配置了登录密码；authed=当前会话有效 ----
// checked=false（启动探测未回）时不渲染登录层，避免闪屏
export const auth = reactive({
  enabled: false,
  authed: true,
  checked: false,
})
// 任何 API 收到 401（会话过期/服务端改密）→ 切回登录层；轮询静默失败不刷屏
setUnauthorizedHandler(() => { if (auth.enabled) auth.authed = false })

/** 启动时探测鉴权状态（/api/auth 在免登录白名单里） */
export async function initAuth(): Promise<void> {
  try {
    const s = await api.authStatus()
    auth.enabled = s.enabled
    auth.authed = s.authed
  } catch { /* 服务不可达：保持默认已登录，交给轮询重试 */ }
  auth.checked = true
}

/** 登录成功后拉全量状态并恢复轮询。
 * remember=true 签发 30 天长会话（手机远程访问息屏/切网频繁，短会话易掉登录）；
 * token 随响应体下发，持久化后以 Bearer 头双轨鉴权（Cookie 丢失时的第二凭证）。 */
export async function doLogin(password: string, remember = false): Promise<void> {
  const r = await api.login(password, remember)  // 失败抛出后端 message（如「密码错误」）
  // 勾选「记住这台设备」→ Bearer 存 localStorage（跨浏览器会话）；
  // 未勾选 → sessionStorage（随标签页结束消失，与未记住语义一致）
  setAuthToken(r.token || '', remember)
  auth.authed = true
  await refresh(true)
}

export async function doLogout(): Promise<void> {
  try { await api.logout() } catch { /* 服务端注销失败也照常退出界面 */ }
  clearAuthToken()  // 本地 Bearer 凭证一并清除（两种存储）
  auth.authed = false
  store.state = null
}

/** 未登录时所有刷新静默跳过（登录层接管，不打扰、不报错） */
export function needLogin(): boolean {
  return auth.checked && auth.enabled && !auth.authed
}

// 弹窗 UI 状态：null=关闭；browserPick 是选择器选定后的回写回调；
// browserMode='dir' 选目录（点=进入，按钮确认），'file' 选文件（点文件即确认）
export const ui = reactive({
  projectDlg: null as null | { original: string | null },
  // taskDlg：id=null 新建；readonly=true 只读查看（归档/非待办任务）
  taskDlg: null as null | { project: string; id: string | null; readonly?: boolean },
  guideDlg: null as null | { project: string },
  skillDlg: null as null | { original: string | null },
  skillScanDlg: false,
  browserDlg: false,
  browserPick: null as ((path: string) => void) | null,
  browserStart: '',
  browserMode: 'dir' as 'dir' | 'file',
  // 自更新重启窗口：触发后页面显示全屏进度，应用回来即自动 reload 并核对版本
  restarting: false,
  // 审批面板：故意不进 anyModal——面板打开时轮询必须继续，
  // pending 列表（store.state.approvals）才能实时刷新、处理后即时消失
  approvalDlg: false,
  // 拖拽排序进行中的任务 id：期间暂停轮询，避免 5s 刷新把拖拽中的列表冲掉
  dragTask: null as string | null,
  // 「手机连接」向导弹窗：局域网地址 + 扫码二维码（桌面顶栏与移动抽屉菜单入口）
  connectDlg: false,
  // 首次使用引导动画（Onboard.vue）：首启自动弹出；顶栏/抽屉「使用引导」可重看
  onboard: false,
})

// ---- 自更新收尾：记录目标版本，应用回归后核对并明确提示成功 ----
const PENDING_KEY = 'orcsyPendingUpdate'
export function markPendingUpdate(target: string): void {
  localStorage.setItem(PENDING_KEY, JSON.stringify({ target, at: Date.now() }))
}
export function getPendingUpdate(): { target: string; at: number } | null {
  try {
    const raw = localStorage.getItem(PENDING_KEY)
    const p = raw ? JSON.parse(raw) as { target: string; at: number } : null
    // 超过 10 分钟的挂单作废（更新早就该完成或已失败）
    return p && Date.now() - p.at < 10 * 60 * 1000 ? p : null
  } catch { return null }
}
export function clearPendingUpdate(): void {
  localStorage.removeItem(PENDING_KEY)
}

// 自更新后等待应用回归：每 2 秒探测一次 /api/auth（免登录白名单，重启后
// 会话失效也能探活），一上线就整页 reload（reload 同时换上新版本的前端页面，
// 版本号/新界面即刻生效；开启鉴权时 reload 后落在登录层属预期）。
// 探测持续 5 分钟不放弃（安装+重启可能超过 90 秒）；遮罩 90 秒后自动撤除
// （不阻塞页面正常轮询与交互），探测成功仍会自动 reload
export function beginRestartWait(target?: string): void {
  if (target) markPendingUpdate(target)
  if (ui.restarting) return
  ui.restarting = true
  window.setTimeout(() => { ui.restarting = false }, 90000) // 遮罩 90 秒撤除，探测继续
  const probe = window.setInterval(() => {
    fetch('/api/auth').then(r => {
      if (r.ok) { window.clearInterval(probe); window.location.reload() }
    }).catch(() => { /* 应用还没回来，继续等 */ })
  }, 2000)
}

export const anyModal = computed(() =>
  !!(ui.projectDlg || ui.taskDlg || ui.guideDlg || ui.skillDlg || ui.skillScanDlg
    || ui.browserDlg))

export async function refresh(force = false): Promise<void> {
  if (needLogin()) return  // 未登录：静默跳过（401 已由全局回调处理）
  if (!force && (anyModal.value || ui.dragTask)) return  // 弹窗/拖拽中不覆盖列表
  try {
    // 移动端蜂窝网络走轻量摘要（省略 60KB 日志尾部与归档）；桌面全量
    store.state = await api.state(isMobile.value)
  } catch { /* 服务瞬时不可达时保持旧数据 */ }
}

let _navigateHandler: ((name: string, query?: Record<string, string>) => void) | null = null

/** 归档列表懒加载：移动端摘要模式（archived 为空但 count>0）展开折叠条时按需拉取。
 * 结果写回 store.state.projects 对应条目，随后轮询的旧快照不会覆盖（refresh 只整包替换）。 */
export async function loadArchived(projectName: string): Promise<void> {
  const p = store.state?.projects.find(x => x.name === projectName)
  if (!p) return
  try {
    const r = await api.projectArchived(projectName)
    if (r.ok) p.archived = r.archived
  } catch { /* 拉取失败静默：折叠条保持「点此重试」态 */ }
}

export function setNavigateHandler(fn: (name: string, query?: Record<string, string>) => void): void {
  _navigateHandler = fn
}

export function navigateTo(name: typeof store.view, query?: Record<string, string>): void {
  store.view = name
  _navigateHandler?.(name, query)
}

/** 跳转到非 Tab 的子页面（如人工把关提交页 /commit）：不改动顶部 Tab 高亮 */
export function navigateToRoute(name: string, query?: Record<string, string>): void {
  _navigateHandler?.(name, query)
}

// 弹窗辅助开启函数（组件任意处调用）
export function openProjectNew(): void {
  navigateTo('projects')
  ui.projectDlg = { original: null }
}
export function openProjectEdit(name: string): void {
  ui.projectDlg = { original: name }
}
export function openTaskNew(project: string): void {
  ui.taskDlg = { project, id: null }
}
export function openTaskView(project: string, id: string): void {
  ui.taskDlg = { project, id, readonly: true }
}
// 打开设置页并定位到指定功能分区（手风琴展开 + 右侧渲染）；缺省定位到第一个分区
export function openSettingsSection(section?: string): void {
  if (section) settingsSection.value = section
  navigateTo('settings', section ? { section } : undefined)
}
export function openGuide(project: string): void {
  ui.guideDlg = { project }
}
export function openCommitDlg(project: string): void {
  // 独立「人工把关提交」页：左侧改动文件清单 + 右侧 diff 对比（非弹窗）
  navigateToRoute('commit', { project })
}
export function openSkillNew(): void {
  navigateTo('skills')
  ui.skillDlg = { original: null }
}
export function openSkillEdit(name: string): void {
  ui.skillDlg = { original: name }
}
export function openSkillScan(): void {
  ui.skillScanDlg = true
}
export function openDirBrowser(startPath: string, onPick: (path: string) => void,
                               mode: 'dir' | 'file' = 'dir'): void {
  ui.browserStart = startPath
  ui.browserPick = onPick
  ui.browserMode = mode
  ui.browserDlg = true
}
