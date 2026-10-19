import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/stage2",
  workers: 1,
  outputDir: "../.cache/playwright-stage2",
  reporter: [
    ["list"],
    ["json", { outputFile: "../docs/stages/artifacts/stage2-ui-results.json" }],
  ],
  use: {
    baseURL: "http://127.0.0.1:18080",
    channel: process.platform === "win32" ? "msedge" : undefined,
    headless: true,
    trace: "retain-on-failure",
  },
});
