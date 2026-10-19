import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/stage6",
  workers: 1,
  outputDir: "../.cache/playwright-stage6",
  reporter: [
    ["list"],
    ["json", { outputFile: "../docs/stages/artifacts/stage6-ui-results.json" }],
  ],
  use: {
    baseURL: "https://localhost:18443",
    channel: process.platform === "win32" ? "msedge" : undefined,
    // 隔离环境采用短期自签证书，HTTP协议验收另行通过明确CA验证。
    ignoreHTTPSErrors: true,
    headless: true,
  },
});
