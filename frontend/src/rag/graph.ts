export interface GraphFact {
  source: string;
  target: string;
  relation: string;
  quote: string;
  chunk_id: string;
}

/** 将同一实体合并为一个节点，保留关系方向与各条原文证据。 */
export function graphNodes(facts: GraphFact[]) {
  const degrees = new Map<string, number>();
  for (const fact of facts) {
    degrees.set(fact.source, (degrees.get(fact.source) || 0) + 1);
    degrees.set(fact.target, (degrees.get(fact.target) || 0) + 1);
  }
  return [...degrees]
    .map(([name, degree]) => ({ name, degree }))
    .sort((a, b) => b.degree - a.degree || a.name.localeCompare(b.name));
}

/** 层级按无向邻接扩展，图中的箭头仍保留原始方向；筛选先于扩展生效。 */
export function visibleFacts(
  facts: GraphFact[],
  hidden: string[],
  seeds: string[],
  depth: number,
) {
  const allowed = facts.filter((fact) => !hidden.includes(fact.relation));
  if (!seeds.length) return allowed;
  const reached = new Set(seeds);
  let frontier = new Set(seeds);
  for (let hop = 0; hop < depth; hop++) {
    const next = new Set<string>();
    for (const fact of allowed) {
      if (frontier.has(fact.source) && !reached.has(fact.target))
        next.add(fact.target);
      if (frontier.has(fact.target) && !reached.has(fact.source))
        next.add(fact.source);
    }
    for (const name of next) reached.add(name);
    frontier = next;
  }
  return allowed.filter(
    (fact) => reached.has(fact.source) && reached.has(fact.target),
  );
}

/** 卡片宽度与标签长度对应，长名称由视图截断，完整名称仍保留在实体上。 */
export function nodeWidth(name: string) {
  return Math.min(
    148,
    Math.max(
      96,
      44 +
        [...name.slice(0, 10)].reduce(
          (width, character) =>
            width + (character.charCodeAt(0) > 255 ? 12 : 7),
          0,
        ),
    ),
  );
}

/** 保留卡片及自关联曲线的边距，大图可平移缩放，不再挤入固定画布。 */
export function graphBounds(points: { name: string; x: number; y: number }[]) {
  const left = Math.min(
    0,
    ...points.map((point) => point.x - nodeWidth(point.name) / 2 - 64),
  );
  const right = Math.max(
    700,
    ...points.map((point) => point.x + nodeWidth(point.name) / 2 + 64),
  );
  const top = Math.min(0, ...points.map((point) => point.y - 86));
  const bottom = Math.max(560, ...points.map((point) => point.y + 86));
  return {
    x: Math.floor(left),
    y: Math.floor(top),
    width: Math.ceil(right) - Math.floor(left),
    height: Math.ceil(bottom) - Math.floor(top),
  };
}

