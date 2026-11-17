import { useNotice } from "../shared/useNotice";
import { useEffect, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { allItems, identityRequest, type Identity } from "../identity/api";
import "../shared/configuration.css";

interface MCPServer {
  id: string;
  name: string;
  url: string;
  transport: "sse" | "streamable_http";
  enabled: boolean;
  has_token: boolean;
  tools: {
    name: string;
    title: string;
    description: string;
    enabled: boolean;
  }[];
}

const empty = {
  name: "",
  url: "",
  transport: "streamable_http" as MCPServer["transport"],
  enabled: true,
};

/** 用户私有远程 MCP 配置；凭证只写入，不从 API 取回。 */
export function MCPPanel({ identity }: { identity: Identity }) {
  const [servers, setServers] = useState<MCPServer[]>([]);
  const [selected, setSelected] = useState<MCPServer | null>(null);
  const [draft, setDraft] = useState(empty);
  const [token, setToken] = useState("");
  const [clearToken, setClearToken] = useState(false);
  const [busy, setBusy] = useState(false);
  const [search, setSearch] = useState("");
  const { notice, setNotice, noticeIsError, reportError } = useNotice();
  const [deleteTarget, setDeleteTarget] = useState<MCPServer | null>(null);
  const path = "/me/mcp-servers";
  const hasUnsavedChanges =
    !!selected &&
    (draft.name !== selected.name ||
      draft.url !== selected.url ||
      draft.transport !== selected.transport ||
      !!token ||
      clearToken);

  useEffect(() => {
    let active = true;
    void allItems<MCPServer>(path)
      .then((rows) => {
        if (active) {
          setServers(rows);
          if (rows[0]) open(rows[0]);
        }
      })
      .catch((error: Error) => {
        if (active) reportError(error.message);
      });
    return () => {
      active = false;
    };
  }, []);

  function open(server: MCPServer | null) {
    setSelected(server);
    setDraft(
      server
        ? {
            name: server.name,
            url: server.url,
            transport: server.transport,
            enabled: server.enabled,
          }
        : empty,
    );
    setToken("");
    setClearToken(false);
    setDeleteTarget(null);
    setNotice("");
  }

  async function action(
    operation: () => Promise<MCPServer | null>,
    message: string,
  ) {
    setBusy(true);
    setNotice("");
    try {
      const result = await operation();
      const rows = await allItems<MCPServer>(path);
      setServers(rows);
      open(
        result ? (rows.find((row) => row.id === result.id) ?? result) : null,
      );
      setNotice(message);
    } catch (error) {
      reportError(error instanceof Error ? error.message : "MCP 操作失败");
    } finally {
      setBusy(false);
    }
  }

  /** 删除指定服务器，保留其他服务器正在编辑的草稿。 */
  async function deleteServer() {
    if (!deleteTarget) return;
    setBusy(true);
    setNotice("");
    try {
      await identityRequest(
        `${path}/${deleteTarget.id}`,
        "DELETE",
        undefined,
        identity.csrf_token,
      );
      setServers((rows) => rows.filter((row) => row.id !== deleteTarget.id));
      if (selected?.id === deleteTarget.id) open(null);
      setDeleteTarget(null);
      setNotice("服务器已删除，凭证和调用权限已撤销。");
    } catch (error) {
      reportError(
        error instanceof Error ? error.message : "删除 MCP 服务器失败",
      );
    } finally {
      setBusy(false);
    }
  }

  const discover = () => {
    if (!selected || busy || !selected.enabled || hasUnsavedChanges) return;
    void action(
      () =>
        identityRequest<MCPServer>(
          `${path}/${selected.id}/discover`,
          "POST",
          {},
          identity.csrf_token,
        ),
      "连接成功，已更新工具列表。新工具默认停用，请选择允许助手使用的工具。",
    );
  };
  return (
    <section
      className="secondary-view configuration-workspace"
      aria-labelledby="view-title"
    >
      <header className="cf-heading">
        <div>
          <h1 id="view-title">MCP</h1>
          <p>连接外部服务，让助手调用更多能力。</p>
        </div>
      </header>
      {notice && (
        <p
          className="cf-notice"
          data-error={noticeIsError}
          role={noticeIsError ? "alert" : "status"}
        >
          {notice}
        </p>
      )}
      <div className="cf-three-columns">
        <aside className="cf-catalog" aria-label="我的服务器">
          <h2>
            我的服务器 <span className="cf-count">{servers.length}</span>
          </h2>
          <input
            className="cf-search"
            aria-label="搜索服务器"
            placeholder="搜索服务器…"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <div className="cf-items">
            {servers
              .filter((row) =>
                row.name.toLowerCase().includes(search.trim().toLowerCase()),
              )
              .map((row) => (
                <div
                  className="cf-entry"
                  key={row.id}
                  data-selected={selected?.id === row.id}
                >
                  <button
                    className="cf-select"
                    disabled={busy}
                    aria-pressed={selected?.id === row.id}
                    onClick={() => open(row)}
                  >
                    <span className="cf-avatar" aria-hidden="true">
                      ♧
                    </span>
                    <span>
                      <strong>{row.name}</strong>
                      <small>
                        {row.enabled ? "已启用" : "已停用"} ·{" "}
                        {row.transport === "sse" ? "SSE" : "Streamable HTTP"}
                      </small>
                    </span>
                  </button>
                </div>
              ))}
          </div>
          {!servers.length && (
            <p className="cf-empty">
              还没有服务器，先添加一个公共 HTTPS MCP 服务。
            </p>
          )}
          {!!servers.length &&
            !servers.some((row) =>
              row.name.toLowerCase().includes(search.trim().toLowerCase()),
            ) && <p className="cf-empty">没有匹配的服务器。</p>}
        </aside>
        <div className="cf-editor">
          <button
            className="cf-primary"
            disabled={busy}
            onClick={() => open(null)}
          >
            ＋ 添加 MCP 服务器
          </button>

          <form
            id="mcp-config-form"
            className="cf-stack"
            onSubmit={(event) => {
              event.preventDefault();
              if (busy) return;
              void action(
                () =>
                  identityRequest<MCPServer>(
                    selected ? `${path}/${selected.id}` : path,
                    selected ? "PUT" : "POST",
                    {
                      ...draft,
                      ...(clearToken ? { token: "" } : token ? { token } : {}),
                    },
                    identity.csrf_token,
                  ),
                "已保存。配置变更后请重新测试连接并选择工具；旧任务权限已失效。",
              );
            }}
          >
            <fieldset disabled={busy} className="cf-stack">
              <section className="cf-card">
                <h2>服务器配置</h2>
                <label>
                  服务器名称
                  <input
                    required
                    maxLength={80}
                    value={draft.name}
                    onChange={(event) =>
                      setDraft({ ...draft, name: event.target.value })
                    }
                  />
                </label>
                <label>
                  服务器 URL
                  <input
                    required
                    type="url"
                    maxLength={2048}
                    placeholder="https://mcp.example.com/mcp"
                    value={draft.url}
                    onChange={(event) =>
                      setDraft({ ...draft, url: event.target.value })
                    }
                  />
                </label>
                <label>
                  传输类型
                  <select
                    aria-label="传输类型"
                    value={draft.transport}
                    onChange={(event) =>
                      setDraft({
                        ...draft,
                        transport: event.target.value as MCPServer["transport"],
                      })
                    }
                  >
                    <option value="streamable_http">Streamable HTTP</option>
                    <option value="sse">SSE</option>
                  </select>
                </label>
                <p className="cf-help">
                  填写公共 HTTPS 地址，端口 443，不含查询串。密钥请填写在 Token
                  中。
                </p>
              </section>
              <section className="cf-card">
                <div className="cf-card-title">
                  <h2>认证设置</h2>
                  <span className="cf-tag">Bearer Token</span>
                </div>
                <label>
                  Bearer Token（可选）
                  <input
                    aria-label="Bearer Token（可选）"
                    type="password"
                    autoComplete="new-password"
                    maxLength={4096}
                    value={token}
                    disabled={clearToken}
                    placeholder={
                      selected?.has_token
                        ? "已保存；留空保留"
                        : "无认证服务可留空"
                    }
                    onChange={(event) => setToken(event.target.value)}
                  />
                </label>
                {selected?.has_token && (
                  <label className="cf-checkbox">
                    <input
                      type="checkbox"
                      checked={clearToken}
                      onChange={(event) => setClearToken(event.target.checked)}
                    />
                    清除已保存 Token
                  </label>
                )}
                <p className="cf-help">凭证仅用于服务端连接，不在页面回显。</p>
              </section>
            </fieldset>
          </form>
          <section className="cf-card">
            <h2>
              已发现工具{" "}
              <span className="cf-count">{selected?.tools.length || 0}</span>
            </h2>
            <p className="cf-help">
              修改连接配置后，请先保存再测试。新工具默认停用。
            </p>
            {selected?.tools.length ? (
              <div className="cf-table-scroll">
                <table className="cf-table">
                  <thead>
                    <tr>
                      <th>工具名称</th>
                      <th>说明</th>
                      <th>启用</th>
                    </tr>
                  </thead>
                  <tbody>
                    {selected.tools.map((tool) => (
                      <tr key={tool.name}>
                        <td>
                          <strong>{tool.title}</strong>
                          <small>{tool.name}</small>
                        </td>
                        <td>{tool.description}</td>
                        <td>
                          <label className="cf-switch" title={tool.title}>
                            <input
                              type="checkbox"
                              role="switch"
                              aria-label={`启用工具 ${tool.title}`}
                              checked={tool.enabled}
                              disabled={
                                busy || !selected.enabled || hasUnsavedChanges
                              }
                              onChange={() =>
                                void action(async () => {
                                  await identityRequest(
                                    `/me/tools/${tool.name}`,
                                    "PATCH",
                                    { enabled: !tool.enabled },
                                    identity.csrf_token,
                                  );
                                  return selected;
                                }, "工具授权已更新。")
                              }
                            />
                            <span
                              className="cf-switch-track"
                              aria-hidden="true"
                            />
                          </label>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="cf-empty">保存服务器后，测试连接并发现远端工具。</p>
            )}
            <p className="cf-help">
              仅启用你信任的工具；仍需在助手或技能中授权，工具可能修改远端数据。
            </p>
          </section>
        </div>
        <aside className="cf-summary-rail">
          <section className="cf-card">
            <h2>连接摘要</h2>
            <div className="cf-identity">
              <span className="cf-avatar" aria-hidden="true">
                ♧
              </span>
              <strong>{draft.name || "未命名服务器"}</strong>
              <span
                className="cf-status"
                data-enabled={selected?.enabled ?? draft.enabled}
              >
                {!selected ? "未保存" : selected.enabled ? "已启用" : "已停用"}
              </span>
            </div>
            <dl className="cf-summary">
              <div>
                <dt>传输方式</dt>
                <dd>{draft.transport === "sse" ? "SSE" : "Streamable HTTP"}</dd>
              </div>
              <div>
                <dt>认证方式</dt>
                <dd>Bearer Token（可选）</dd>
              </div>
              <div>
                <dt>凭证状态</dt>
                <dd>
                  {clearToken
                    ? "待清除"
                    : token
                      ? "待保存"
                      : selected?.has_token
                        ? "已保存"
                        : "未设置"}
                </dd>
              </div>
              <div>
                <dt>已发现工具</dt>
                <dd>{selected?.tools.length || 0} 项</dd>
              </div>
              <div>
                <dt>工具已启用</dt>
                <dd>
                  {selected?.tools.filter((tool) => tool.enabled).length || 0}{" "}
                  项
                </dd>
              </div>
            </dl>
            <p className="cf-help">启用状态不代表当前连接可用。</p>
          </section>
          <section className="cf-card">
            <h2>保存与测试</h2>
            <p className="cf-help">保存配置后，再测试连接并同步远端工具。</p>
            <button
              className="cf-primary cf-wide"
              form="mcp-config-form"
              disabled={busy}
            >
              保存配置
            </button>
            <button
              className="cf-secondary cf-wide"
              disabled={
                !selected || busy || !selected.enabled || hasUnsavedChanges
              }
              onClick={discover}
            >
              测试连接并发现工具
            </button>
            {hasUnsavedChanges && (
              <p className="cf-help">请先保存修改，再测试连接。</p>
            )}
            <label className="cf-switch-row">
              启用服务器
              <input
                type="checkbox"
                role="switch"
                checked={selected?.enabled ?? draft.enabled}
                disabled={busy || (!!selected && hasUnsavedChanges)}
                onChange={(event) => {
                  if (!selected)
                    setDraft({ ...draft, enabled: event.target.checked });
                  else
                    void action(
                      () =>
                        identityRequest<MCPServer>(
                          `${path}/${selected.id}`,
                          "PATCH",
                          { enabled: !selected.enabled },
                          identity.csrf_token,
                        ),
                      "服务器状态已更新。",
                    );
                }}
              />
              <span className="cf-switch-track" aria-hidden="true" />
            </label>
            {selected && (
              <div className="cf-delete-server">
                <button
                  type="button"
                  className="cf-secondary cf-wide cf-danger"
                  aria-label={`删除服务器：${selected.name}`}
                  disabled={busy}
                  onClick={() => {
                    setNotice("");
                    setDeleteTarget(selected);
                  }}
                >
                  删除服务器
                </button>
              </div>
            )}
          </section>
        </aside>
      </div>
      <Dialog.Root
        open={!!deleteTarget}
        onOpenChange={(open) => {
          if (!busy && !open) setDeleteTarget(null);
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="agent-confirm-backdrop" />
          <Dialog.Content className="agent-confirm-dialog">
            <Dialog.Title className="agent-confirm-title">
              删除 MCP 服务器
            </Dialog.Title>
            <Dialog.Description className="agent-confirm-description">
              删除“{deleteTarget?.name}”后将撤销工具访问并清除凭证，是否继续？
            </Dialog.Description>
            {notice && (
              <p role={noticeIsError ? "alert" : "status"}>{notice}</p>
            )}
            <div className="agent-confirm-actions">
              <Dialog.Close asChild>
                <button disabled={busy}>取消</button>
              </Dialog.Close>
              <button
                className="agent-confirm-delete"
                disabled={busy}
                onClick={() => void deleteServer()}
              >
                确认删除
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </section>
  );
}
