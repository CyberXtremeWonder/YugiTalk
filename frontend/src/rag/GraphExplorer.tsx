import { useEffect, useId, useMemo, useRef, useState } from "react";
import { identityRequest } from "../identity/api";
import type { Source } from "./api";
import {
  graphBounds,
  graphNodes,
  layoutGraph,
  nodeWidth,
  visibleFacts,
  type GraphFact,
} from "./graph";
import "./graph.css";

const palette = ["blue", "teal", "purple", "amber"];

/** 关系网络与原文共用接口事实；颜色只区分实体，不推测后端没有提供的实体分类。 */
export function GraphExplorer({
  facts,
  indexVersion,
}: {
  facts: GraphFact[];
  indexVersion?: number;
}) {
  const root = useRef<HTMLDivElement>(null);
  const canvas = useRef<SVGSVGElement>(null);
  const request = useRef(0);
  const drag = useRef<{ name?: string; x: number; y: number } | null>(null);
  const [query, setQuery] = useState("");
  const [hidden, setHidden] = useState<string[]>([]);
  const [selected, setSelected] = useState("");
  const [focus, setFocus] = useState("");
  const [depth, setDepth] = useState(1);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [moved, setMoved] = useState<Record<string, { x: number; y: number }>>(
    {},
  );
  const [list, setList] = useState(false);
  const [preview, setPreview] = useState<{
    entity: string;
    source: Source;
  } | null>(null);
  const [error, setError] = useState("");
  const marker = useId();
  const allNodes = useMemo(() => graphNodes(facts), [facts]);
  const relations = useMemo(
    () => [...new Set(facts.map((fact) => fact.relation))].sort(),
    [facts],
  );
  const matches = useMemo(
    () =>
      query.trim()
        ? allNodes
            .filter((node) =>
              node.name.toLowerCase().includes(query.trim().toLowerCase()),
            )
            .map((node) => node.name)
        : [],
    [allNodes, query],
  );
  const shown = useMemo(
    () =>
      visibleFacts(
        facts,
        hidden,
        matches.length ? matches : focus ? [focus] : [],
        depth,
      ),
    [facts, hidden, matches, focus, depth],
  );
  // 同一关系的多份证据共用一条连线，详情仍保留所有原文。
  const edges = useMemo(
    () => [
      ...new Map(
        shown.map((fact) => [
          JSON.stringify([fact.source, fact.relation, fact.target]),
          fact,
        ]),
      ).values(),
    ],
    [shown],
  );
  const noMatches = !!query.trim() && !matches.length;
  const positions = useMemo(
    () => layoutGraph(noMatches ? [] : shown, focus),
    [shown, noMatches, focus],
  );
  const nodes = positions.map((node) => ({ ...node, ...moved[node.name] }));
  const lookup = new Map(nodes.map((node) => [node.name, node]));
  const current = lookup.has(selected) ? selected : nodes[0]?.name || "";
  const evidence = shown.filter(
    (fact) => fact.source === current || fact.target === current,
  );
  const labeledEdges = new Set(
    edges
      .filter((fact) => fact.source === current || fact.target === current)
      .slice(0, 12),
  );
  const neighbors = new Set(
    evidence.flatMap((fact) => [fact.source, fact.target]),
  );
  const frame = useMemo(() => graphBounds(positions), [positions]);
  const center = {
    x: frame.x + frame.width / 2,
    y: frame.y + frame.height / 2,
  };

  function select(name: string) {
    request.current++;
    setSelected(name);
    setPreview(null);
    setError("");
  }
  function reset() {
    setPan({ x: 0, y: 0 });
    setZoom(1);
    setMoved({});
  }
  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      setZoom((value) =>
        Math.max(0.35, Math.min(2.5, value * Math.exp(-event.deltaY * 0.001))),
      );
    };
    element.addEventListener("wheel", wheel, { passive: false });
    return () => element.removeEventListener("wheel", wheel);
  }, [list]);
  useEffect(
    () => () => {
      request.current++;
    },
    [],
  );

  return (
    <div className="graph-explorer" ref={root}>
      <div className="graph-toolbar">
        <label className="graph-search">
          <svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.7"
            aria-hidden="true"
          >
            <circle cx="10.5" cy="10.5" r="6.5" />
            <path d="m16 16 4 4" />
          </svg>
          <input
            aria-label="搜索图谱实体"
            placeholder="搜索实体名称…"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setPreview(null);
              request.current++;
            }}
          />
        </label>
        <div className="graph-tabs" aria-label="图谱视图">
          <button aria-pressed={!list} onClick={() => setList(false)}>
            关系网络
          </button>
          <button aria-pressed={list} onClick={() => setList(true)}>
            实体列表
          </button>
        </div>
        <button onClick={reset}>重置视图</button>
        <button
          onClick={() => {
            const task = document.fullscreenElement
              ? document.exitFullscreen()
              : root.current?.requestFullscreen();
            void task?.catch(() => setError("当前浏览器无法进入全屏。"));
          }}
        >
          全屏
        </button>
      </div>
      <div className="graph-workspace">
        <aside className="graph-filters" aria-label="图谱筛选">
          <h3>筛选</h3>
          <h4>关系类型</h4>
          {relations.map((relation) => (
            <label key={relation} className="graph-check">
              <input
                type="checkbox"
                checked={!hidden.includes(relation)}
                onChange={(event) =>
                  setHidden((values) =>
                    event.target.checked
                      ? values.filter((value) => value !== relation)
                      : [...values, relation],
                  )
                }
              />
              <span>{relation}</span>
              <small>
                {facts.filter((fact) => fact.relation === relation).length}
              </small>
            </label>
          ))}
          <h4>展开层级</h4>
          <div className="graph-depth">
            {[1, 2, 3].map((value) => (
              <button
                key={value}
                aria-pressed={depth === value}
                onClick={() => setDepth(value)}
              >
                {value}
              </button>
            ))}
          </div>
          <p className="graph-hint">搜索或聚焦节点后，按关系层级展开。</p>
          {focus && (
            <button
              className="graph-clear"
              onClick={() => {
                setFocus("");
                setQuery("");
              }}
            >
              显示全部关系
            </button>
          )}
          <h4>
            已加载实体 <small>{allNodes.length}</small>
          </h4>
          <div className="graph-node-list">
            {allNodes
              .filter(
                (node) =>
                  !query ||
                  node.name.toLowerCase().includes(query.toLowerCase()),
              )
              .map((node) => (
                <button
                  key={node.name}
                  aria-pressed={current === node.name}
                  onClick={() => {
                    select(node.name);
                    setFocus(node.name);
                  }}
                >
                  {node.name}
                  <small>{node.degree}</small>
                </button>
              ))}
          </div>
        </aside>
        <div className={`graph-stage ${focus ? "focused" : ""}`}>
          {!list && nodes.length > 0 && (
            <div className="graph-stage-heading">
              <div>
                <strong>{focus ? "关联视图" : "关系网络"}</strong>
                <span>
                  {nodes.length} 个实体 · {shown.length} 条关系
                </span>
              </div>
              <button
                aria-pressed={!!focus}
                onClick={() => {
                  setFocus(focus ? "" : current);
                  setQuery("");
                  setDepth(1);
                  reset();
                }}
              >
                {focus ? "显示全部关系" : "聚焦所选实体"}
                <span aria-hidden="true"> ↗</span>
              </button>
            </div>
          )}
          {noMatches || !nodes.length ? (
            <div className="graph-empty">
              没有匹配的实体或关系，请调整搜索和筛选条件。
            </div>
          ) : list ? (
            <div className="graph-entity-table">
              <table>
                <thead>
                  <tr>
                    <th>实体名称</th>
                    <th>关联关系</th>
                  </tr>
                </thead>
                <tbody>
                  {nodes.map((node) => (
                    <tr key={node.name}>
                      <td>
                        <button onClick={() => select(node.name)}>
                          {node.name}
                        </button>
                      </td>
                      <td>{node.degree}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <>
              <svg
                ref={canvas}
                className="graph-canvas"
                viewBox={`${frame.x} ${frame.y} ${frame.width} ${frame.height}`}
                aria-label="知识图谱关系网络"
                onPointerDown={(event) => {
                  if (event.button !== 0) return;
                  const name =
                    (event.target as Element)
                      .closest("[data-node]")
                      ?.getAttribute("data-node") || undefined;
                  if (name) select(name);
                  drag.current = { name, x: event.clientX, y: event.clientY };
                  event.currentTarget.setPointerCapture(event.pointerId);
                }}
                onPointerMove={(event) => {
                  if (!drag.current) return;
                  const scale = Math.min(
                    event.currentTarget.clientWidth / frame.width,
                    event.currentTarget.clientHeight / frame.height,
                  );
                  const dx = (event.clientX - drag.current.x) / scale,
                    dy = (event.clientY - drag.current.y) / scale;
                  const name = drag.current.name;
                  if (name) {
                    const node = lookup.get(name)!;
                    setMoved((values) => ({
                      ...values,
                      [name]: { x: node.x + dx / zoom, y: node.y + dy / zoom },
                    }));
                  } else
                    setPan((value) => ({ x: value.x + dx, y: value.y + dy }));
                  drag.current = { name, x: event.clientX, y: event.clientY };
                }}
                onPointerUp={() => {
                  drag.current = null;
                }}
                onPointerCancel={() => {
                  drag.current = null;
                }}
              >
                <defs>
                  <marker
                    id={marker}
                    viewBox="0 0 10 10"
                    refX="9"
                    refY="5"
                    markerWidth="5"
                    markerHeight="5"
                    orient="auto-start-reverse"
                  >
                    <path d="M0 0 10 5 0 10Z" fill="context-stroke" />
                  </marker>
                </defs>
                <g
                  transform={`translate(${center.x + pan.x} ${center.y + pan.y}) scale(${zoom}) translate(${-center.x} ${-center.y})`}
                >
                  {edges.map((fact, i) => {
                    const a = lookup.get(fact.source),
                      b = lookup.get(fact.target);
                    if (!a || !b) return null;
                    const distance = Math.max(
                      1,
                      Math.hypot(b.x - a.x, b.y - a.y),
                    );
                    const dx = (b.x - a.x) / distance,
                      dy = (b.y - a.y) / distance;
                    const connected =
                      fact.source === current || fact.target === current;
                    const siblings = edges.filter(
                      (edge) =>
                        (edge.source === fact.source &&
                          edge.target === fact.target) ||
                        (edge.source === fact.target &&
                          edge.target === fact.source),
                    );
                    const lane =
                      siblings.indexOf(fact) - (siblings.length - 1) / 2;
                    const bend =
                      siblings.length > 1
                        ? lane *
                          36 *
                          (fact.source.localeCompare(fact.target) > 0 ? -1 : 1)
                        : 26;
                    const cx = (a.x + b.x) / 2 - dy * bend;
                    const cy = (a.y + b.y) / 2 + dx * bend;
                    const boundary = (node: typeof a) => {
                      const vx = cx - node.x,
                        vy = cy - node.y;
                      const t = Math.min(
                        (nodeWidth(node.name) / 2 + 5) /
                          Math.max(0.01, Math.abs(vx)),
                        26 / Math.max(0.01, Math.abs(vy)),
                      );
                      return { x: node.x + vx * t, y: node.y + vy * t };
                    };
                    const start = boundary(a),
                      end = boundary(b);
                    const self = fact.source === fact.target;
                    const incoming = fact.target === current;
                    const t = Math.max(
                      0.18,
                      Math.min(
                        0.82,
                        (incoming ? 0.4 : 0.6) +
                          lane * 0.18 * (incoming ? -1 : 1),
                      ),
                    );
                    const labelX = self
                      ? a.x
                      : (1 - t) ** 2 * a.x +
                        2 * (1 - t) * t * cx +
                        t ** 2 * b.x;
                    const labelY = self
                      ? a.y - 72
                      : (1 - t) ** 2 * a.y +
                        2 * (1 - t) * t * cy +
                        t ** 2 * b.y;
                    const label =
                      fact.relation.length > 10
                        ? fact.relation.slice(0, 10) + "…"
                        : fact.relation;
                    return (
                      <g
                        key={`${fact.chunk_id}-${i}`}
                        className={`graph-edge ${connected ? "connected" : ""}`}
                      >
                        <title>
                          {fact.source} → {fact.relation} → {fact.target}
                        </title>
                        <path
                          d={
                            self
                              ? `M${a.x - 24} ${a.y - 24} C${a.x - 80} ${a.y - 94} ${a.x + 80} ${a.y - 94} ${a.x + 24} ${a.y - 26}`
                              : `M${start.x} ${start.y} Q${cx} ${cy} ${end.x} ${end.y}`
                          }
                          markerEnd={`url(#${marker})`}
                        />
                        {(labeledEdges.has(fact) ||
                          (nodes.length < 9 && edges.length <= 12)) && (
                          <g
                            className="graph-edge-label"
                            transform={`translate(${labelX} ${labelY})`}
                          >
                            <rect
                              x={-(label.length * 11 + 18) / 2}
                              y="-10"
                              width={label.length * 11 + 18}
                              height="20"
                              rx="6"
                            />
                            <text y="4">{label}</text>
                          </g>
                        )}
                      </g>
                    );
                  })}
                  {nodes.map((node) => (
                    <g
                      key={node.name}
                      data-node={node.name}
                      role="button"
                      tabIndex={0}
                      aria-label={`实体 ${node.name}`}
                      className={`graph-node ${palette[allNodes.findIndex((item) => item.name === node.name) % palette.length]} ${current === node.name ? "selected" : ""} ${neighbors.has(node.name) ? "neighbor" : ""}`}
                      transform={`translate(${node.x} ${node.y})`}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          select(node.name);
                        }
                      }}
                    >
                      <title>
                        {node.name} · {node.degree} 条关系
                      </title>
                      <rect
                        className="graph-halo"
                        x={-nodeWidth(node.name) / 2 - 5}
                        y="-27"
                        width={nodeWidth(node.name) + 10}
                        height="54"
                        rx="16"
                      />
                      <rect
                        className="graph-node-card"
                        x={-nodeWidth(node.name) / 2}
                        y="-22"
                        width={nodeWidth(node.name)}
                        height="44"
                        rx="11"
                      />
                      <circle
                        className="graph-node-dot"
                        cx={-nodeWidth(node.name) / 2 + 17}
                        r="4"
                      />
                      <text
                        className="graph-node-label"
                        x={-nodeWidth(node.name) / 2 + 30}
                        y="4"
                      >
                        {node.name.length > 8
                          ? node.name.slice(0, 8) + "…"
                          : node.name}
                      </text>
                    </g>
                  ))}
                </g>
              </svg>
              <div className="graph-canvas-note">
                <span /> 选中实体，突出关联 · 拖动平移 · 滚轮缩放
              </div>
              <div className="graph-zoom">
                <button
                  aria-label="缩小图谱"
                  onClick={() =>
                    setZoom((value) => Math.max(0.35, value - 0.15))
                  }
                >
                  −
                </button>
                <span>{Math.round(zoom * 100)}%</span>
                <button
                  aria-label="放大图谱"
                  onClick={() =>
                    setZoom((value) => Math.min(2.5, value + 0.15))
                  }
                >
                  ＋
                </button>
                <button onClick={reset}>适应画布</button>
              </div>
            </>
          )}
        </div>
        <aside className="graph-inspector" aria-label="实体详情">
          <h3>实体详情</h3>
          {current ? (
            <>
              <h2>
                <span className="graph-entity-dot" />
                {current}
              </h2>
              <p className="graph-hint">
                {evidence.length} 条关联关系 · 点击原文核对依据
              </p>
              <h4>关联关系</h4>
              <div className="graph-connections">
                {evidence.map((fact, i) => (
                  <button
                    key={i}
                    onClick={() =>
                      select(
                        fact.source === current ? fact.target : fact.source,
                      )
                    }
                  >
                    <span>{fact.relation}</span>
                    {fact.source === current
                      ? "→ " + fact.target
                      : fact.source + " →"}
                  </button>
                ))}
              </div>
              <h4>原文证据</h4>
              {evidence.map((fact, i) => (
                <article
                  className="graph-evidence"
                  key={`${fact.chunk_id}-${i}`}
                >
                  <strong>
                    {fact.source} → {fact.relation} → {fact.target}
                  </strong>
                  <blockquote>{fact.quote}</blockquote>
                  <small>
                    来源分块 {fact.chunk_id.slice(0, 8)}
                    {indexVersion !== undefined ? ` · v${indexVersion}` : ""}
                  </small>
                  <button
                    onClick={() => {
                      const serial = ++request.current;
                      setPreview(null);
                      setError("");
                      void identityRequest<Source>(
                        `/sources/${encodeURIComponent(fact.chunk_id)}${indexVersion !== undefined ? `?index_version=${indexVersion}` : ""}`,
                      )
                        .then((source) => {
                          if (serial === request.current)
                            setPreview({ entity: current, source });
                        })
                        .catch(() => {
                          if (serial === request.current)
                            setError("来源已删除、更新或无权访问。");
                        });
                    }}
                  >
                    查看原文 ↗
                  </button>
                </article>
              ))}
              <button
                className="graph-expand"
                onClick={() => {
                  setFocus(current);
                  setQuery("");
                  setDepth((value) =>
                    focus === current ? Math.min(3, value + 1) : 1,
                  );
                }}
              >
                展开关联节点
              </button>
            </>
          ) : (
            <p className="graph-hint">选择图中的实体，查看关系和原文证据。</p>
          )}
          {error && <p role="alert">{error}</p>}
          {preview && preview.entity === current && (
            <article className="graph-source-preview">
              <strong>
                {preview.source.filename} ·{" "}
                {preview.source.page
                  ? `第 ${preview.source.page} 页`
                  : `第 ${preview.source.paragraph} 段`}
              </strong>
              <p>{preview.source.content}</p>
              <button onClick={() => setPreview(null)}>收起原文</button>
            </article>
          )}
        </aside>
      </div>
      <footer className="graph-footer">
        <span>
          已显示 {nodes.length} 个实体 · {noMatches ? 0 : shown.length} 条关系
        </span>
        <span>颜色仅用于区分实体 · 关系均来自已加载文档</span>
      </footer>
    </div>
  );
}