/** 聚焦时按关系层级放射展开，全图使用确定性力导向，并保留卡片碰撞间距。 */
export function layoutGraph(facts: GraphFact[], focus?: string) {
  const nodes = graphNodes(facts);
  const focusIndex = nodes.findIndex((node) => node.name === focus);
  const radial = new Map<string, { x: number; y: number }>();
  if (focusIndex >= 0) {
    const center = nodes.splice(focusIndex, 1)[0];
    nodes.unshift(center);
    const levels = new Map([[center.name, 0]]);
    let frontier = [center.name];
    for (let level = 1; frontier.length; level++) {
      const next = new Set<string>();
      for (const fact of facts) {
        if (frontier.includes(fact.source) && !levels.has(fact.target))
          next.add(fact.target);
        if (frontier.includes(fact.target) && !levels.has(fact.source))
          next.add(fact.source);
      }
      for (const name of next) levels.set(name, level);
      frontier = [...next];
    }
    const outerLevel = Math.max(...levels.values()) + 1;
    const rings = new Map<number, typeof nodes>();
    for (const node of nodes.slice(1)) {
      const level = levels.get(node.name) ?? outerLevel;
      if (!rings.has(level)) rings.set(level, []);
      rings.get(level)!.push(node);
    }
    let radiusX = 0,
      radiusY = 0;
    radial.set(center.name, { x: 350, y: 280 });
    for (const [, ring] of [...rings].sort(([a], [b]) => a - b)) {
      radiusX = Math.max(radiusX + 212, ring.length * 28);
      radiusY = Math.max(radiusY + 150, ring.length * 18);
      ring.forEach((node, i) => {
        const angle = -Math.PI / 2 + (i * Math.PI * 2) / ring.length;
        radial.set(node.name, {
          x: 350 + Math.cos(angle) * radiusX,
          y: 280 + Math.sin(angle) * radiusY,
        });
      });
    }
  }
  const points = nodes.map((node, index) => {
    const radius = index ? 80 + Math.sqrt(index) * 44 : 0;
    const angle = index * 2.399963;
    return {
      ...node,
      x: 350 + Math.cos(angle) * radius * 1.25,
      y: 280 + Math.sin(angle) * radius,
      ...radial.get(node.name),
    };
  });
  const lookup = new Map(points.map((point) => [point.name, point]));
  const index = new Map(points.map((point, i) => [point.name, i]));
  const widths = points.map((point) => nodeWidth(point.name));
  for (let round = 0; round < (focusIndex >= 0 ? 0 : 240); round++) {
    const forces = points.map(() => ({ x: 0, y: 0 }));
    for (let i = 0; i < points.length; i++) {
      for (let j = i + 1; j < points.length; j++) {
        const dx = points[i].x - points[j].x,
          dy = points[i].y - points[j].y;
        const distance = Math.max(1, Math.hypot(dx, dy));
        const strength = 4000 / (distance * distance);
        forces[i].x += (dx / distance) * strength;
        forces[i].y += (dy / distance) * strength;
        forces[j].x -= (dx / distance) * strength;
        forces[j].y -= (dy / distance) * strength;
        const overlapX = (widths[i] + widths[j]) / 2 + 24 - Math.abs(dx);
        const overlapY = 72 - Math.abs(dy);
        if (overlapX > 0 && overlapY > 0) {
          if (overlapX < overlapY) {
            const push = Math.sign(dx || 1) * overlapX * 0.3;
            forces[i].x += push;
            forces[j].x -= push;
          } else {
            const push = Math.sign(dy || 1) * overlapY * 0.3;
            forces[i].y += push;
            forces[j].y -= push;
          }
        }
      }
    }
    for (const fact of facts) {
      const a = lookup.get(fact.source)!,
        b = lookup.get(fact.target)!;
      const dx = b.x - a.x,
        dy = b.y - a.y;
      const distance = Math.max(1, Math.hypot(dx, dy));
      const force = (distance - 160) * 0.025;
      forces[index.get(a.name)!].x += (dx / distance) * force;
      forces[index.get(a.name)!].y += (dy / distance) * force;
      forces[index.get(b.name)!].x -= (dx / distance) * force;
      forces[index.get(b.name)!].y -= (dy / distance) * force;
    }
    points.forEach((point, i) => {
      if (i === 0) {
        point.x = 350;
        point.y = 280;
        return;
      }
      point.x += Math.max(-8, Math.min(8, forces[i].x));
      point.y += Math.max(-8, Math.min(8, forces[i].y));
    });
  }

  // 先松开弹簧残留的卡片碰撞，避免高连接度节点附近的标签互相覆盖。
  for (let round = 0; round < 48; round++) {
    let collisions = 0;
    for (let i = 0; i < points.length; i++) {
      for (let j = i + 1; j < points.length; j++) {
        const a = points[i],
          b = points[j];
        const dx = a.x - b.x,
          dy = a.y - b.y;
        const overlapX = (widths[i] + widths[j]) / 2 + 32 - Math.abs(dx);
        const overlapY = 76 - Math.abs(dy);
        if (overlapX <= 0 || overlapY <= 0) continue;
        collisions++;
        const axis = overlapX < overlapY ? "x" : "y";
        const push =
          Math.sign((axis === "x" ? dx : dy) || 1) *
          (axis === "x" ? overlapX : overlapY);
        if (i !== 0) a[axis] += push / 2;
        b[axis] -= i === 0 ? push : push / 2;
      }
    }
    if (!collisions) break;
  }

  // 合并占用区间，将尚未分开的卡片放到最近空位，确保密集图也不重叠。
  points.forEach((point, i) => {
    const intervals = points
      .slice(0, i)
      .filter((other) => Math.abs(point.y - other.y) < 68)
      .map((other) => {
        const spacing =
          (nodeWidth(point.name) + nodeWidth(other.name)) / 2 + 24;
        return { left: other.x - spacing, right: other.x + spacing };
      })
      .sort((a, b) => a.left - b.left);
    const merged: typeof intervals = [];
    for (const interval of intervals) {
      const last = merged[merged.length - 1];
      if (last && interval.left <= last.right)
        last.right = Math.max(last.right, interval.right);
      else merged.push({ ...interval });
    }
    const occupied = merged.find(
      (interval) => point.x > interval.left && point.x < interval.right,
    );
    if (occupied)
      point.x =
        point.x - occupied.left < occupied.right - point.x
          ? occupied.left
          : occupied.right;
  });
  return points;
}
