import path from "path"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"
import { inspectAttr } from 'kimi-plugin-inspect-react'

// https://vite.dev/config/
export default defineConfig({
  base: './',
  plugins: [inspectAttr(), react()],
  server: {
    // Kimi 预览认准 7100（实测不重映射、不透传 --port），必须固定 7100。
    // strictPort: 若 7100 被 Windows 排除段锁死（EACCES/10013），
    // 宁可大声失败也不悄悄挪端口——挪了 Kimi 照样探测 7100 扑空，故障更难查。
    // host: true 允许局域网访问。
    port: 7100,
    strictPort: true,
    host: true,
    // 把 /api 代理到 Python 标准库后端（监听 7901）
    proxy: {
      '/api': 'http://localhost:7901',
    },
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
});
