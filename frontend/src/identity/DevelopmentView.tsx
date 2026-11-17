import { useEffect, useState } from "react";
import { identityRequest, type Identity } from "./api";
import { ToolReviewPanel } from "../tools/ToolReviewPanel";
import { useServiceHealth } from "./useServiceHealth";
import "./admin.css";

const serviceDescriptions: Record<string, string> = {
  api: "API · 状态接口响应",
  postgres: "PostgreSQL · 数据库与迁移检查",
  redis: "Redis · 队列与缓存连通性",
  storage: "对象存储 · MinIO 就绪检查",
  milvus: "Milvus · 向量服务就绪检查",
  worker: "Worker · Celery 控制通道",
  docling: "Docling · 文档解析服务",
  embedding: "BGE · 本地嵌入服务",
};

interface Account {
  id: string;
  name: string;
  email: string;
  active: boolean;
  platform_admin: boolean;
}
interface AuditEntry {
  id: string;
  action: string;
  created_at: string;
}

/** 展示管理员开发面板；功能页面继续保留，管理权限由服务端逐请求复核。 */
export function DevelopmentView({ identity }: { identity: Identity }) {
  const health = useServiceHealth();
  const { status } = health;
  const [loaded, setLoaded] = useState(false);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  useEffect(() => {
    let active = true;
    void Promise.all([
      // 账号和审计并行读取；健康检查独立刷新，依赖异常不阻塞管理操作。
      identityRequest<{ items: Account[]; next_cursor: string | null }>(
        "/admin/users?limit=20",
      ),
      identityRequest<{ items: AuditEntry[] }>("/admin/audit?limit=20"),
    ])
      .then(([users, events]) => {
        if (active) {
          setLoaded(true);
          setAccounts(users.items);
          setCursor(users.next_cursor);
          setAudit(events.items);
        }
      })
      .catch((failure) => {
        if (active)
          setError(
            failure instanceof Error ? failure.message : "管理面板加载失败",
          );
      })
      .finally(() => {
        if (active) setBusy(false);
      });
    return () => {
      active = false;
    };
  }, []);
  /** 更新账号启停状态，写请求携带防伪令牌，管理员账号不允许停用。 */
  async function toggle(account: Account) {
    setBusy(true);
    setError("");
    try {
      await identityRequest(
        `/admin/users/${account.id}`,
        "PATCH",
        { active: !account.active },
        identity.csrf_token,
      );
      setAccounts((items) =>
        items.map((item) =>
          item.id === account.id ? { ...item, active: !item.active } : item,
        ),
      );
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "账号更新失败");
    } finally {
      setBusy(false);
    }
  }
  /** 按有界游标加载下一页账号，避免一次返回全部用户。 */
  async function more() {
    if (!cursor) return;
    setBusy(true);
    setError("");
    try {
      const page = await identityRequest<{
        items: Account[];
        next_cursor: string | null;
      }>(`/admin/users?limit=20&cursor=${encodeURIComponent(cursor)}`);
      setAccounts((items) => [...items, ...page.items]);
      setCursor(page.next_cursor);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "账号加载失败");
    } finally {
      setBusy(false);
    }
  }
  const checks = Object.entries({
    api: true,
    postgres: null,
    redis: null,
    storage: null,
    milvus: null,
    worker: null,
    docling: null,
    embedding: null,
    ...status?.checks,
  });
  const healthy = checks.filter(([, ready]) => ready).length;
  const unhealthy = checks.filter(([, ready]) => ready === false).length;
  const unmonitored = checks.filter(([, ready]) => ready === null).length;
  const healthTone =
    health.stale || !status
      ? "neutral"
      : unhealthy
        ? "warn"
        : unmonitored
          ? "neutral"
          : "good";
  const healthLabel = health.stale
    ? "状态已过期"
    : !status
      ? "等待检查"
      : unhealthy
        ? "部分服务异常"
        : unmonitored
          ? "部分服务未接入探测"
          : "服务运行正常";
  const matched = accounts.filter((account) =>
    `${account.name} ${account.email}`
      .toLowerCase()
      .includes(query.trim().toLowerCase()),
  );
  return (
    <section
      className="secondary-view admin-panel"
      aria-label="管理员面板"
      aria-busy={busy}
    >
      <div className="admin-container">
        <header className="admin-heading">
          <div>
            <div className="admin-eyebrow">工作台 / 管理中心</div>
            <h1>管理员面板</h1>
            <p>查看服务运行情况，管理账号与工具审核。</p>
          </div>
          <span className="admin-mode">
            <span />
            开发模式
          </span>
        </header>
        {error && <p role="alert">{error}</p>}
        {busy && <p role="status">正在处理…</p>}
        <div className="admin-summary" aria-label="管理概览">
          <article className="admin-stat">
            <span className="admin-stat-icon" aria-hidden="true">
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.6"
              >
                <rect x="4" y="3" width="16" height="7" rx="2" />
                <rect x="4" y="14" width="16" height="7" rx="2" />
                <path d="M8 6.5h.01M8 17.5h.01M12 6.5h5M12 17.5h5" />
              </svg>
            </span>
            <div>
              <p>服务健康</p>
              <strong>
                {status && !health.stale
                  ? `${healthy} / ${checks.length}`
                  : "—"}
              </strong>
              <small>
                {health.stale ? "状态已过期，等待重新检查" : "本次健康探测结果"}
              </small>
            </div>
            {status && !health.stale && checks.length > 0 && (
              <span className={`admin-badge ${healthTone}`}>
                {unhealthy
                  ? "需要关注"
                  : unmonitored
                    ? "部分未接入"
                    : "全部正常"}
              </span>
            )}
          </article>
          <article className="admin-stat">
            <span className="admin-stat-icon violet" aria-hidden="true">
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.6"
              >
                <circle cx="9" cy="8" r="3" />
                <path d="M3 21v-3a6 6 0 0 1 12 0v3M16 5a3 3 0 0 1 0 6M17 15a5 5 0 0 1 4 5" />
              </svg>
            </span>
            <div>
              <p>已加载账号</p>
              <strong>{loaded ? accounts.length : "—"}</strong>
              <small>
                {loaded
                  ? `${accounts.filter((account) => account.active).length} 个启用 · ${accounts.filter((account) => !account.active).length} 个停用`
                  : "正在读取账号列表"}
              </small>
            </div>
          </article>
          <article className="admin-stat">
            <span className="admin-stat-icon mint" aria-hidden="true">
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.6"
              >
                <rect x="3" y="5" width="18" height="14" rx="3" />
                <path d="m3 7 9 6 9-6" />
              </svg>
            </span>
            <div>
              <p>邮箱注册</p>
              <strong className="admin-stat-word">
                {status
                  ? status.registration_ready
                    ? "已就绪"
                    : "待配置"
                  : "—"}
              </strong>
              <small>注册邮件服务配置状态</small>
            </div>
          </article>
        </div>
        <section
          className="admin-card admin-services"
          aria-labelledby="admin-services-title"
        >
          <div className="admin-section-heading">
            <div>
              <h2 id="admin-services-title">服务状态</h2>
              <p>每 15 秒自动检查 · 离开页面停止刷新</p>
            </div>
            <div className="admin-health-actions">
              {status && (
                <span className="admin-environment">{status.environment}</span>
              )}
              <button
                type="button"
                disabled={health.checking}
                onClick={health.refresh}
              >
                {health.checking ? "检查中…" : "刷新状态"}
              </button>
            </div>
          </div>
          <div
            className={`admin-health-updated ${health.stale ? "stale" : ""}`}
          >
            <span className={`admin-badge ${healthTone}`}>
              <i />
              {healthLabel}
            </span>
            <span>
              {health.checkedAt ? (
                <>
                  最近成功检查{" "}
                  <time dateTime={health.checkedAt}>
                    {new Date(health.checkedAt).toLocaleTimeString()}
                  </time>
                </>
              ) : (
                "尚未获取健康检查结果"
              )}
            </span>
          </div>
          {health.error && <p role="alert">{health.error}</p>}
          <div className="admin-service-grid">
            {checks.map(([name, ready]) => {
              const unknown = !status || health.stale || ready === null;
              const detail = status?.check_details?.[name];
              return (
                <div className="admin-service" key={name}>
                  <div className="admin-service-top">
                    <span className="admin-service-mark" aria-hidden="true">
                      {name.slice(0, 2).toUpperCase()}
                    </span>
                    <strong>{name}</strong>
                    <span
                      className={`admin-badge ${unknown ? "neutral" : ready ? "good" : "warn"}`}
                    >
                      <i />
                      {unknown
                        ? status && !health.stale && ready === null
                          ? "未接入"
                          : "未知"
                        : ready
                          ? "正常"
                          : "异常"}
                    </span>
                  </div>
                  <p>{serviceDescriptions[name] || name}</p>
                  <div className="admin-service-latency">
                    <span>探测耗时</span>
                    <strong>
                      {!unknown && detail?.latency_ms != null
                        ? `${detail.latency_ms} ms`
                        : "—"}
                    </strong>
                  </div>
                  <small>
                    {unknown
                      ? status && !health.stale && ready === null
                        ? "当前部署未接入此项健康探测"
                        : "等待最新探测结果"
                      : detail?.description ||
                        (name === "api"
                          ? "本次状态请求已成功返回"
                          : ready
                            ? "健康检查通过"
                            : "检查未通过，请检查对应服务")}
                  </small>
                </div>
              );
            })}
          </div>
          {status && (
            <>
              <dl className="admin-models">
                <div>
                  <dt>对话模型</dt>
                  <dd>
                    {status.model_id}
                    <span>{status.model_provider}</span>
                  </dd>
                </div>
                <div>
                  <dt>嵌入模型</dt>
                  <dd>{status.embedding_model}</dd>
                </div>
              </dl>
              <p className="admin-health-note">
                对话模型仅展示配置，不执行推理探测；Worker
                响应仅代表控制通道可达，未接入不代表服务异常。
              </p>
            </>
          )}
        </section>
        <div className="admin-main-grid">
          <section
            className="admin-card admin-users"
            aria-labelledby="admin-users-title"
          >
            <div className="admin-section-heading">
              <div>
                <h2 id="admin-users-title">
                  账号管理 <span>{accounts.length}</span>
                </h2>
                <p>管理已加载账号的访问状态</p>
              </div>
            </div>
            <label className="admin-search">
              <svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.6"
                aria-hidden="true"
              >
                <circle cx="10.5" cy="10.5" r="6.5" />
                <path d="m16 16 4 4" />
              </svg>
              <input
                aria-label="搜索账号"
                placeholder="搜索已加载账号的姓名或邮箱…"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
            <div className="admin-user-table">
              <table>
                <thead>
                  <tr>
                    <th scope="col">账号</th>
                    <th scope="col">角色</th>
                    <th scope="col">状态</th>
                    <th scope="col">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {matched.map((account) => (
                    <tr key={account.id}>
                      <td>
                        <div className="admin-user-identity">
                          <span
                            className={`admin-avatar ${account.platform_admin ? "is-admin" : ""}`}
                            aria-hidden="true"
                          >
                            {account.name.slice(0, 1).toUpperCase()}
                          </span>
                          <div>
                            <strong>{account.name}</strong>
                            <span>{account.email}</span>
                          </div>
                        </div>
                      </td>
                      <td>
                        <span
                          className={`admin-role ${account.platform_admin ? "is-admin" : ""}`}
                        >
                          {account.platform_admin ? "管理员" : "普通用户"}
                        </span>
                      </td>
                      <td>
                        <span
                          className={`admin-badge ${account.active ? "good" : "neutral"}`}
                        >
                          <i />
                          {account.active ? "启用" : "停用"}
                        </span>
                      </td>
                      <td>
                        <button
                          type="button"
                          className={
                            account.active ? "admin-disable" : "admin-enable"
                          }
                          disabled={busy || account.platform_admin}
                          title={
                            account.platform_admin
                              ? "管理员账号不可停用"
                              : undefined
                          }
                          onClick={() => void toggle(account)}
                        >
                          {account.active ? "停用" : "启用"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!matched.length && !busy && !error && (
              <div className="admin-empty">
                {query.trim()
                  ? "没有匹配的账号，请调整搜索条件。"
                  : "暂无账号。"}
              </div>
            )}
            <footer className="admin-users-footer">
              <span>
                已加载 {accounts.length} 个账号
                {query.trim() ? ` · 匹配 ${matched.length} 个` : ""}
              </span>
              {cursor && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void more()}
                >
                  加载更多账号
                </button>
              )}
            </footer>
          </section>
          <div className="admin-side">
            <ToolReviewPanel identity={identity} />
            <section
              className="admin-card admin-audit"
              aria-labelledby="admin-audit-title"
            >
              <div className="admin-section-heading">
                <div>
                  <h2 id="admin-audit-title">最近审计记录</h2>
                  <p>管理操作留痕，便于追溯</p>
                </div>
                <span className="admin-count">{audit.length}</span>
              </div>
              <ul className="admin-audit-list">
                {audit.map((event) => (
                  <li key={event.id}>
                    <span className="admin-audit-dot" aria-hidden="true" />
                    <div>
                      <strong>{event.action}</strong>
                      <time dateTime={event.created_at}>
                        {new Date(event.created_at).toLocaleString()}
                      </time>
                    </div>
                  </li>
                ))}
              </ul>
              {!audit.length && !busy && !error && (
                <div className="admin-empty">暂无审计记录。</div>
              )}
            </section>
          </div>
        </div>
        <p className="admin-footnote">
          管理操作记录审计 · 管理员账号受保护，不可停用
        </p>
      </div>
    </section>
  );
}
