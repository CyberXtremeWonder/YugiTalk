import { useNotice } from "../shared/useNotice";
import { useEffect, useState } from "react";
import { identityRequest, type Identity } from "../identity/api";
import type { ToolConfig } from "./api";

interface Submission {
  id: string;
  account_id: string;
  submitter_id: string;
  config: ToolConfig;
}

/** 管理员审核待审工具声明；查看及批准均不访问工具端点。 */
export function ToolReviewPanel({ identity }: { identity: Identity }) {
  const [rows, setRows] = useState<Submission[]>([]);
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const { notice, setNotice, noticeIsError, reportError } = useNotice();
  useEffect(() => {
    let active = true;
    void identityRequest<{ items: Submission[]; next_cursor: string | null }>(
      "/admin/tool-submissions?limit=20",
    )
      .then((page) => {
        if (active) {
          setRows(page.items);
          setCursor(page.next_cursor);
        }
      })
      .catch((error) => {
        if (active) {
          setLoadFailed(true);
          reportError(error.message);
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);
  /** 提交审核结论，服务端重新验证权限、状态、内容完整性和目录配额。 */
  async function review(row: Submission, decision: "approved" | "rejected") {
    const reason = (reasons[row.id] || "").trim();
    if (decision === "rejected" && !reason) {
      setNotice("驳回时请填写审核意见。");
      return;
    }
    setBusy(true);
    setNotice("");
    try {
      await identityRequest(
        `/admin/tool-submissions/${row.id}/review`,
        "POST",
        { decision, reason },
        identity.csrf_token,
      );
      setRows((items) => items.filter((item) => item.id !== row.id));
      // 只有服务端成功提交结论后才移出待审列表，失败时保留条目供用户处理。
      setNotice(
        decision === "approved"
          ? "已批准，个人工具已绑定提交者，仍需配置与助手授权。"
          : "已驳回，用户可查看审核意见后重新提交。",
      );
    } catch (error) {
      reportError(error instanceof Error ? error.message : "审核失败");
    } finally {
      setBusy(false);
    }
  }
  /** 按游标读取下一页待审声明，限制每次请求数量。 */
  async function more() {
    if (!cursor) return;
    setBusy(true);
    try {
      const page = await identityRequest<{
        items: Submission[];
        next_cursor: string | null;
      }>(
        `/admin/tool-submissions?limit=20&cursor=${encodeURIComponent(cursor)}`,
      );
      setRows((items) => [...items, ...page.items]);
      setCursor(page.next_cursor);
    } catch (error) {
      reportError(error instanceof Error ? error.message : "列表加载失败");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      className="tool-editor admin-review"
      aria-labelledby="tool-review-title"
      aria-busy={loading || busy}
    >
      <header className="admin-section-heading">
        <div>
          <h2 id="tool-review-title">工具提交审核</h2>
          <p>核对工具声明，处理用户提交的审核申请。</p>
        </div>
        {!loading && !loadFailed && (
          <span className="admin-review-count">已加载 {rows.length} 项</span>
        )}
      </header>
      {loading && <p role="status">正在加载待审核工具…</p>}
      {!loading && !loadFailed && !rows.length && (
        <div className="admin-review-empty">
          <svg
            width="32"
            height="32"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <circle cx="12" cy="12" r="9" />
            <path d="m8 12 3 3 5-6" />
          </svg>
          <strong>暂无待审核工具。</strong>
          <p>新的工具提交会显示在这里。</p>
        </div>
      )}
      <div className="admin-review-list">
        {rows.map((row) => (
          <article className="admin-review-card" key={row.id}>
            <div className="admin-review-card-heading">
              <h3>{row.config.title}</h3>
              <code>{row.config.name}</code>
            </div>
            <p>{row.config.description}</p>
            <dl className="admin-review-meta">
              <div>
                <dt>用户</dt>
                <dd>{row.account_id}</dd>
              </div>
              <div>
                <dt>提交者</dt>
                <dd>{row.submitter_id}</dd>
              </div>
            </dl>
            <details>
              <summary>查看完整声明</summary>
              <pre>{JSON.stringify(row.config, null, 2)}</pre>
            </details>
            <label>
              审核意见
              <textarea
                aria-label={`审核意见 ${row.config.name}`}
                placeholder="填写审核意见，驳回时必填"
                rows={3}
                value={reasons[row.id] || ""}
                maxLength={500}
                onChange={(event) =>
                  setReasons({ ...reasons, [row.id]: event.target.value })
                }
                disabled={busy}
              />
            </label>
            <div className="skill-actions admin-review-actions">
              <button
                className="admin-review-approve"
                type="button"
                disabled={busy}
                onClick={() => void review(row, "approved")}
              >
                批准
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => void review(row, "rejected")}
              >
                驳回
              </button>
            </div>
          </article>
        ))}
      </div>
      {cursor && (
        <button type="button" disabled={busy} onClick={() => void more()}>
          加载更多待审工具
        </button>
      )}
      {notice && <p role={noticeIsError ? "alert" : "status"}>{notice}</p>}
    </section>
  );
}
