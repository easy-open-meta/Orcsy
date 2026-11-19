import { createApp } from 'vue'
import App from './App.vue'
import { router } from './router'
import { registerPack } from './lang'
import en from './locales/en'
import zhTW from './locales/zhTW'

// 语言包在挂载前注册：首帧即按 localStorage/浏览器语言渲染，无简中闪现
registerPack('en', en)
registerPack('zhTW', zhTW)

const app = createApp(App)
app.use(router)
app.mount('#app')

// PWA：注册 Service Worker（可安装性 + 部署重启窗口的离线回退页）
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js')
      .catch((e) => console.warn('[orcsy] Service Worker 注册失败（不影响功能）：', e))
  })
}
