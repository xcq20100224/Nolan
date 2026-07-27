import path from "path"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"
import { inspectAttr } from 'kimi-plugin-inspect-react'

// https://vite.dev/config/
export default defineConfig({
  base: './',
  plugins: [inspectAttr(), react()],
  server: {
    // 网页版统一预览端口；host: true 允许局域网访问
    port: 7100,
    host: true,
    // 把 /api 代理到 Python 标准库后端（监听 7101）
    proxy: {
      '/api': 'http://localhost:7101',
    },
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
});
