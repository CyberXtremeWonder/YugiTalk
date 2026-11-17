import { expect, test } from "vitest";
import {
  graphBounds,
  graphNodes,
  layoutGraph,
  nodeWidth,
  visibleFacts,
  type GraphFact,
} from "./graph";

const facts: GraphFact[] = [
  {
    source: "RAG",
    target: "检索",
    relation: "使用",
    quote: "原文一",
    chunk_id: "1",
  },
  {
    source: "检索",
    target: "向量",
    relation: "包含",
    quote: "原文二",
    chunk_id: "2",
  },
  {
    source: "向量",
    target: "索引",
    relation: "依赖",
    quote: "原文三",
    chunk_id: "3",
  },
  {
    source: "孤立分支",
    target: "其他",
    relation: "使用",
    quote: "原文四",
    chunk_id: "4",
  },
];

test("实体合并保留关系度数和原始证据，不虚构实体分类", () => {
  expect(graphNodes(facts).find((node) => node.name === "检索")?.degree).toBe(
    2,
  );
  expect(graphNodes(facts)).toHaveLength(6);
  expect(visibleFacts(facts, [], [], 1)).toEqual(facts);
});

test("展开层级覆盖出入边，关系筛选不能被扩展绕过", () => {
  expect(visibleFacts(facts, [], ["RAG"], 1)).toEqual([facts[0]]);
  expect(visibleFacts(facts, [], ["RAG"], 2)).toEqual(facts.slice(0, 2));
  expect(visibleFacts(facts, [], ["RAG"], 3)).toEqual(facts.slice(0, 3));
  expect(visibleFacts(facts, [], ["索引"], 1)).toEqual([facts[2]]);
  expect(visibleFacts(facts, ["包含"], ["RAG"], 3)).toEqual([facts[0]]);
  expect(visibleFacts(facts, [], ["不存在"], 3)).toEqual([]);
});

test.each(["chain", "star"])(
  "大图 %s 展开画布，卡片不重叠且排布稳定",
  (shape) => {
    const many = Array.from({ length: 100 }, (_, i) => ({
      source: shape === "star" ? "中心实体" : `实体${i}`,
      target: `实体${i + 1}`,
      relation: "关联",
      quote: "正文",
      chunk_id: String(i),
    }));
    const points = layoutGraph(many);
    expect(points).toEqual(layoutGraph(many));
    expect(points).toHaveLength(101);
    const bounds = graphBounds(points);
    expect(bounds.width).toBeGreaterThan(700);
    for (const [i, point] of points.entries()) {
      expect(Number.isFinite(point.x) && Number.isFinite(point.y)).toBe(true);
      expect(point.x - nodeWidth(point.name) / 2).toBeGreaterThanOrEqual(
        bounds.x + 64,
      );
      expect(point.x + nodeWidth(point.name) / 2).toBeLessThanOrEqual(
        bounds.x + bounds.width - 64,
      );
      expect(point.y).toBeGreaterThanOrEqual(bounds.y + 86);
      expect(point.y).toBeLessThanOrEqual(bounds.y + bounds.height - 86);
      for (const other of points.slice(i + 1)) {
        const separated =
          Math.abs(point.x - other.x) >=
            (nodeWidth(point.name) + nodeWidth(other.name)) / 2 + 23.99 ||
          Math.abs(point.y - other.y) >= 68;
        expect(separated).toBe(true);
      }
    }
  },
);

test("空图和自关联保留默认画布，长实体名称卡片宽度受限", () => {
  expect(layoutGraph([])).toEqual([]);
  expect(graphBounds([])).toEqual({ x: 0, y: 0, width: 700, height: 560 });
  expect(layoutGraph([{ ...facts[0], target: "RAG" }])[0].x).toBe(350);
  expect(nodeWidth("RAG")).toBe(96);
  expect(nodeWidth("很长很长很长很长很长很长的实体名称")).toBe(148);
});

test.each([14, 100])(
  "聚焦 %i 个邻居时放射展开，中心固定且卡片不重叠",
  (count) => {
    const star = Array.from({ length: count }, (_, i) => ({
      source: "中心实体",
      target: `较长的关联实体名称${i}`,
      relation: "关联",
      quote: "正文",
      chunk_id: String(i),
    }));
    const points = layoutGraph(star, "中心实体");
    expect(points[0]).toMatchObject({ name: "中心实体", x: 350, y: 280 });
    expect(points).toEqual(layoutGraph(star, "中心实体"));
    for (const [i, point] of points.entries()) {
      for (const other of points.slice(i + 1)) {
        expect(
          Math.abs(point.x - other.x) >=
            (nodeWidth(point.name) + nodeWidth(other.name)) / 2 + 23.99 ||
            Math.abs(point.y - other.y) >= 68,
        ).toBe(true);
      }
    }
  },
);

test("聚焦实体按出入边分层，低度数节点也保持在中心", () => {
  const points = layoutGraph(facts.slice(0, 3), "RAG");
  expect(points[0]).toMatchObject({ name: "RAG", x: 350, y: 280 });
  const distances = ["检索", "向量", "索引"].map((name) => {
    const point = points.find((item) => item.name === name)!;
    return Math.hypot(point.x - 350, point.y - 280);
  });
  expect(distances[0]).toBeLessThan(distances[1]);
  expect(distances[1]).toBeLessThan(distances[2]);
  expect(layoutGraph(facts, "不存在")).toEqual(layoutGraph(facts));
});
