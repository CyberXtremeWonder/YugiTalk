import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/steering",
  workers: 1,
  outputDir: "../.cache/playwright-steering",
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:5179",
    channel: process.platform === "win32" ? "msedge" : undefined,
    headless: true,
  },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 5179 --strictPort",
    url: "http://127.0.0.1:5179/tests/steering/harness.html",
    reuseExistingServer: false,
  },
});
