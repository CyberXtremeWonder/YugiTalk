import { defineConfig } from "@playwright/test";
import previous from "./playwright.stage3.config";
export default defineConfig({
  ...previous,
  testMatch: "**/stage{2,3,4}/*.spec.ts",
  outputDir: "../.cache/playwright-stage4",
  reporter: [
    ["list"],
    ["json", { outputFile: "../docs/stages/artifacts/stage4-ui-results.json" }],
  ],
});
