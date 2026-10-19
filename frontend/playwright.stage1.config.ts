import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/stage1",
  workers: 1,
  outputDir: "../.cache/playwright-stage1",
  reporter: [
    ["list"],
    ["json", { outputFile: "../docs/stages/artifacts/stage1-ui-results.json" }],
  ],
  use: {
    baseURL: process.env.E2E_BASE_URL || "http://127.0.0.1:5173",
    channel: process.platform === "win32" ? "msedge" : undefined,
    headless: true,
  },
});
