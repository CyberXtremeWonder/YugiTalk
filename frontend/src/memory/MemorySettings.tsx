import { useEffect, useState } from "react";
import { identityRequest, type Identity } from "../identity/api";

interface Memory {
  id: string;
  content: string;
}
/** 管理用户主动确认的长期记忆、内容编辑与使用开关。 */
export function MemorySettings({
  identity,
  accountId,
}: {
  identity: Identity;
  accountId: string;
}) {
  const [enabled, setEnabled] = useState(true);
  const [rows, setRows] = useState<Memory[]>([]);
  const [content, setContent] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const path = `/accounts/${accountId}`;
  /** 重新读取当前功能的授权数据，并同步页面显示。 */
  async function refresh() {
    const result = await identityRequest<{ enabled: boolean; items: Memory[] }>(
      `${path}/memories`,
    );
    setEnabled(result.enabled);
    setRows(result.items);
  }
  useEffect(() => {
    let active = true;
    void identityRequest<{ enabled: boolean; items: Memory[] }>(
      `${path}/memories`,
    )
      .then((result) => {
        if (active) {
          setEnabled(result.enabled);
          setRows(result.items);
        }
      })
      .catch((error) => {
        if (active) setNotice(error.message);
      });
    return () => {
      active = false;
    };
  }, [path]);
  /** 提交记忆修改操作，成功后刷新数据并处理失败提示。 */
  async function mutate(
    route: string,
    method: string,
    body?: unknown,
    rollback?: () => void,
  ) {
    setBusy(true);
    setNotice("");
    try {
      await identityRequest(route, method, body, identity.csrf_token);
      await refresh();
      // 以服务器返回的开关和列表为准，成功后清空草稿及确认标记，避免复用上次确认。
      setContent("");
      setEditing(null);
      setConfirmed(false);
      setNotice("偏好已更新。");
    } catch (error) {
      rollback?.();
      setNotice(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      className="account-settings memory-settings"
      aria-labelledby="memory-heading"
    >
      <h2 id="memory-heading">个人记忆</h2>
      <p>仅保存你主动确认的偏好。不会自动记录，关闭后不读取或写入。</p>
      <label className="memory-toggle">
        <input
          type="checkbox"
          checked={enabled}
          disabled={busy}
          onChange={(event) => {
            const previous = enabled;
            // 开关先即时响应点击；请求失败则通过 rollback 恢复旧值。
            const next = event.target.checked;
            setEnabled(next);
            void mutate(
              `${path}/memory-preference`,
              "PATCH",
              { enabled: next },
              () => setEnabled(previous),
            );
          }}
        />
        启用长期记忆
      </label>
      {enabled && (
        <>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void mutate(
                editing ? `/memories/${editing}` : `${path}/memories`,
                editing ? "PATCH" : "POST",
                { content, confirmed },
              );
            }}
          >
            <label>
              偏好内容
              <textarea
                value={content}
                onChange={(event) => setContent(event.target.value)}
                required
                maxLength={1000}
                placeholder="例如：默认使用中文，代码示例优先使用 Python。"
              />
            </label>
            <label className="memory-toggle">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(event) => setConfirmed(event.target.checked)}
              />
              我确认保存此偏好，内容不含密码、令牌或密钥
            </label>
            <button disabled={!confirmed || busy}>
              {editing ? "更新偏好" : "保存偏好"}
            </button>
            {editing && (
              <button
                type="button"
                onClick={() => {
                  setEditing(null);
                  setContent("");
                }}
              >
                取消编辑
              </button>
            )}
          </form>
          <ul className="document-list">
            {rows.map((row) => (
              <li key={row.id}>
                <p>{row.content}</p>
                <div>
                  <button
                    disabled={busy}
                    onClick={() => {
                      setEditing(row.id);
                      setContent(row.content);
                      setConfirmed(false);
                    }}
                  >
                    修改
                  </button>
                  <button
                    disabled={busy}
                    onClick={() => void mutate(`/memories/${row.id}`, "DELETE")}
                  >
                    删除
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
      <p role="status">{notice}</p>
    </section>
  );
}
