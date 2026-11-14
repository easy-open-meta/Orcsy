import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { viteSingleFile } from 'vite-plugin-singlefile'
import Components from 'unplugin-vue-components/vite'
import { NaiveUiResolver } from 'unplugin-vue-components/resolvers'

// singlefile：JS/CSS 全内联进一个 index.html，供 build/gen_page.py 转成
// dashboard_html.py 编译进 exe（零外部请求、零数据文件，规避 _MEI 坑）
export default defineConfig({
  plugins: [
    vue(),
    Components({ resolvers: [NaiveUiResolver()] }),
    viteSingleFile(),
  ],
  // 本机调试：npm run dev 起 5173，API/静态资源代理到本机 5116 的实例——
  // 移动端仿真迭代样式时免于每轮 build + 重启（viteSingleFile 只影响 build）
  build: {
    // Material Icons woff2（~174KB）内联进单文件，保持零外部请求
    assetsInlineLimit: 400000,
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:5116',
      '/manifest.webmanifest': 'http://127.0.0.1:5116',
      '/appicon.png': 'http://127.0.0.1:5116',
      '/sw.js': 'http://127.0.0.1:5116',
    },
  },
})
