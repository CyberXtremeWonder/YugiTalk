import { useEffect, useState } from "react";
import { ApiError } from "../identity/api";
import {
  fetchBalances,
  fetchUsage,
  type AccountBalance,
  type BalanceSnapshot,
  type UsageDays,
  type UsageSnapshot,
} from "./api";
import "./usage.css";

const number = new Intl.NumberFormat("zh-CN");
const providers: Record<string, string> = {
  openai_compatible: "OpenAI 兼容",
  anthropic: "Anthropic",
  mock: "Mock · 离线",
  deepseek: "DeepSeek",
  unknown: "未知来源",
};
const balanceStatuses = {
  available: "已接入",
  not_connected: "未接入",
  not_configured: "未配置凭证",
  unavailable: "暂时不可用",
};

/** 保留供应商返回的金额精度，不把账户余额当作浮点累计值。 */
export function balanceLabel(amount: string, currency: "CNY" | "USD"): string {
  const [whole, fraction = ""] = amount.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${currency === "CNY" ? "¥" : "US$"}${grouped}.${fraction.padEnd(2, "0")}`;
}

/** 缺失数据保留未知；零是明确返回的有效用量。 */
export function usageLabel(value: number | null): string {
  return value === null ? "未知" : number.format(value);
}

/** 只呈现公开错误，不将供应商响应或浏览器原始异常带入页面。 */
function errorLabel(error: unknown, timeout: boolean, balance = false): string {
  if (timeout)
    return balance ? "余额查询超时，请重试。" : "用量查询超时，请重试。";
  if (error instanceof ApiError) {
    if (error.status === 403) return "Token 用量仅限管理员访问。";
    if (error.status === 401) return "登录已过期，请重新登录。";
  }
  return balance ? "余额暂时无法加载，请重试。" : "用量暂时无法加载，请重试。";
}

/** 展示每日输入与输出，完整文本说明让图形也可通过辅助技术读取。 */
function UsageTrend({ data }: { data: UsageSnapshot }) {
  const peak = Math.max(
    ...data.trend.map((day) => day.input_tokens + day.output_tokens),
    0,
  );
  const maximum = Math.max(peak, 1);
  return (
    <section
      className="usage-card usage-trend"
      aria-labelledby="usage-trend-title"
    >
      <div className="usage-section-heading">
        <div>
          <h2 id="usage-trend-title">每日 Token 用量</h2>
          <p>按请求时间统计 · 北京时间</p>
        </div>
        <div className="usage-legend">
          <span>
            <i />
            输入
          </span>
          <span>
            <i />
            输出
          </span>
        </div>
      </div>
      {data.summary.model_calls ? (
        <>
          <div
            className="usage-chart"
            role="group"
            aria-label={`近 ${data.days} 天已知输入 ${usageLabel(data.summary.input_tokens)} Token，输出 ${usageLabel(data.summary.output_tokens)} Token。每日明细可通过键盘聚焦柱形查看。`}
          >
            {data.trend.map((day) => (
              <div
                className="usage-chart-day"
                key={day.date}
                tabIndex={0}
                aria-label={`${day.date}，输入 ${usageLabel(day.input_tokens)}，输出 ${usageLabel(day.output_tokens)}，${day.model_calls} 次请求`}
                title={`${day.date}\n输入 ${usageLabel(day.input_tokens)} · 输出 ${usageLabel(day.output_tokens)}`}
              >
                <span className="usage-chart-stack" aria-hidden="true">
                  <i
                    className="usage-chart-output"
                    style={{
                      height: `${(day.output_tokens / maximum) * 100}%`,
                    }}
                  />
                  <i
                    className="usage-chart-input"
                    style={{ height: `${(day.input_tokens / maximum) * 100}%` }}
                  />
                </span>
              </div>
            ))}
          </div>
          <div className="usage-chart-axis" aria-hidden="true">
            <span>{data.trend[0]?.date.slice(5).replace("-", "/")}</span>
            <span>峰值 {usageLabel(peak)} Token / 天</span>
            <span>{data.trend.at(-1)?.date.slice(5).replace("-", "/")}</span>
          </div>
        </>
      ) : (
        <div className="usage-empty">所选期间还没有模型请求记录</div>
      )}
    </section>
  );
}

/** 按实际模型呈现用量与费用接入状态，缺失记录不伪装成完整消费。 */
export function UsageDetails({ data }: { data: UsageSnapshot }) {
  const partial = data.summary.unknown_usage_calls > 0;
  const metrics = [
    {
      label: partial ? "已知 Token 合计" : "Token 合计",
      value: data.summary.total_tokens,
      note: `近 ${data.days} 天 · 输入 + 输出`,
      symbol: "↗",
    },
    {
      label: "输入 Token",
      value: data.summary.input_tokens,
      note: "包含供应商报告的缓存输入",
      symbol: "↘",
    },
    {
      label: "输出 Token",
      value: data.summary.output_tokens,
      note: "以供应商返回的用量为准",
      symbol: "↗",
    },
    {
      label: "模型请求",
      value: data.summary.model_calls,
      note: "包含失败与重试请求",
      symbol: "≋",
    },
  ];
  return (
    <>
      <div className="usage-kpis">
        {metrics.map((metric) => (
          <article key={metric.label}>
            <div>
              <span>{metric.label}</span>
              <i aria-hidden="true">{metric.symbol}</i>
            </div>
            <strong>{usageLabel(metric.value)}</strong>
            <small>{metric.note}</small>
          </article>
        ))}
      </div>
      {(partial || data.summary.unrecorded_runs > 0) && (
        <p className="usage-notice" role="status">
          当前为已知用量小计。
          {partial &&
            ` ${number.format(data.summary.unknown_usage_calls)} 次请求用量不完整。`}
          {data.summary.unrecorded_runs > 0 &&
            ` ${number.format(data.summary.unrecorded_runs)} 个任务未记录请求明细。`}
        </p>
      )}
      <UsageTrend data={data} />
      <section className="usage-card" aria-labelledby="usage-models-title">
        <div className="usage-section-heading">
          <div>
            <h2 id="usage-models-title">各模型用量与费用</h2>
            <p>聊天、规划、评判等已记录的模型请求</p>
          </div>
          <span className="usage-status">费用接口未接入</span>
        </div>
        <div
          className="usage-table-scroll"
          tabIndex={0}
          role="region"
          aria-label="各模型用量明细，可横向滚动"
        >
          <table className="usage-table">
            <thead>
              <tr>
                <th scope="col">模型</th>
                <th scope="col">来源</th>
                <th scope="col">请求次数</th>
                <th scope="col">输入 Token</th>
                <th scope="col">输出 Token</th>
                <th scope="col">Token 合计</th>
                <th scope="col">费用</th>
              </tr>
            </thead>
            <tbody>
              {data.models.map((model) => (
                <tr key={`${model.provider}:${model.model}`}>
                  <th scope="row">
                    <strong>{model.model}</strong>
                    {model.unknown_usage_calls > 0 && (
                      <small>
                        已知部分 · {usageLabel(model.unknown_usage_calls)}{" "}
                        次用量不完整
                      </small>
                    )}
                  </th>
                  <td>
                    <span className="usage-provider">
                      {providers[model.provider] || model.provider}
                    </span>
                  </td>
                  <td>{usageLabel(model.model_calls)}</td>
                  <td>{usageLabel(model.input_tokens)}</td>
                  <td>{usageLabel(model.output_tokens)}</td>
                  <td className="usage-total">
                    {usageLabel(model.total_tokens)}
                  </td>
                  <td>
                    <span className="usage-unconnected">未接入</span>
                  </td>
                </tr>
              ))}
              {!data.models.length && (
                <tr>
                  <td colSpan={7} className="usage-empty">
                    所选期间暂无模型用量
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <p className="usage-footnote">
          费用接口未提供数据时保留“未接入”；Token
          额度不等于费用。尚未记录的嵌入与重排用量不计入本页。
        </p>
      </section>
    </>
  );
}

/** 余额以供应商账户展示，同一账户的多个模型不重复累计余额。 */
export function BalanceCard({ balance }: { balance: AccountBalance }) {
  return (
    <article className="usage-balance-card">
      <div className="usage-balance-heading">
        <span className="usage-account-icon" aria-hidden="true">
          ◈
        </span>
        <div>
          <h3>{balance.label}</h3>
          <span>{providers[balance.provider] || balance.provider}</span>
        </div>
        <span
          className={`usage-status ${balance.status === "available" ? "connected" : ""}`}
        >
          {balanceStatuses[balance.status]}
        </span>
      </div>
      {balance.status === "available" && balance.amounts.length ? (
        balance.amounts.map((amount) => (
          <div className="usage-currency" key={amount.currency}>
            <span>
              可用余额 <small>{amount.currency}</small>
            </span>
            <strong>
              {balanceLabel(amount.total_balance, amount.currency)}
            </strong>
            <dl>
              <div>
                <dt>充值余额</dt>
                <dd>
                  {balanceLabel(amount.topped_up_balance, amount.currency)}
                </dd>
              </div>
              <div>
                <dt>赠送余额</dt>
                <dd>{balanceLabel(amount.granted_balance, amount.currency)}</dd>
              </div>
            </dl>
          </div>
        ))
      ) : (
        <div className="usage-balance-missing">
          <strong>—</strong>
          <span>
            {balance.status === "not_connected"
              ? "当前连接尚未接入余额接口"
              : balance.status === "not_configured"
                ? "请先在模型连接中配置凭证"
                : "余额查询失败，请稍后刷新"}
          </span>
        </div>
      )}
      {balance.is_available === false && (
        <p className="usage-balance-warning">
          供应商提示余额不足，当前账户可能无法继续调用。
        </p>
      )}
      <div className="usage-balance-footer">
        <p title={balance.models.join("、")}>
          {balance.models.join(" · ") || "模型连接"}
        </p>
        {balance.checked_at && (
          <small>
            查询于 {new Date(balance.checked_at).toLocaleString("zh-CN")}
          </small>
        )}
      </div>
    </article>
  );
}

/** 分开加载用量与账户余额，切换窗口或离开页面时取消失效请求。 */
export function UsageView() {
  const [days, setDays] = useState<UsageDays>(30);
  const [refresh, setRefresh] = useState(0);
  const [data, setData] = useState<UsageSnapshot | null>(null);
  const [balances, setBalances] = useState<BalanceSnapshot | null>(null);
  const [usageLoading, setUsageLoading] = useState(true);
  const [balanceLoading, setBalanceLoading] = useState(true);
  const [usageError, setUsageError] = useState("");
  const [balanceError, setBalanceError] = useState("");
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 12000);
    setUsageLoading(true);
    setUsageError("");
    void fetchUsage(days, controller.signal)
      .then((snapshot) => {
        if (active) setData(snapshot);
      })
      .catch((error: unknown) => {
        if (active) setUsageError(errorLabel(error, timedOut));
      })
      .finally(() => {
        clearTimeout(timer);
        if (active) setUsageLoading(false);
      });
    return () => {
      active = false;
      clearTimeout(timer);
      controller.abort();
    };
  }, [days, refresh]);
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 16000);
    setBalanceLoading(true);
    setBalanceError("");
    void fetchBalances(controller.signal)
      .then((snapshot) => {
        if (active) setBalances(snapshot);
      })
      .catch((error: unknown) => {
        if (active) setBalanceError(errorLabel(error, timedOut, true));
      })
      .finally(() => {
        clearTimeout(timer);
        if (active) setBalanceLoading(false);
      });
    return () => {
      active = false;
      clearTimeout(timer);
      controller.abort();
    };
  }, [refresh]);
  const current = data?.days === days ? data : null;
  return (
    <section className="usage-view" aria-labelledby="usage-title">
      <div className="usage-container">
        <header className="usage-heading">
          <div>
            <span className="usage-eyebrow">模型消费</span>
            <h1 id="usage-title">Token 用量</h1>
            <p>查看各模型的真实用量、费用明细与账户余额。</p>
          </div>
          <div className="usage-actions">
            <div
              className="usage-periods"
              role="group"
              aria-label="统计时间范围"
            >
              {([7, 30, 90] as const).map((period) => (
                <button
                  key={period}
                  aria-pressed={days === period}
                  onClick={() => setDays(period)}
                >
                  近 {period} 天
                </button>
              ))}
            </div>
            <button
              className="usage-refresh"
              disabled={usageLoading || balanceLoading}
              onClick={() => setRefresh((value) => value + 1)}
            >
              <span aria-hidden="true">↻</span>
              {usageLoading || balanceLoading ? "更新中…" : "刷新"}
            </button>
          </div>
        </header>
        <div className="usage-context">
          <span>
            <i />
            供应商报告用量
          </span>
          {current && (
            <time dateTime={current.sampled_at}>
              更新于 {new Date(current.sampled_at).toLocaleString("zh-CN")}
            </time>
          )}
        </div>
        <div aria-busy={usageLoading}>
          {usageError && (
            <p className="usage-error" role="alert">
              {usageError}
              {current && " 当前显示上次成功获取的数据。"}
            </p>
          )}
          {current ? (
            <UsageDetails data={current} />
          ) : usageLoading ? (
            <div className="usage-loading" role="status">
              <span />
              正在加载模型用量…
            </div>
          ) : (
            <div className="usage-card usage-empty">
              暂时没有可展示的用量数据
            </div>
          )}
        </div>
        <section
          className="usage-accounts"
          aria-labelledby="usage-accounts-title"
          aria-busy={balanceLoading}
        >
          <div className="usage-section-heading">
            <div>
              <h2 id="usage-accounts-title">账户余额</h2>
              <p>来自当前模型连接 · 账户共享余额，不按模型相加</p>
            </div>
            <span className="usage-status">独立于统计时间范围</span>
          </div>
          <p className="usage-account-scope">
            仅查询默认模型、RAG
            角色及当前管理员的助手连接；余额不代表所选期间支出。
          </p>
          {balanceError && (
            <p className="usage-error" role="alert">
              {balanceError}
              {balances && " 当前显示上次成功查询的余额。"}
            </p>
          )}
          {balances ? (
            balances.balances.length ? (
              <div className="usage-balances">
                {balances.balances.map((balance) => (
                  <BalanceCard key={balance.id} balance={balance} />
                ))}
              </div>
            ) : (
              <div className="usage-card usage-empty">
                当前没有可查询余额的模型连接
              </div>
            )
          ) : balanceLoading ? (
            <div className="usage-card usage-loading" role="status">
              <span />
              正在查询账户余额…
            </div>
          ) : null}
          {balances?.truncated && (
            <p className="usage-footnote">
              模型连接较多，仅展示本次返回的账户。
            </p>
          )}
        </section>
      </div>
    </section>
  );
}
