# 我的记忆（C 端面板）

Vue 3 + Vite + TypeScript。部署路径：`/memory/ui/`。只调用后端 `/v1/me/*`。

## 本地开发

```bash
npm ci
npm run dev          # http://127.0.0.1:5173/memory/ui/?user_id=alice
npm test             # Vitest
npm run build
npx playwright install chromium && npm run test:e2e   # Playwright（mock API）
```

身份：URL 带 `?user_id=` 会预填并写入 `localStorage`；也可以在页面输入框填写 `user_id` 后点「使用」。

环境变量：`VITE_API_BASE`（默认 `/memory`）。
