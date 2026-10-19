import { defineConfig } from "@playwright/test";
import previous from "./playwright.stage2.config";
export default defineConfig({
  ...previous,
  testDir: "./tests",
  testMatch: "**/stage{2,3}/*.spec.ts",
  outputDir: "../.cache/playwright-stage3",
  reporter: [
    ["list"],
    ["json", { outputFile: "../docs/stages/artifacts/stage3-ui-results.json" }],
  ],
});
