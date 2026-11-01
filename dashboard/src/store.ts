// 全局共享状态：/api/state 数据 + 当前视图 + 弹窗开关（任一弹窗打开时轮询暂停）
import { computed, reactive } from 'vue'
import { api, type AppStateT } from './api'

export const store = reactive({
  state: null as AppStateT | null,
  view: 'dash' as 'dash' | 'settings' | 'skills',
})

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

// 自更新后等待应用回归：每 2 秒探测一次 /api/state，一上线就整页 reload
// （reload 同时换上新版本的前端页面，版本号/新界面即刻生效）。
// 探测持续 5 分钟不放弃（安装+重启可能超过 90 秒）；遮罩 90 秒后自动撤除
// （不阻塞页面正常轮询与交互），探测成功仍会自动 reload
export function beginRestartWait(target?: string): void {
  if (target) markPendingUpdate(target)
  if (ui.restarting) return
  ui.restarting = true
  window.setTimeout(() => { ui.restarting = false }, 90000) // 遮罩 90 秒撤除，探测继续
  const probe = window.setInterval(() => {
    fetch('/api/state').then(r => {
      if (r.ok) { window.clearInterval(probe); window.location.reload() }
    }).catch(() => { /* 应用还没回来，继续等 */ })
  }, 2000)
}

export const anyModal = computed(() =>
  !!(ui.projectDlg || ui.taskDlg || ui.guideDlg || ui.skillDlg || ui.skillScanDlg
    || ui.browserDlg))

export async function refresh(force = false): Promise<void> {
  if (!force && anyModal.value) return
  try { store.state = await api.state() } catch { /* 服务瞬时不可达时保持旧数据 */ }
}

// 弹窗辅助开启函数（组件任意处调用）
export function openProjectNew(): void {
  store.view = 'settings'
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
export function openGuide(project: string): void {
  ui.guideDlg = { project }
}
export function openSkillNew(): void {
  store.view = 'skills'
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
