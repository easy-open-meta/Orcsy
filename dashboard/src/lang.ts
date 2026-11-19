// 语言切换内核：简中（默认）/ 繁中 / 英文。
// 与 theme.ts 同款模式：localStorage 持久化 + index.html 早期脚本防闪烁。
// 不引入 vue-i18n：key 直接用简中原文（zh 原样回退），翻译表只补 zhTW/en，
// 现有简中文案零改动即可工作，漏翻条目自动回退简中。
import { computed, ref } from 'vue'

export type Lang = 'zh' | 'zhTW' | 'en'

const KEY = 'ta-lang'

const LOCALE_ATTR: Record<Lang, string> = {
  zh: 'zh-CN',
  zhTW: 'zh-TW',
  en: 'en',
}

export const LANG_LABEL: Record<Lang, string> = {
  zh: '简体中文',
  zhTW: '繁體中文',
  en: 'English',
}

const TITLES: Record<Lang, string> = {
  zh: 'Orcsy 任务编排中心',
  zhTW: 'Orcsy 任務編排中心',
  en: 'Orcsy Task Orchestration',
}

const DESCRIPTIONS: Record<Lang, string> = {
  zh: 'Orcsy 任务编排中心：Agent 按优先级自动完成功能开发与 bug 修复，移动端可远程查看、编辑与新建任务。',
  zhTW: 'Orcsy 任務編排中心：Agent 依優先級自動完成功能開發與 bug 修復，行動端可遠端查看、編輯與新建任務。',
  en: 'Orcsy task orchestration: agents complete feature work and bug fixes by priority; manage tasks remotely from mobile.',
}

// 翻译表由 locales/*.ts 注入（避免 lang.ts 与语言包循环依赖）
const packs: Record<Exclude<Lang, 'zh'>, Record<string, string>> = {
  zhTW: {},
  en: {},
}

export function registerPack(name: Exclude<Lang, 'zh'>, pack: Record<string, string>): void {
  packs[name] = pack
}

function detectBrowserLang(): Lang {
  const langs = Array.isArray(navigator.languages) && navigator.languages.length
    ? navigator.languages
    : [navigator.language || '']
  for (const raw of langs) {
    const l = (raw || '').toLowerCase()
    if (l.startsWith('zh-tw') || l.startsWith('zh-hant') || l.startsWith('zh-hk')
        || l.startsWith('zh-mo')) return 'zhTW'
    if (l.startsWith('zh')) return 'zh'
    if (l.startsWith('en')) return 'en'
  }
  return 'zh'
}

export const lang = ref<Lang>((() => {
  try {
    const v = localStorage.getItem(KEY)
    if (v === 'zh' || v === 'zhTW' || v === 'en') return v
  } catch { /* 隐私模式忽略 */ }
  return detectBrowserLang()
})())

function apply(): void {
  document.documentElement.lang = LOCALE_ATTR[lang.value]
  document.title = TITLES[lang.value]
  document.querySelector('meta[name="description"]')
    ?.setAttribute('content', DESCRIPTIONS[lang.value])
}

export function setLang(l: Lang): void {
  lang.value = l
  apply()
  try { localStorage.setItem(KEY, l) } catch { /* 隐私模式忽略 */ }
}

const CYCLE: Lang[] = ['zh', 'zhTW', 'en']

export function toggleLang(): void {
  const i = CYCLE.indexOf(lang.value)
  setLang(CYCLE[(i + 1) % CYCLE.length])
}

// 当前翻译函数（script 内一次性取值，如 window.$message 文案）
export function tNow(s: string): string {
  if (lang.value === 'zh') return s
  return packs[lang.value][s] ?? s
}

// 带占位符翻译：语言包里写「删除 {n} 个任务？」，调用 tFmt('删除 ${n} 个任务？', {n: 3})
// key 仍用简中原文模板；zh 直接做占位符替换。
export function tFmt(s: string, params: Record<string, string | number>): string {
  let out = tNow(s)
  for (const [k, v] of Object.entries(params)) out = out.split(`{${k}}`).join(String(v))
  return out
}

// template 内使用：t('待办') 返回 computed
export function t(s: string) {
  return computed(() => tNow(s))
}

export const langLabel = computed(() => LANG_LABEL[lang.value])

// 路由标题（document.title 用）：router meta.title 存简中原文，此处按 lang 出译文
export function routeTitle(key: string): string {
  return tNow(key)
}

apply()
