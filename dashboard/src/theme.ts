// 现有双主题 CSS token → Naive UI 主题映射（视觉延续 0.3 的深蓝暗色 / 浅色系）
import { darkTheme, lightTheme, type GlobalTheme, type GlobalThemeOverrides } from 'naive-ui'
import { computed, ref } from 'vue'

export type ThemeName = 'dark' | 'light' | 'system'

const KEY = 'ta-theme'
const systemDark = window.matchMedia('(prefers-color-scheme: dark)')

// 主题模式：'system' = 跟随系统外观（浏览器设置变化实时跟随）。
// index.html 的早期脚本已按同样规则把 data-theme 置成生效值（防闪屏）
export const theme = ref<ThemeName>((() => {
  try {
    const v = localStorage.getItem(KEY)
    if (v === 'light' || v === 'dark' || v === 'system') return v
  } catch { /* 隐私模式忽略 */ }
  return 'system'
})())

function effective(): 'dark' | 'light' {
  if (theme.value === 'system') return systemDark.matches ? 'dark' : 'light'
  return theme.value
}

function apply(): void {
  const e = effective()
  document.documentElement.dataset.theme = e
  // PWA：浏览器 UI 主题色跟随（地址栏/状态栏着色）
  document.querySelector('meta[name="theme-color"]')
    ?.setAttribute('content', e === 'dark' ? '#0b0f17' : '#f2f4f9')
}

const CYCLE: ThemeName[] = ['dark', 'light', 'system']

export function toggleTheme(): void {
  const i = CYCLE.indexOf(theme.value)
  theme.value = CYCLE[(i + 1) % CYCLE.length]
  apply()
  try { localStorage.setItem(KEY, theme.value) } catch { /* 隐私模式忽略 */ }
}

export const themeIcon = computed(() =>
  theme.value === 'system' ? 'brightness_auto'
    : theme.value === 'dark' ? 'dark_mode' : 'light_mode')
export const themeLabel = computed(() =>
  theme.value === 'system' ? '跟随系统'
    : theme.value === 'dark' ? '深色' : '浅色')

apply()
systemDark.addEventListener('change', () => { if (theme.value === 'system') apply() })

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
