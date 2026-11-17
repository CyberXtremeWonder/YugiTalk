// 知识图谱和整站主题的隔离浏览器验收，不访问业务数据库或模型。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";
import { createServer } from "../frontend/node_modules/vite/dist/node/index.js";
import { chromium } from "../frontend/node_modules/playwright/index.mjs";

const frontend = fileURLToPath(new URL("../frontend", import.meta.url));
const directory = resolve(frontend, ".cache/knowledge-graph-ui");
await mkdir(directory, { recursive: true });
const tuples = [
  ["RAG", "知识库", "使用"], ["RAG", "混合检索", "使用"],
  ["RAG", "BGE-M3", "使用"], ["RAG", "分块策略", "依赖"],
  ["RAG", "MIRACL", "评估"], ["RAG", "原文证据", "引用"],
  ["混合检索", "向量检索", "包含"], ["混合检索", "关键词检索", "包含"],
  ["混合检索", "RRF", "使用"], ["向量检索", "Milvus", "依赖"],
  ["RRF", "候选结果", "融合"], ["候选结果", "重排序", "使用"],
  ["知识库", "技术文档", "包含"], ["知识库", "Milvus", "存储"],
  ["分块策略", "父块扩展", "包含"], ["原文证据", "evidence_id", "标识"],
  ["MIRACL", "Recall@5", "评估"], ["重排序", "NDCG@10", "评估"],
];
const facts = tuples.map(([source, target, relation], i) => ({
  source, target, relation, quote: `${source}通过「${relation}」关联${target}，这段原文用于核对关系依据。`, chunk_id: `chunk-${i}`,
}));
// 密集示例覆盖交叉连接、重复证据、双向关系、自环和长实体名称。
const denseEntities = [
  "知识库", "文档解析", "Markdown 解析", "PDF 解析", "表格解析", "OCR",
  "分块策略", "父块扩展", "段落索引", "向量检索", "关键词检索", "混合检索",
  "BGE-M3", "Milvus", "PostgreSQL", "对象存储", "RRF", "候选结果",
  "重排序", "查询改写", "实体抽取", "关系抽取", "知识图谱", "证据回查",
  "访问权限", "文档版本", "evidence_id", "回答生成", "引用核对", "检索评估",
  "Recall@5", "NDCG@10", "索引任务", "任务恢复", "可观测性", "跨知识库访问权限校验服务",
];
const denseTuples = denseEntities.flatMap((name, i) => [
  [name, denseEntities[(i + 1) % denseEntities.length], "依赖"],
  [name, denseEntities[(i + 7) % denseEntities.length], "关联"],
]);
denseTuples.push(
  ...denseEntities.slice(1, 13).map(name => [denseEntities[0], name, "连接"]),
  denseTuples[0],
  [denseTuples[0][1], denseTuples[0][0], denseTuples[0][2]],
  [denseEntities[0], denseEntities[0], "更新"],
);
const denseFacts = denseTuples.map(([source, target, relation], i) => ({
  source, target, relation, quote: `密集布局示例：${source}通过「${relation}」关联${target}。`, chunk_id: `dense-${i}`,
}));
const denseEdges = [...new Map(denseFacts.map(fact => [JSON.stringify([fact.source, fact.relation, fact.target]), fact])).values()];
const server = await createServer({ root: frontend, configFile: false, server: { host: "127.0.0.1", port: 15177, strictPort: true } });
let browser;
try {
  await server.listen();
  browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1100 } });
  const errors = [], sources = [];
  let forbidden = false;
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/api/v1/**", route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    let json = { items: [], next_cursor: null };
    if (path.endsWith("/auth/me")) json = { user: { id: "user", name: "测试用户", platform_admin: false }, csrf_token: "test" };
    if (path.endsWith("/me/knowledge-bases")) json = { items: [{ id: "base", name: "项目技术文档" }], next_cursor: null };
    if (path.endsWith("/base/documents")) json = { items: [
      { id: "doc", filename: "retrieval.md", size: 2048, status: "ready", index_version: 2 },
      { id: "empty", filename: "未构建资料.pdf", size: 1024, status: "ready", index_version: 1 },
      { id: "dense", filename: "密集关系示例.md", size: 8192, status: "ready", index_version: 3 },
    ], next_cursor: null };
    if (path.endsWith("/doc/graph")) json = { status: "ready", index_version: 2, entity_count: 19, facts, mode: "model" };
    if (path.endsWith("/empty/graph")) json = { status: "unavailable", facts: [] };
    if (path.endsWith("/dense/graph")) json = { status: "ready", index_version: 3, entity_count: denseEntities.length, facts: denseFacts, mode: "model" };
    if (path.includes("/sources/")) {
      sources.push(url.searchParams.get("index_version"));
      if (forbidden) return route.fulfill({ status: 404, json: { detail: "not_found" } });
      json = { filename: "retrieval.md", paragraph: 2, content: "完整原文：检索结果通过 RRF 合并，再由重排序模型筛选。" };
    }
    return route.fulfill({ json });
  });
  await page.goto("http://127.0.0.1:15177/");
  await page.getByRole("button", { name: "知识库", exact: true }).click();
  assert.equal(await page.locator(".km-file-table .graph-panel").count(), 0);
  await page.getByRole("button", { name: "知识图谱", exact: true }).click();
  await page.getByRole("button", { name: "重新构建图谱", exact: true }).waitFor();
  const canvas = page.locator(".graph-canvas");
  await canvas.waitFor();
  assert.equal(await page.locator(".graph-node").count(), new Set(tuples.flatMap(tuple => tuple.slice(0, 2))).size);
  const node = page.getByRole("button", { name: "实体 混合检索", exact: true });
  await node.click();
  assert.equal(await page.locator(".graph-inspector h2").innerText(), "混合检索");
  await page.locator(".graph-evidence button").first().click();
  await page.getByText(/完整原文：检索结果/).waitFor();
  assert.deepEqual(sources, ["2"]);
  forbidden = true;
  await page.locator(".graph-evidence button").first().click();
  await page.getByText("来源已删除、更新或无权访问。").waitFor();
  assert.equal(await page.getByText(/完整原文：检索结果/).count(), 0);
  await page.getByLabel("搜索图谱实体").fill("不存在的实体");
  await page.getByText("没有匹配的实体或关系，请调整搜索和筛选条件。").waitFor();
  await page.getByLabel("搜索图谱实体").fill("");
  await page.getByRole("button", { name: "放大图谱" }).click();
  assert.equal(await page.locator(".graph-zoom span").innerText(), "115%");
  await page.getByRole("button", { name: "重置视图", exact: true }).click();
  const before = await node.getAttribute("transform"), bounds = await node.boundingBox();
  await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + 20);
  await page.mouse.down(); await page.mouse.move(bounds.x + 80, bounds.y + 40, { steps: 5 }); await page.mouse.up();
  assert.notEqual(await node.getAttribute("transform"), before);
  await page.getByRole("button", { name: "重置视图", exact: true }).click();
  await page.getByRole("button", { name: "实体列表", exact: true }).click();
  await page.locator(".graph-entity-table table").waitFor();
  await page.getByRole("button", { name: "关系网络", exact: true }).click();
  const edgeCount = await page.locator(".graph-edge").count();
  await page.locator(".graph-check input").first().uncheck();
  assert.ok(await page.locator(".graph-edge").count() < edgeCount);
  await page.locator(".graph-check input").first().check();
  await page.getByRole("button", { name: "全屏", exact: true }).click();
  assert.equal(await page.evaluate(() => !!document.fullscreenElement), true);
  await page.getByRole("button", { name: "全屏", exact: true }).click();
  assert.equal(await page.evaluate(() => !!document.fullscreenElement), false);
  await node.click();
  await page.screenshot({ path: resolve(directory, "graph-light.png"), fullPage: true, animations: "disabled" });
  await page.getByRole("button", { name: "切换到深色主题" }).click();
  await page.screenshot({ path: resolve(directory, "graph-dark.png"), fullPage: true, animations: "disabled" });
  await page.getByLabel("图谱文档").selectOption("empty");
  await page.getByText("图谱：尚未构建").waitFor();
  assert.equal(await page.locator(".graph-canvas").count(), 0);
  await page.getByLabel("图谱文档").selectOption("doc");
  await canvas.waitFor();
  await page.getByRole("button", { name: "切换到浅色主题" }).click();
  await page.getByLabel("图谱文档").selectOption("dense");
  await page.getByRole("button", { name: `实体 ${denseEntities[0]}`, exact: true }).waitFor();
  assert.equal(await page.locator(".graph-node").count(), denseEntities.length);
  assert.equal(await page.locator(".graph-edge").count(), denseEdges.length);
  assert.equal(await page.locator(".graph-edge:not(.connected) .graph-edge-label").count(), 0);
  const denseSelected = await page.locator(".graph-node.selected").getAttribute("data-node");
  assert.equal(await page.locator(".graph-edge-label").count(), Math.min(12, denseEdges.filter(fact => fact.source === denseSelected || fact.target === denseSelected).length));
  const cards = await page.locator(".graph-node-card").evaluateAll(elements => elements.map(element => {
    const { x, y, width, height } = element.getBoundingClientRect();
    return { x, y, width, height, name: element.parentElement.getAttribute("data-node") };
  }));
  for (let i = 0; i < cards.length; i++) {
    for (let j = i + 1; j < cards.length; j++) {
      const a = cards[i], b = cards[j];
      assert.ok(a.x + a.width <= b.x + 0.1 || b.x + b.width <= a.x + 0.1 || a.y + a.height <= b.y + 0.1 || b.y + b.height <= a.y + 0.1, `密集布局卡片重叠：${a.name} / ${b.name}`);
    }
  }
  const frame = (await canvas.getAttribute("viewBox")).split(" ").map(Number);
  assert.ok(frame.every(Number.isFinite));
  assert.ok(frame[2] > 700 || frame[3] > 560);
  assert.equal(await page.locator(".graph-edge > path").evaluateAll(paths => paths.some(path => /NaN|Infinity/.test(path.getAttribute("d")))), false);
  await page.screenshot({ path: resolve(directory, "graph-dense.png"), fullPage: true, animations: "disabled" });
  await page.locator(".graph-stage-heading").getByRole("button", { name: "聚焦所选实体" }).click();
  const reached = new Set([denseSelected]);
  for (const fact of denseEdges) {
    if (fact.source === denseSelected) reached.add(fact.target);
    if (fact.target === denseSelected) reached.add(fact.source);
  }
  assert.deepEqual(new Set(await page.locator(".graph-node").evaluateAll(nodes => nodes.map(node => node.getAttribute("data-node")))), reached);
  assert.ok(await page.locator(".graph-node").count() < denseEntities.length);
  await page.screenshot({ path: resolve(directory, "graph-focused.png"), fullPage: true, animations: "disabled" });
  await page.locator(".graph-stage-heading").getByRole("button", { name: "显示全部关系" }).click();
  assert.equal(await page.locator(".graph-node").count(), denseEntities.length);
  assert.equal(await page.locator(".graph-edge").count(), denseEdges.length);
  await page.getByRole("button", { name: "放大图谱" }).click();
  assert.equal(await page.locator(".graph-zoom span").innerText(), "115%");
  await page.getByRole("button", { name: "重置视图", exact: true }).click();
  const denseNode = page.locator(".graph-node.selected");
  const denseBefore = await denseNode.getAttribute("transform");
  const denseCard = await denseNode.locator(".graph-node-card").boundingBox();
  await page.mouse.move(denseCard.x + denseCard.width / 2, denseCard.y + denseCard.height / 2);
  await page.mouse.down();
  await page.mouse.move(denseCard.x + denseCard.width / 2 + 35, denseCard.y + denseCard.height / 2 + 20, { steps: 5 });
  await page.mouse.up();
  assert.notEqual(await denseNode.getAttribute("transform"), denseBefore);
  await page.getByLabel("图谱文档").selectOption("doc");
  await canvas.waitFor();
  await page.setViewportSize({ width: 400, height: 850 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.equal(await page.locator(".graph-workspace").evaluate(el => el.scrollWidth <= el.clientWidth), true);
  await page.screenshot({ path: resolve(directory, "graph-mobile.png"), fullPage: true, animations: "disabled" });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, checks: ["real-api-shape", "node-select", "source-version", "source-revocation", "search", "relation-filter", "zoom", "node-drag", "fullscreen", "entity-list", "document-switch", "light-dark", "dense-no-overlap", "dense-deduplication", "dense-labels", "dense-focus", "dense-zoom-drag", "mobile"], screenshots: directory }));
} finally { await browser?.close(); await server.close(); }
