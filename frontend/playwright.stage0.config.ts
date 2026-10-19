import { defineConfig } from "@playwright/test";

// 使用独立浏览器测试上下文，不读取个人 Edge 配置或已登录会话。
export default defineConfig({
  testDir: "./tests/stage0",
  workers: 1,
  reporter: [
    ["list"],
    ["json", { outputFile: "../docs/stages/artifacts/ui-test-results.json" }],
  ],
  outputDir: "../.cache/playwright-stage0",
  use: {
    baseURL: "http://127.0.0.1:4170",
    channel: "msedge",
    headless: true,
  },
});
