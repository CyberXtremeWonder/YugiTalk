import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/monitoring",
  workers: 1,
  outputDir: "../.cache/playwright-monitoring",
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:4173",
    channel: process.platform === "win32" ? "msedge" : undefined,
    headless: true,
  },
  webServer: {
    command: "npm run preview",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: true,
  },
});
