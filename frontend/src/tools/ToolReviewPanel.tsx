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
  const [notice, setNotice] = useState("");
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
        if (active) setNotice(error.message);
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
      setNotice(
        decision === "approved"
          ? "已批准，个人工具已绑定提交者，仍需配置与助手授权。"
          : "已驳回，用户可查看审核意见后重新提交。",
      );
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "审核失败");
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
      setNotice(error instanceof Error ? error.message : "列表加载失败");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="tool-editor" aria-labelledby="tool-review-title">
      <h2 id="tool-review-title">工具提交审核</h2>
      {!rows.length && <p>暂无待审核工具。</p>}
      {rows.map((row) => (
        <div className="account-settings" key={row.id}>
          <h3>
            {row.config.title} · {row.config.name}
          </h3>
          <p>{row.config.description}</p>
          <p>
            用户：{row.account_id} · 提交者：{row.submitter_id}
          </p>
          <details>
            <summary>查看完整声明</summary>
            <pre>{JSON.stringify(row.config, null, 2)}</pre>
          </details>
          <label>
            审核意见
            <textarea
              aria-label={`审核意见 ${row.config.name}`}
              value={reasons[row.id] || ""}
              maxLength={500}
              onChange={(event) =>
                setReasons({ ...reasons, [row.id]: event.target.value })
              }
              disabled={busy}
            />
          </label>
          <div className="skill-actions">
            <button
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
        </div>
      ))}
      {cursor && (
        <button type="button" disabled={busy} onClick={() => void more()}>
          加载更多待审工具
        </button>
      )}
      {notice && <p role="status">{notice}</p>}
    </section>
  );
}
