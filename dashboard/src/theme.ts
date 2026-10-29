// 现有双主题 CSS token → Naive UI 主题映射（视觉延续 0.3 的深蓝暗色 / 浅色系）
import { darkTheme, lightTheme, type GlobalTheme, type GlobalThemeOverrides } from 'naive-ui'
import { computed, ref } from 'vue'

export type ThemeName = 'dark' | 'light'

export const theme = ref<ThemeName>(
  (document.documentElement.dataset.theme as ThemeName) || 'dark')

export function toggleTheme(): void {
  const next: ThemeName = theme.value === 'dark' ? 'light' : 'dark'
  theme.value = next
  document.documentElement.dataset.theme = next
  try { localStorage.setItem('ta-theme', next) } catch { /* 隐私模式忽略 */ }
}

const DARK: GlobalThemeOverrides = {
  common: {
    bodyColor: '#0b0f17',
    cardColor: '#131a2a',
    modalColor: '#131a2a',
    popoverColor: '#1a2235',
    inputColor: '#1a2235',
    tableColor: '#131a2a',
    borderColor: '#31405f',
    dividerColor: '#253048',
    textColorBase: '#e5eaf6',
    textColor1: '#e5eaf6',
    textColor2: '#aab6d0',
    textColor3: '#64748f',
    primaryColor: '#5b8cff',
    primaryColorHover: '#7aa3ff',
    primaryColorPressed: '#3f6ef0',
    primaryColorSuppl: '#5b8cff',
    successColor: '#3ecf8e',
    warningColor: '#e8a13c',
    errorColor: '#e05c5c',
    infoColor: '#5b8cff',
    borderRadius: '8px',
  },
}

const LIGHT: GlobalThemeOverrides = {
  common: {
    bodyColor: '#f2f4f9',
    cardColor: '#ffffff',
    modalColor: '#ffffff',
    popoverColor: '#ffffff',
    inputColor: '#f7f8fc',
    borderColor: '#cbd5ea',
    dividerColor: '#e4e9f3',
    textColorBase: '#1d2637',
    textColor1: '#1d2637',
    textColor2: '#44526e',
    textColor3: '#8e9bb3',
    primaryColor: '#3b6ef5',
    primaryColorHover: '#5c85ff',
    primaryColorPressed: '#2f5ce0',
    primaryColorSuppl: '#3b6ef5',
    successColor: '#0e9f6e',
    warningColor: '#a8690e',
    errorColor: '#d23f3f',
    infoColor: '#3b6ef5',
    borderRadius: '8px',
  },
}

export const naiveTheme = computed<GlobalTheme>(() => (theme.value === 'dark' ? darkTheme : lightTheme))
export const themeOverrides = computed<GlobalThemeOverrides>(() => (theme.value === 'dark' ? DARK : LIGHT))

// 任务状态 → n-tag 类型映射
export const ST_NAME: Record<string, string> = {
  open: '待办', in_progress: '执行中', done: '已完成', blocked: '受阻', cancelled: '已取消',
}
export const ST_TYPE: Record<string, 'default' | 'info' | 'success' | 'warning' | 'error'> = {
  open: 'info', in_progress: 'warning', done: 'success', blocked: 'error', cancelled: 'default',
}
// P0~P4 优先级色（与旧版 pri.P0~P4 一致）
export const PRI_COLOR: Record<string, string> = {
  P0: '#e05c5c', P1: '#ef7d45', P2: '#e8a13c', P3: '#5b8cff', P4: '#64748f',
}
