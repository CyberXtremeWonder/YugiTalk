import { useEffect, useRef, useState } from "react";
import { fetchSnapshot, type Snapshot, type DiagnosticError } from "./api";
import "./dashboard.css";

const number = new Intl.NumberFormat("zh-CN");
const states: Record<string, string> = {
  queued: "排队中",
  running: "执行中",
  waiting_approval: "等待审批",
  waiting_input: "等待补充",
  succeeded: "已完成",
  failed: "已失败",
  cancelled: "已取消",
};

/** 绘制分钟趋势，悬停可查看数值，并提供可访问的文字摘要。 */
function Trend({
  values,
  times,
  label,
  color,
  unit = "/ 分钟",
}: {
  values: number[];
  times: string[];
  label: string;
  color: string;
  unit?: string;
}) {
  const peak = Math.max(...values, 1);
  // 将等间隔分钟样本映射到 SVG 坐标；峰值至少为 1，零流量时避免除零。
  const points = values
    .map(
      (value, index) =>
        `${12 + (index * 536) / Math.max(values.length - 1, 1)},${138 - (value / peak) * 110}`,
    )
    .join(" ");
  return (
    <div className="monitor-trend">
      <svg
        viewBox="0 0 560 160"
        role="img"
        aria-label={`${label}，峰值 ${number.format(Math.max(...values, 0))} ${unit}`}
      >
        {[28, 83, 138].map((y) => (
          <line
            key={y}
            x1="12"
            x2="548"
            y1={y}
            y2={y}
            className="monitor-gridline"
          />
        ))}
        <polygon
          points={`12,138 ${points} 548,138`}
          fill={color}
          opacity="0.08"
        />
        <polyline
          points={points}
          fill="none"
          stroke={color}
          strokeWidth="2.5"
          strokeLinejoin="round"
        />
        {values.map((value, index) => (
          <circle
            key={index}
            cx={12 + (index * 536) / Math.max(values.length - 1, 1)}
            cy={138 - (value / peak) * 110}
            r="4"
            fill={color}
          >
            <title>
              {new Date(times[index]).toLocaleTimeString("zh-CN", {
                hour: "2-digit",
                minute: "2-digit",
              })}{" "}
              · {number.format(value)}
            </title>
          </circle>
        ))}
      </svg>
      <div className="monitor-axis">
        <span>
          {times[0] &&
            new Date(times[0]).toLocaleTimeString("zh-CN", {
              hour: "2-digit",
              minute: "2-digit",
            })}
        </span>
        <span>
          峰值 {number.format(Math.max(...values, 0))} {unit}
        </span>
        <span>现在</span>
      </div>
    </div>
  );
}

/** 展示可关联的异常元数据，业务正文和用户信息不进入面板。 */
function ErrorRow({ item }: { item: DiagnosticError }) {
  return (
    <li className="monitor-error">
      <span
        className={`monitor-tag ${item.status && item.status < 500 ? "warning" : "danger"}`}
      >
        {item.kind === "http"
          ? item.status
          : item.kind === "task"
            ? "任务"
            : "资料"}
      </span>
      <div>
        <strong>{item.code}</strong>
        <p>{item.route ? `${item.method} ${item.route}` : `编号 ${item.id}`}</p>
        {item.request_id && (
          <details>
            <summary>关联标识</summary>
            <p>
              Request: <code>{item.request_id}</code>
            </p>
            <p>
              Trace: <code>{item.trace_id || "无"}</code>
            </p>
          </details>
        )}
      </div>
      <time>
        {item.timestamp
          ? new Date(item.timestamp).toLocaleTimeString("zh-CN")
          : "当前失败状态"}
      </time>
    </li>
  );
}

