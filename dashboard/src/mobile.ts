// 移动端适配共享状态：断点感知 + PWA 环境判定（手机远程访问，docs/design-mobile-remote-access.md）。
//
// 断点定义：isMobile = 视口宽 ≤ 768px —— 移动端启用专属布局（吸顶顶栏 / 底部
// 导航 / 全屏任务抽屉），桌面端保留原有布局不变。visualViewport 也纳入监听：
// 软键盘弹起时某些安卓浏览器不改 innerWidth，但会改 visualViewport 宽度。
import { onMounted, onUnmounted, ref } from 'vue'

const MOBILE_BREAKPOINT = 768

export const isMobile = ref(false)
export const viewportHeight = ref(window.innerHeight)

function update(): void {
  const vv = window.visualViewport
  isMobile.value = (vv ? vv.width : window.innerWidth) <= MOBILE_BREAKPOINT
  viewportHeight.value = Math.round(vv ? vv.height : window.innerHeight)
}

// App 壳挂载即全局启用（模块级 ref 单例，多视图共享同一份判定）
let bound = false
export function useMobileViewport(): void {
  onMounted(() => {
    update()
    if (bound) return
    bound = true
    window.addEventListener('resize', update)
    window.visualViewport?.addEventListener('resize', update)
    window.visualViewport?.addEventListener('scroll', update)
  })
  onUnmounted(() => {
    // 全局单例监听不随组件卸载移除：App 壳与页面同生命周期，无需重建
  })
}

/** 当前是否运行在安装为 PWA 的独立窗口里（Android「添加到主屏幕」/ iOS WebClip） */
export function isStandalonePwa(): boolean {
  const nav = navigator as Navigator & {
    standalone?: boolean
  }
  return window.matchMedia('(display-mode: standalone)').matches || nav.standalone === true
}

// ---- 页面可见性感知（弱网低耗）：息屏/切后台暂停轮询（App.vue 定时器已在
// document.hidden 时跳过），回到前台立即拉一次最新状态静默自愈 ----
type VisibilityHandler = () => void
const visibilityHandlers: VisibilityHandler[] = []
let visibilityBound = false

function onVisibilityChange(): void {
  if (document.visibilityState !== 'visible') return  // 息屏由轮询定时器自行跳过
  for (const fn of visibilityHandlers) {
    try { fn() } catch { /* 单个回调失败不影响其它 */ }
  }
}

export function onPageVisible(fn: VisibilityHandler): void {
  visibilityHandlers.push(fn)
  if (!visibilityBound) {
    visibilityBound = true
    document.addEventListener('visibilitychange', onVisibilityChange)
  }
}
