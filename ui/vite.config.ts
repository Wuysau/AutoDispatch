import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'

const apiProxy = {
  '/api/autodispatch': { target: 'http://127.0.0.1:8765', changeOrigin: false },
}

/** 仅在生产构建时移除 impeccable live 助手注入的脚本，避免把本地调试工具打进 dist。 */
function stripLiveHelper(): Plugin {
  return {
    name: 'strip-live-helper',
    apply: 'build',
    transformIndexHtml(html) {
      return html.replace(/<!--\s*impeccable-live-start[\s\S]*?impeccable-live-end\s*-->/g, '')
    },
  }
}

export default defineConfig({
  plugins: [react(), stripLiveHelper()],
  server: { host: '0.0.0.0', port: 5173, proxy: apiProxy },
  preview: { host: '0.0.0.0', port: 5173, strictPort: true, proxy: apiProxy },
})
