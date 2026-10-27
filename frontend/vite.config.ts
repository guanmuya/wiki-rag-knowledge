import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 桌面应用：构建产物用相对路径加载（Electron 用 file:// 打开），
// 因此 base 必须是 './'，否则打包后资源 404。
export default defineConfig({
  plugins: [react()],
  base: './',
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      // 仅浏览器开发模式使用；桌面模式下前端直接访问 sidecar 的绝对地址
      '/api': {
        target: process.env.WRK_API_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
})
