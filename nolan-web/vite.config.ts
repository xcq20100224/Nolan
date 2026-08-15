import path from "path"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"
import { inspectAttr } from 'kimi-plugin-inspect-react'

// https://vite.dev/config/
export default defineConfig({
  base: './',
  plugins: [inspectAttr(), react()],
  server: {
    // 网页版统一预览端口；host: true 允许局域网访问。
    // 注意：Windows 排除端口段 5573~7631 几乎连续（netsh excludedportrange 实测），
    // 7000 系全部不可用（EACCES），故固定用 8600；CLI --port 传入时优先于本值
    port: 8600,
    strictPort: false,
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
