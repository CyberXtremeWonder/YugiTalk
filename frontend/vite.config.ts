import { defineConfig } from "vite";

// 代理目标仅在服务端读取，不暴露到浏览器构建产物。
export default defineConfig({
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/health": process.env.API_PROXY_TARGET || "http://127.0.0.1:8000",
      "/api": process.env.API_PROXY_TARGET || "http://127.0.0.1:8000",
    },
  },
  preview: { host: "127.0.0.1", port: 4173, strictPort: true },
});
