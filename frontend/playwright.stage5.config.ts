import { defineConfig } from "@playwright/test";
import previous from "./playwright.stage4.config";
export default defineConfig({
  ...previous,
  testMatch: "**/stage{2,3,4,5}/*.spec.ts",
  outputDir: "../.cache/playwright-stage5",
  reporter: [
    ["list"],
    ["json", { outputFile: "../docs/stages/artifacts/stage5-ui-results.json" }],
  ],
});
