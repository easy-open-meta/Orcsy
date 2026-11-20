/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}

// useMessage/useDialog 在 Dialogs.vue 中桥接到 window，供全局调用。
// 本文件是全局脚本（无 import/export），不能用 declare global（会触发 TS2669
// 并让整段增强失效）；直接声明同名接口即可与 lib.dom 的 Window 合并。
interface Window {
  $message?: (content: string, type?: 'success' | 'warning' | 'error' | 'info') => void
  $dialog?: any
}
