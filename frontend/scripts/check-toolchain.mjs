/** 阶段 0 导入验证：检查已安装的依赖，不代表前端应用已经实现。 */
import assert from "node:assert/strict";
import React from "react";
import { createServer } from "vite";
import ts from "typescript";
import * as Dialog from "@radix-ui/react-dialog";

assert.equal(typeof React.createElement, "function");
assert.equal(typeof createServer, "function");
assert.equal(typeof ts.transpileModule, "function");
assert.ok(Dialog.Root);
console.log(
  `PASS: React ${React.version}, TypeScript ${ts.version}, Vite and Radix imports`,
);
