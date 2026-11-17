import { useNotice } from "../shared/useNotice";
import "../shared/configuration.css";
import { useEffect, useRef, useState } from "react";
import { allItems, identityRequest, type Identity } from "../identity/api";
import type { Account } from "../accounts/api";
import {
  parseParameters,
  type ToolConfig,
  type ToolEntry,
  type ToolRole,
} from "./api";

const defaults: ToolConfig = {
  name: "",
  title: "",
  description: "",
  endpoint: "",
  method: "GET",
  parameters: [],
  required_roles: ["owner", "admin", "member"],
  secret_name: "",
  timeout_seconds: 10,
  max_response_bytes: 8000,
};

/** 管理工具目录、不可变查询版本和启停，校验参数时不访问外部网络。 */
export function ToolsView({
  identity,
}: {
  identity: Identity;
  account: Account;
}) {
  const [rows, setRows] = useState<ToolEntry[]>([]);
  const [selected, setSelected] = useState<ToolEntry | null>(null);
  const [draft, setDraft] = useState<ToolConfig>(defaults);
  const [parameters, setParameters] = useState("[]");
  const [sample, setSample] = useState("{}");
  const [busy, setBusy] = useState(false);
  const [search, setSearch] = useState("");
  const [sourceFilter, setSourceFilter] = useState("all");
  const [tab, setTab] = useState("config");
  const [showSubmissions, setShowSubmissions] = useState(false);
  const { notice, setNotice, noticeIsError, reportError } = useNotice();
  const [submitting, setSubmitting] = useState(false);
  const [submissions, setSubmissions] = useState<
    { id: string; status: string; reason: string; config: ToolConfig }[]
  >([]);
  const alive = useRef(true);
  const path = `/me/tools`;
  const canManage = selected?.can_manage ?? true;
  const canPublish =
    canManage &&
    identity.user.platform_admin &&
    !selected?.builtin &&
    selected?.kind !== "mcp";

  /** 重读当前空间目录，使删除和启停立即反映在页面上。 */
  async function refresh() {
    const items = await allItems<ToolEntry>(path);
    if (alive.current) setRows(items);
    return items;
  }
  useEffect(() => {
    alive.current = true;
    void refresh()
      .then((items) => {
        if (alive.current && items[0]) open(items[0]);
      })
      .catch((error) => {
        if (alive.current) reportError(error.message);
      });
    void loadSubmissions().catch((error) => {
      if (alive.current) reportError(error.message);
    });
    return () => {
      alive.current = false;
    };
    // 空间切换由父组件重建，防止草稿跨空间复用。
  }, []);

  /** 创建空草稿，不修改已经发布的工具。 */
  function reset() {
    setTab("config");
    setSubmitting(false);
    setSelected(null);
    setDraft(defaults);
    setParameters("[]");
    setSample("{}");
    setNotice("");
  }
  /** 从目录加载声明版本，内置工具只展示说明和参数结构。 */
  function open(row: ToolEntry) {
    setTab("config");
    setSubmitting(false);
    setSelected(row);
    setNotice("");
    setSample("{}");
    if (row.config) {
      // 目录配置与参数编辑文本分别保存；只有提交时才把 JSON 文本解析回结构化参数。
      setDraft(row.config);
      setParameters(JSON.stringify(row.config.parameters, null, 2));
    } else {
      setDraft({
        ...defaults,
        name: row.name,
        title: row.title,
        description: row.description,
      });
      setParameters("[]");
    }
  }
  /** 发布新版本，仅保存声明和密钥引用名称，不提交真实凭证。 */
  async function save() {
    setBusy(true);
    setNotice("");
    try {
      const result = await identityRequest<ToolEntry>(
        selected ? `${path}/${selected.name}/versions` : path,
        "POST",
        {
          ...draft,
          parameters: parseParameters(parameters),
          enabled: selected?.enabled ?? true,
        },
        identity.csrf_token,
      );
      const items = await refresh();
      // 发布响应确认成功后再取最新目录，使版本号、权限与就绪说明保持服务端口径。
      if (alive.current) {
        setSelected(items.find((row) => row.name === result.name) || result);
        setNotice(`Tool 已发布：${result.title} · v${result.number}`);
      }
    } catch (error) {
      if (alive.current)
        reportError(error instanceof Error ? error.message : "保存工具失败");
    } finally {
      if (alive.current) setBusy(false);
    }
  }
  /** 读取本人提交和审核意见，其他用户的提交不进入当前列表。 */
  async function loadSubmissions() {
    const result = await identityRequest<{
      items: {
        id: string;
        status: string;
        reason: string;
        config: ToolConfig;
      }[];
    }>(`/me/tool-submissions?limit=100`);
    if (alive.current) setSubmissions(result.items);
  }
  /** 保存待审声明，提交不会创建可调用工具或授予助手权限。 */
  async function submitReview() {
    setBusy(true);
    setNotice("");
    try {
      await identityRequest(
        `/me/tool-submissions`,
        "POST",
        { ...draft, parameters: parseParameters(parameters), enabled: false },
        identity.csrf_token,
      );
      await loadSubmissions();
      // 待审声明不会立刻进入工具目录；刷新的是审核记录，之后清空当前提交草稿。
      if (alive.current) {
        reset();
        setNotice("工具已提交审核，批准后才会加入工具目录。");
      }
    } catch (error) {
      if (alive.current)
        reportError(error instanceof Error ? error.message : "提交失败");
    } finally {
      if (alive.current) setBusy(false);
    }
  }
  /** 启停或软删除工具；删除后清空草稿，内置工具不可删除。 */
  async function changeState(remove = false) {
    if (!selected) return;
    setBusy(true);
    setNotice("");
    try {
      const result = await identityRequest<ToolEntry>(
        `${path}/${selected.name}`,
        remove ? "DELETE" : "PATCH",
        remove ? undefined : { enabled: !selected.enabled },
        identity.csrf_token,
      );
      const items = await refresh();
      if (!alive.current) return;
      if (remove) reset();
      else setSelected(items.find((row) => row.name === result.name) || result);
      setNotice(
        remove
          ? "Tool 已删除。"
          : selected.enabled
            ? "Tool 已停用。"
            : "Tool 已启用。",
      );
    } catch (error) {
      if (alive.current)
        reportError(error instanceof Error ? error.message : "更新工具失败");
    } finally {
      if (alive.current) setBusy(false);
    }
  }
  /** 使用服务端当前版本检查示例输入，不调用真实工具或付费模型。 */
  async function validate() {
    if (!selected) return;
    setBusy(true);
    setNotice("");
    try {
      const args: unknown = JSON.parse(sample);
      // 先检查 JSON 外形，再交给服务端当前已发布版本校验字段；此按钮不会执行草稿端点。
      if (!args || typeof args !== "object" || Array.isArray(args))
        throw new Error("示例参数应为 JSON 对象。");
      await identityRequest(
        `${path}/${selected.name}/validate`,
        "POST",
        args,
        identity.csrf_token,
      );
      if (alive.current) setNotice("参数校验通过；未调用外部服务。");
    } catch (error) {
      if (alive.current)
        reportError(error instanceof Error ? error.message : "参数校验失败");
    } finally {
      if (alive.current) setBusy(false);
    }
  }

  async function toggleRow(row: ToolEntry) {
    if (busy || !row.can_manage) return;
    setBusy(true);
    setNotice("");
    try {
      await identityRequest(
        `${path}/${row.name}`,
        "PATCH",
        { enabled: !row.enabled },
        identity.csrf_token,
      );
      const items = await refresh();
      if (alive.current && selected?.name === row.name)
        setSelected(items.find((item) => item.name === row.name) || row);
      if (alive.current) setNotice("工具启用状态已更新。");
    } catch (error) {
      if (alive.current)
        reportError(error instanceof Error ? error.message : "更新工具失败");
    } finally {
      if (alive.current) setBusy(false);
    }
  }
  const filters = [
    ["all", "全部"],
    ["builtin", "内置"],
    ["http", "HTTP"],
    ["mcp", "MCP"],
  ];
  const filtered = rows.filter(
    (row) =>
      (sourceFilter === "all" || row.kind === sourceFilter) &&
      `${row.name} ${row.title}`
        .toLowerCase()
        .includes(search.trim().toLowerCase()),
  );
  let parameterRows: ToolConfig["parameters"] = [];
  try {
    parameterRows = parseParameters(parameters).filter(
      (parameter) =>
        parameter &&
        typeof parameter.name === "string" &&
        typeof parameter.type === "string",
    );
  } catch {
    /* 编辑中的 JSON 在发布时显示明确校验错误。 */
  }
  return (
    <section
      className="secondary-view configuration-workspace"
      aria-labelledby="view-title"
    >
      <header className="cf-heading">
        <div>
          <h1 id="view-title">Tool</h1>
          <p>统一管理助手可使用的工具。</p>
        </div>
        <div className="cf-actions">
          <button
            className="cf-secondary"
            onClick={() => setShowSubmissions((value) => !value)}
          >
            我的提交
          </button>
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
      {showSubmissions && (
        <section className="cf-card cf-submissions" aria-label="我的工具提交">
          <h2>我的提交</h2>
          {submissions.length ? (
            submissions.map((item) => (
              <article key={item.id}>
                <strong>{item.config.title}</strong>
                <p>
                  {item.status === "pending"
                    ? "待审核"
                    : item.status === "approved"
                      ? "已批准"
                      : "已驳回"}
                  {item.reason ? ` · ${item.reason}` : ""}
                </p>
              </article>
            ))
          ) : (
            <p className="cf-help">尚无工具提交。</p>
          )}
          {identity.user.platform_admin && (
            <button
              className="cf-link"
              disabled={busy}
              onClick={() => {
                reset();
                setSubmitting(true);
              }}
            >
              提交工具审核
            </button>
          )}
        </section>
      )}
      <div className="cf-tool-layout">
        <section className="cf-card cf-tool-directory" aria-label="工具目录">
          <div className="cf-card-title">
            <h2>
              工具目录 <span className="cf-count">{rows.length}</span>
            </h2>
            <input
              className="cf-search"
              aria-label="搜索工具名称"
              placeholder="搜索工具名称…"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>
          <div className="cf-direct-actions cf-create-actions">
            <button
              className="cf-secondary"
              disabled={busy}
              onClick={() => {
                reset();
                setSubmitting(true);
              }}
            >
              ＋ 提交工具审核
            </button>
            {identity.user.platform_admin && (
              <button className="cf-primary" disabled={busy} onClick={reset}>
                ＋ 新工具
              </button>
            )}
          </div>
          <div className="cf-filters" aria-label="工具来源筛选">
            {filters.map(([key, title]) => (
              <button
                key={key}
                aria-pressed={sourceFilter === key}
                onClick={() => setSourceFilter(key)}
              >
                {title}{" "}
                <span>
                  {key === "all"
                    ? rows.length
                    : rows.filter((row) => row.kind === key).length}
                </span>
              </button>
            ))}
          </div>
          <div className="cf-table-scroll">
            <table className="cf-table cf-tool-table">
              <thead>
                <tr>
                  <th>工具名称</th>
                  <th>来源</th>
                  <th>配置</th>
                  <th>启用</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((row) => (
                  <tr
                    key={row.name}
                    data-selected={selected?.name === row.name}
                  >
                    <td>
                      <button
                        className="cf-table-select"
                        disabled={busy}
                        onClick={() => open(row)}
                      >
                        {row.title}
                        <small>{row.name}</small>
                      </button>
                    </td>
                    <td>
                      <span className={`cf-source ${row.kind}`}>
                        {row.kind === "builtin"
                          ? "内置"
                          : row.kind === "http"
                            ? "HTTP"
                            : "MCP"}
                      </span>
                    </td>
                    <td>
                      <span className="cf-ready" data-ready={row.ready}>
                        {row.ready ? "配置就绪" : "待配置"}
                      </span>
                    </td>
                    <td>
                      <label className="cf-switch">
                        <input
                          type="checkbox"
                          role="switch"
                          aria-label={`启用工具 ${row.title}`}
                          checked={row.enabled}
                          disabled={busy || !row.can_manage}
                          onChange={() => void toggleRow(row)}
                        />
                        <span className="cf-switch-track" aria-hidden="true" />
                      </label>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!filtered.length && <p className="cf-empty">没有匹配的工具。</p>}
          <footer className="cf-directory-foot">
            <span>共 {rows.length} 个工具</span>
            <span>内置工具不可删除；MCP 连接在 MCP 页面管理。</span>
          </footer>
        </section>
        <section className="cf-card cf-tool-editor" aria-label="工具配置">
          <div className="cf-card-title">
            <div className="cf-identity">
              <span className="cf-avatar teal" aria-hidden="true">
                ▤
              </span>
              <strong>
                {selected?.title ||
                  (submitting ? "提交查询工具" : "新增查询工具")}
              </strong>
              {selected && (
                <span className="cf-tag">
                  {selected.kind === "builtin"
                    ? "内置"
                    : selected.kind === "mcp"
                      ? "MCP"
                      : "HTTP 查询"}{" "}
                  · v{selected.number}
                </span>
              )}
            </div>
          </div>
          {selected && <p className="cf-help">{selected.description}</p>}
          {selected?.kind === "mcp" || selected?.builtin ? (
            <>
              <p className="cf-help">
                {selected.builtin
                  ? "内置工具保留服务端实现，可启停，不可删除或覆盖。"
                  : "MCP 工具由远端服务器提供；连接和凭证在 MCP 页面维护。"}
              </p>
              {selected.kind === "mcp" && (
                <a className="cf-link" href="#mcp">
                  前往 MCP 管理 →
                </a>
              )}
              <details className="cf-schema">
                <summary>参数结构</summary>
                <pre>{JSON.stringify(selected.schema.parameters, null, 2)}</pre>
              </details>
              {canManage && (
                <button
                  className="cf-secondary"
                  disabled={busy}
                  onClick={() => void changeState()}
                >
                  {selected.enabled ? "停用 Tool" : "启用 Tool"}
                </button>
              )}
            </>
          ) : (
            <>
              <div className="cf-tabs" aria-label="工具配置分区">
                {[
                  ["config", "配置"],
                  ["parameters", "参数"],
                  ...(selected?.kind === "http" && canManage
                    ? [["validate", "校验"]]
                    : []),
                ].map(([key, title]) => (
                  <button
                    key={key}
                    type="button"
                    aria-pressed={tab === key}
                    onClick={() => setTab(key)}
                  >
                    {title}
                  </button>
                ))}
              </div>
              <form
                id="tool-config-form"
                noValidate
                onSubmit={(event) => {
                  event.preventDefault();
                  if (busy || (!canPublish && !submitting)) return;
                  const invalid = event.currentTarget.querySelector<
                    HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement
                  >("input:invalid,select:invalid,textarea:invalid");
                  if (invalid) {
                    setTab(
                      invalid.closest<HTMLElement>("[data-config-tab]")?.dataset
                        .configTab || "config",
                    );
                    let parent = invalid.parentElement;
                    while (parent && parent !== event.currentTarget) {
                      if (parent instanceof HTMLDetailsElement)
                        parent.open = true;
                      parent = parent.parentElement;
                    }
                    requestAnimationFrame(() => {
                      invalid.focus();
                      invalid.reportValidity();
                    });
                    return;
                  }
                  if (submitting) void submitReview();
                  else void save();
                }}
              >
                <fieldset disabled={busy || (!canPublish && !submitting)}>
                  <div data-config-tab="config" hidden={tab !== "config"}>
                    <div className="cf-form-pair">
                      <label>
                        工具标识
                        <input
                          required
                          pattern="[a-z][a-z0-9_]*"
                          maxLength={64}
                          readOnly={!!selected}
                          value={draft.name}
                          placeholder="例如 public_lookup"
                          onChange={(event) =>
                            setDraft({ ...draft, name: event.target.value })
                          }
                        />
                      </label>
                      <label>
                        显示名称
                        <input
                          required
                          maxLength={80}
                          value={draft.title}
                          onChange={(event) =>
                            setDraft({ ...draft, title: event.target.value })
                          }
                        />
                      </label>
                    </div>
                    <label>
                      用途说明
                      <textarea
                        required
                        maxLength={500}
                        rows={2}
                        value={draft.description}
                        onChange={(event) =>
                          setDraft({
                            ...draft,
                            description: event.target.value,
                          })
                        }
                      />
                    </label>
                    <div className="cf-form-pair">
                      <label>
                        请求方式
                        <select
                          aria-label="请求方式"
                          value={draft.method}
                          onChange={(event) =>
                            setDraft({
                              ...draft,
                              method: event.target
                                .value as ToolConfig["method"],
                            })
                          }
                        >
                          <option value="GET">GET</option>
                          <option value="POST">POST</option>
                        </select>
                      </label>
                      <label>
                        查询超时（秒）
                        <input
                          required
                          type="number"
                          min={1}
                          max={20}
                          value={draft.timeout_seconds}
                          onChange={(event) =>
                            setDraft({
                              ...draft,
                              timeout_seconds: Number(event.target.value),
                            })
                          }
                        />
                      </label>
                    </div>
                    <label>
                      HTTPS 查询地址
                      <input
                        required
                        type="url"
                        maxLength={2048}
                        value={draft.endpoint}
                        placeholder="https://api.example.com/search"
                        onChange={(event) =>
                          setDraft({ ...draft, endpoint: event.target.value })
                        }
                      />
                    </label>
                    <details className="cf-advanced">
                      <summary>
                        高级配置 <small>密钥引用、角色与响应限制</small>
                      </summary>
                      <label>
                        密钥引用名称（可选）
                        <input
                          maxLength={64}
                          value={draft.secret_name}
                          placeholder="只填写服务端配置名称，不填写密钥"
                          onChange={(event) =>
                            setDraft({
                              ...draft,
                              secret_name: event.target.value,
                            })
                          }
                        />
                      </label>
                      <label>
                        响应容量（字节）
                        <input
                          required
                          type="number"
                          min={256}
                          max={16000}
                          value={draft.max_response_bytes}
                          onChange={(event) =>
                            setDraft({
                              ...draft,
                              max_response_bytes: Number(event.target.value),
                            })
                          }
                        />
                      </label>
                      <fieldset>
                        <legend>可调用的空间角色</legend>
                        {(
                          [
                            ["owner", "所有者"],
                            ["admin", "空间管理员"],
                            ["member", "普通成员"],
                          ] as [ToolRole, string][]
                        ).map(([role, title]) => (
                          <label className="cf-checkbox" key={role}>
                            <input
                              type="checkbox"
                              checked={draft.required_roles.includes(role)}
                              onChange={(event) =>
                                setDraft({
                                  ...draft,
                                  required_roles: event.target.checked
                                    ? [...draft.required_roles, role]
                                    : draft.required_roles.filter(
                                        (value) => value !== role,
                                      ),
                                })
                              }
                            />
                            {title}
                          </label>
                        ))}
                      </fieldset>
                    </details>
                  </div>
                  <div
                    data-config-tab="parameters"
                    hidden={tab !== "parameters"}
                  >
                    <h3>参数定义</h3>
                    <label>
                      参数定义（JSON 数组）
                      <textarea
                        aria-label="参数定义（JSON 数组）"
                        className="cf-code"
                        value={parameters}
                        maxLength={10000}
                        rows={9}
                        onChange={(event) => setParameters(event.target.value)}
                      />
                    </label>
                    <p className="cf-help">
                      每项包含
                      name、type、description、required。发布前会检查格式。
                    </p>
                  </div>
                </fieldset>
              </form>
              {tab !== "validate" && (
                <div className="cf-divider">
                  <div className="cf-card-title">
                    <h3>输入参数</h3>
                    <button
                      className="cf-link"
                      onClick={() => setTab("parameters")}
                    >
                      编辑参数
                    </button>
                  </div>
                  <table className="cf-table">
                    <thead>
                      <tr>
                        <th>名称</th>
                        <th>类型</th>
                        <th>必填</th>
                      </tr>
                    </thead>
                    <tbody>
                      {parameterRows.map((parameter) => (
                        <tr key={parameter.name}>
                          <td>{parameter.name}</td>
                          <td>{parameter.type}</td>
                          <td>{parameter.required ? "是" : "否"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {!parameterRows.length && (
                    <p className="cf-help">暂无有效参数定义。</p>
                  )}
                </div>
              )}
              {tab === "validate" && selected?.kind === "http" && canManage && (
                <div className="cf-validation">
                  <label>
                    示例参数（仅校验）
                    <textarea
                      aria-label="示例参数（仅校验）"
                      className="cf-code"
                      rows={2}
                      value={sample}
                      maxLength={8000}
                      onChange={(event) => setSample(event.target.value)}
                    />
                  </label>
                  <button
                    type="button"
                    className="cf-secondary"
                    disabled={busy}
                    onClick={() => void validate()}
                  >
                    校验参数
                  </button>
                  <p className="cf-help">
                    检查当前已发布版本的参数格式，不执行工具。
                  </p>
                </div>
              )}
              {(canPublish || submitting) && (
                <button
                  className="cf-primary cf-wide"
                  form="tool-config-form"
                  disabled={busy}
                >
                  {busy
                    ? "提交中…"
                    : submitting
                      ? "提交审核"
                      : selected
                        ? "发布新版本"
                        : "创建 Tool"}
                </button>
              )}
              {selected?.kind === "http" && !selected.builtin && canManage && (
                <div className="cf-direct-actions">
                  <button
                    className="cf-secondary"
                    disabled={busy}
                    onClick={() => void changeState()}
                  >
                    {selected.enabled ? "停用 Tool" : "启用 Tool"}
                  </button>
                  <button
                    className="cf-secondary cf-danger"
                    disabled={busy}
                    onClick={() => void changeState(true)}
                  >
                    删除 Tool
                  </button>
                </div>
              )}
              {!canPublish && !submitting && (
                <p className="cf-help">
                  当前没有直接发布权限。可通过“提交工具审核”创建待审声明。
                </p>
              )}
            </>
          )}
          {selected?.configuration_note && (
            <p className="cf-help">{selected.configuration_note}</p>
          )}
          <p className="cf-help">
            停用或删除将撤销后续调用；仍需同时通过助手、技能及空间角色授权。
          </p>
        </section>
      </div>
    </section>
  );
}
