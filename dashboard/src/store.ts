// 全局共享状态：/api/state 数据 + 当前视图 + 弹窗开关（任一弹窗打开时轮询暂停）
import { computed, reactive } from 'vue'
import { api, type AppStateT } from './api'

export const store = reactive({
  state: null as AppStateT | null,
  view: 'dash' as 'dash' | 'settings' | 'skills',
})

// 弹窗 UI 状态：null=关闭；browserPick 是目录选择器选定后的回写回调
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
})

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
export function openDirBrowser(startPath: string, onPick: (path: string) => void): void {
  ui.browserStart = startPath
  ui.browserPick = onPick
  ui.browserDlg = true
}
