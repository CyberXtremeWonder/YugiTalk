import { useEffect, useState } from "react";
import { identityRequest, type Identity } from "./api";
import { ToolReviewPanel } from "../tools/ToolReviewPanel";

interface Account {
  id: string;
  name: string;
  email: string;
  active: boolean;
  platform_admin: boolean;
}
interface Status {
  environment: string;
  model_provider: string;
  model_id: string;
  embedding_model: string;
  registration_ready: boolean;
  checks: Record<string, boolean>;
}
interface AuditEntry {
  id: string;
  action: string;
  created_at: string;
}

/** 展示管理员开发面板；功能页面继续保留，管理权限由服务端逐请求复核。 */
export function DevelopmentView({ identity }: { identity: Identity }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    void Promise.all([
      identityRequest<Status>("/admin/development/status"),
      identityRequest<{ items: Account[]; next_cursor: string | null }>(
        "/admin/users?limit=20",
      ),
      identityRequest<{ items: AuditEntry[] }>("/admin/audit?limit=20"),
    ])
      .then(([service, users, events]) => {
        if (active) {
          setStatus(service);
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
  return (
    <section className="secondary-view account-settings admin-panel">
      <h1>管理员面板</h1>
      <p>开发模式保留全部现有功能，管理操作记录审计。</p>
      {error && <p role="alert">{error}</p>}
      {busy && <p role="status">正在处理…</p>}
      {status && (
        <div className="account-settings">
          <h2>服务状态</h2>
          <p>
            运行环境：{status.environment} · 模型：{status.model_provider}/
            {status.model_id}
          </p>
          <p>
            嵌入模型：{status.embedding_model} · 邮箱注册：
            {status.registration_ready ? "已就绪" : "待配置"}
          </p>
          <ul>
            {Object.entries(status.checks).map(([name, ready]) => (
              <li key={name}>
                {name}：{ready ? "正常" : "不可用"}
              </li>
            ))}
          </ul>
        </div>
      )}
      <ToolReviewPanel identity={identity} />
      <h2>账号管理</h2>
      {accounts.map((account) => (
        <div className="account-member" key={account.id}>
          <div>
            <strong>{account.name}</strong>
            <p>
              {account.email} · {account.platform_admin ? "管理员" : "普通用户"}{" "}
              · {account.active ? "启用" : "停用"}
            </p>
          </div>
          <button
            type="button"
            disabled={busy || account.platform_admin}
            onClick={() => void toggle(account)}
          >
            {account.active ? "停用" : "启用"}
          </button>
        </div>
      ))}
      {cursor && (
        <button type="button" disabled={busy} onClick={() => void more()}>
          加载更多账号
        </button>
      )}
      <h2>最近审计记录</h2>
      <ul>
        {audit.map((event) => (
          <li key={event.id}>
            {event.action} · {new Date(event.created_at).toLocaleString()}
          </li>
        ))}
      </ul>
      {!audit.length && !busy && <p>暂无审计记录。</p>}
    </section>
  );
}
