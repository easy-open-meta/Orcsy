/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}

// useMessage/useDialog 在 Dialogs.vue 中桥接到 window，供全局调用
declare global {
  interface Window {
    $message?: (content: string, type?: 'success' | 'warning' | 'error' | 'info') => void
    $dialog?: any
  }
}
