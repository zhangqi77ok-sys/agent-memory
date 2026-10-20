/// <reference types="vitest/config" />
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

const apiProxy = {
  target: 'http://127.0.0.1:8000',
  changeOrigin: true,
  rewrite: (p: string) => p.replace(/^\/memory/, ''),
}

export default defineConfig({
  plugins: [vue()],
  base: '/memory/ui/',
  server: {
    port: 5173,
    // 只代理 API，绝不能匹配 /memory/ui/（否则页面本身被转到后端）
    proxy: {
      '/memory/v1': apiProxy,
      '/memory/healthz': apiProxy,
      '/memory/readyz': apiProxy,
      '/memory/docs': apiProxy,
      '/memory/openapi.json': apiProxy,
    },
  },
  preview: {
    port: 4173,
    // preview 不代理；e2e 用 page.route mock
  },
  test: {
    environment: 'happy-dom',
    include: ['src/**/*.{spec,test}.ts'],
  },
})