/** 每五秒刷新监控，暂停和隐藏页面停止轮询；失败保留上次数据并明确标记。 */
export function Dashboard() {
  const [data, setData] = useState<Snapshot | null>(null);
  const [error, setError] = useState("");
  const [paused, setPaused] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState("all");
  const [metric, setMetric] = useState<"requests" | "average_ms">("requests");
  const lastRefresh = useRef(refresh);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    let controller: AbortController | undefined;
    /** 单次请求设定超时，完成后再安排下一次，避免请求重叠。 */
    async function load() {
      if (!active || document.hidden) return;
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 8000);
      setBusy(true);
      try {
        const snapshot = await fetchSnapshot(controller.signal);
        if (active) {
          setData(snapshot);
          setError("");
        }
      } catch (failure) {
        if (active && !document.hidden)
          setError(
            failure instanceof Error ? failure.message : "监控刷新失败。",
          );
      } finally {
        clearTimeout(timeout);
        controller = undefined;
        if (active) {
          setBusy(false);
          if (!paused) timer = setTimeout(() => void load(), 5000);
        }
      }
    }
    /** 页面重新可见时更新数据，隐藏时释放请求和定时器。 */
    function visibility() {
      clearTimeout(timer);
      if (document.hidden) controller?.abort();
      else if (!paused && !controller) void load();
    }
    setBusy(false);
    if (!paused || lastRefresh.current !== refresh) void load();
    // 暂停只关闭自动刷新；手动刷新计数变化仍允许单次请求。
    lastRefresh.current = refresh;
    document.addEventListener("visibilitychange", visibility);
    return () => {
      active = false;
      clearTimeout(timer);
      controller?.abort();
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [paused, refresh]);
  const errors = data
    ? [...data.http.errors, ...data.tasks.errors].filter(
        (item) => filter === "all" || item.kind === filter,
      )
    : [];
  return (
    <section className="monitor-dashboard" aria-labelledby="monitor-title">
      <header className="monitor-heading">
        <div>
          <span className="monitor-eyebrow">YUGITALK / OBSERVABILITY</span>
          <h1 id="monitor-title">实时监控</h1>
          <p>用量、流量与异常，一眼掌握运行情况。</p>
        </div>
        <div className="monitor-actions">
          <span className={`monitor-live ${error ? "danger" : ""}`}>
            <i />
            {error ? "连接异常" : paused ? "已暂停" : "每 5 秒更新"}
          </span>
          <button onClick={() => setPaused(!paused)}>
            {paused ? "恢复实时" : "暂停刷新"}
          </button>
          <button
            disabled={busy}
            onClick={() => setRefresh((value) => value + 1)}
          >
            {busy ? "刷新中…" : "立即刷新"}
          </button>
        </div>
      </header>
      <div className="monitor-context">
        <span>最近 15 分钟 · 接口统计覆盖当前 API 进程</span>
        <span>
          最后更新{" "}
          {data ? new Date(data.sampled_at).toLocaleTimeString("zh-CN") : "—"}
        </span>
      </div>
      {error && (
        <p className="monitor-alert" role="alert">
          {error} {data ? "当前展示上次成功获取的数据。" : ""}
        </p>
      )}
      {!data ? (
        <div className="monitor-empty" role="status">
          {error ? "等待重新获取监控数据" : "正在连接监控数据…"}
        </div>
      ) : (
        <>
          <div className="monitor-kpis">
            <article>
              <span>累计结算 Token</span>
              <strong>{number.format(data.tasks.charged_tokens)}</strong>
              <small>
                近 15 分钟 +{number.format(data.tasks.recent_tokens)}
              </small>
            </article>
            <article>
              <span>接口请求</span>
              <strong>
                {number.format(data.http.requests)}
                <em>次</em>
              </strong>
              <small>最近 15 个分钟桶 · 不含面板轮询</small>
            </article>
            <article>
              <span>平均响应耗时</span>
              <strong>
                {number.format(data.http.average_ms)}
                <em>ms</em>
              </strong>
              <small>响应头耗时，流式正文另计</small>
            </article>
            <article
              className={data.http.server_errors ? "monitor-kpi-error" : ""}
            >
              <span>服务异常率 · 5xx</span>
              <strong>
                {data.http.error_rate}
                <em>%</em>
              </strong>
              <small>
                {data.http.server_errors} 次服务异常 · {data.http.client_errors}{" "}
                次 4xx
              </small>
            </article>
          </div>
          <div className="monitor-charts">
            <article className="monitor-card">
              <div className="monitor-card-title">
                <h2>{metric === "requests" ? "接口流量" : "响应耗时"}</h2>
                <div
                  className="monitor-metrics"
                  role="group"
                  aria-label="接口趋势指标"
                >
                  <button
                    aria-pressed={metric === "requests"}
                    onClick={() => setMetric("requests")}
                  >
                    流量
                  </button>
                  <button
                    aria-pressed={metric === "average_ms"}
                    onClick={() => setMetric("average_ms")}
                  >
                    耗时
                  </button>
                </div>
              </div>
              <Trend
                label={
                  metric === "requests" ? "接口请求趋势" : "平均响应耗时趋势"
                }
                values={data.http.trend.map((item) => item[metric])}
                times={data.http.trend.map((item) => item.timestamp)}
                color="#4c83ee"
                unit={metric === "requests" ? "/ 分钟" : "ms"}
              />
              <div className="monitor-chart-foot">
                <span>
                  15 分钟累计 {number.format(data.http.requests)} 次请求
                </span>
                <span>5xx {data.http.server_errors} 次</span>
              </div>
            </article>
            <article className="monitor-card">
              <div className="monitor-card-title">
                <h2>Token 结算趋势</h2>
                <span>Token / 分钟</span>
              </div>
              <Trend
                label="Token 结算趋势"
                values={data.tasks.trend.map((item) => item.tokens)}
                times={data.tasks.trend.map((item) => item.timestamp)}
                color="#8c68dc"
              />
              <div className="monitor-chart-foot">
                <span>已知输入 {number.format(data.tasks.input_tokens)}</span>
                <span>已知输出 {number.format(data.tasks.output_tokens)}</span>
              </div>
              <small className="monitor-note">
                {data.tasks.unknown_usage_runs
                  ? `${data.tasks.unknown_usage_runs} 个已结束任务未返回完整用量；结算量可能包含保守估算。`
                  : "任务结束后计入；执行中的增量尚未结算。"}
              </small>
            </article>
          </div>
          <div className="monitor-task-strip">
            <strong>任务状态</strong>
            {Object.entries(states).map(([key, title]) => (
              <span key={key}>
                <i className={`state-${key}`} />
                {title}
                <b>{number.format(data.tasks.states[key] || 0)}</b>
              </span>
            ))}
          </div>
          <div className="monitor-details">
            <article className="monitor-card">
              <div className="monitor-card-title">
                <h2>接口排行</h2>
                <span>按调用量 · 前 20 项</span>
              </div>
              <div className="monitor-table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>接口</th>
                      <th>请求</th>
                      <th>均值 / 最大 ms</th>
                      <th>4xx / 5xx</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.http.routes.map((route) => (
                      <tr key={`${route.method}:${route.route}`}>
                        <td>
                          <span className="monitor-method">{route.method}</span>
                          <code>{route.route}</code>
                        </td>
                        <td>{number.format(route.requests)}</td>
                        <td>
                          {route.average_ms} / {route.max_ms}
                        </td>
                        <td
                          className={route.server_errors ? "monitor-red" : ""}
                        >
                          {route.client_errors} / {route.server_errors}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {!data.http.routes.length && (
                  <p className="monitor-empty">尚无业务接口流量。</p>
                )}
              </div>
            </article>
            <article className="monitor-card">
              <div className="monitor-card-title">
                <h2>异常与告警</h2>
                <select
                  aria-label="异常类型"
                  value={filter}
                  onChange={(event) => setFilter(event.target.value)}
                >
                  <option value="all">全部类型</option>
                  <option value="http">接口</option>
                  <option value="task">任务</option>
                  <option value="document">资料</option>
                </select>
              </div>
              <p className="monitor-note">
                接口最近 20 条 · 任务近 15 分钟 · 资料当前失败状态
              </p>
              <ul className="monitor-errors">
                {errors.map((item, index) => (
                  <ErrorRow
                    key={`${item.kind}:${item.request_id || item.id}:${index}`}
                    item={item}
                  />
                ))}
              </ul>
              {!errors.length && (
                <p className="monitor-empty">当前范围内没有异常记录。</p>
              )}
            </article>
          </div>
          <article className="monitor-card">
            <div className="monitor-card-title">
              <h2>模型用量</h2>
              <span>累计结算 · 前 8 项</span>
            </div>
            <div className="monitor-models">
              {data.tasks.models.map((model) => (
                <div key={`${model.provider}:${model.model}`}>
                  <span>
                    {model.provider} / {model.model}
                  </span>
                  <strong>
                    {number.format(model.tokens)} <small>Token</small>
                  </strong>
                  <span>{number.format(model.runs)} 个任务</span>
                </div>
              ))}
              {!data.tasks.models.length && (
                <p className="monitor-empty">尚无模型任务。</p>
              )}
            </div>
          </article>
          <p className="monitor-note">
            Token 与任务数据来自全平台数据库；接口计数在 API
            进程重启后重新开始。进程启动于{" "}
            {new Date(data.http.started_at).toLocaleString("zh-CN")}
            。前端浏览器异常目前在控制台查看。
          </p>
        </>
      )}
    </section>
  );
}
