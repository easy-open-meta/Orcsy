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
})
