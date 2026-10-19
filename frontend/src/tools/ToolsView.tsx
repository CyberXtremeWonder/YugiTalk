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
  const [notice, setNotice] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submissions, setSubmissions] = useState<
    { id: string; status: string; reason: string; config: ToolConfig }[]
  >([]);
  const alive = useRef(true);
  const path = `/me/tools`;
  const canManage = selected?.can_manage ?? true;
  const canPublish = canManage && identity.user.platform_admin;

  /** 重读当前空间目录，使删除和启停立即反映在页面上。 */
  async function refresh() {
    const items = await allItems<ToolEntry>(path);
    if (alive.current) setRows(items);
    return items;
  }
  useEffect(() => {
    alive.current = true;
    void refresh().catch((error) => {
      if (alive.current) setNotice(error.message);
    });
    void loadSubmissions().catch((error) => {
      if (alive.current) setNotice(error.message);
    });
    return () => {
      alive.current = false;
    };
    // 空间切换由父组件重建，防止草稿跨空间复用。
  }, []);

  /** 创建空草稿，不修改已经发布的工具。 */
  function reset() {
    setSubmitting(false);
    setSelected(null);
    setDraft(defaults);
    setParameters("[]");
    setSample("{}");
    setNotice("");
  }
  /** 从目录加载声明版本，内置工具只展示说明和参数结构。 */
  function open(row: ToolEntry) {
    setSubmitting(false);
    setSelected(row);
    setNotice("");
    setSample("{}");
    if (row.config) {
      setDraft(row.config);
      setParameters(JSON.stringify(row.config.parameters, null, 2));
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
      if (alive.current) {
        setSelected(items.find((row) => row.name === result.name) || result);
        setNotice(`Tool 已发布：${result.title} · v${result.number}`);
      }
    } catch (error) {
      if (alive.current)
        setNotice(error instanceof Error ? error.message : "保存工具失败");
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
      if (alive.current) {
        reset();
        setNotice("工具已提交审核，批准后才会加入工具目录。");
      }
    } catch (error) {
      if (alive.current)
        setNotice(error instanceof Error ? error.message : "提交失败");
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
        setNotice(error instanceof Error ? error.message : "更新工具失败");
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
        setNotice(error instanceof Error ? error.message : "参数校验失败");
    } finally {
      if (alive.current) setBusy(false);
    }
  }

  return (
    <section className="secondary-view" aria-labelledby="view-title">
      <h1 id="view-title">Tool</h1>
      <p>
        统一管理工具，在 Agent 和 Skill 中按需授权。当前支持内置工具与只读 HTTPS
        JSON 查询。
      </p>
      <div className="agent-layout tool-layout">
        <div className="agent-list">
          <button
            disabled={busy}
            onClick={() => {
              reset();
              setSubmitting(true);
            }}
          >
            ＋ 提交工具审核
          </button>
          {canPublish && (
            <button disabled={busy} onClick={reset}>
              ＋ 新工具
            </button>
          )}
          {rows.map((row) => (
            <button
              key={row.name}
              disabled={busy}
              aria-pressed={selected?.name === row.name}
              onClick={() => open(row)}
            >
              {row.title}
              <small>
                {row.builtin ? "内置" : `HTTP · v${row.number}`} ·{" "}
                {row.owner_id ? "个人 · " : "空间 · "}
                {row.enabled ? "已启用" : "已停用"}
                {!row.ready ? " · 待配置" : ""}
              </small>
            </button>
          ))}
          {!!submissions.length && (
            <section aria-label="我的工具提交">
              <h2>我的提交</h2>
              {submissions.map((item) => (
                <div key={item.id}>
                  <strong>{item.config.title}</strong>
                  <p>
                    {item.status === "pending"
                      ? "待审核"
                      : item.status === "approved"
                        ? "已批准"
                        : "已驳回"}
                    {item.reason ? ` · ${item.reason}` : ""}
                  </p>
                </div>
              ))}
            </section>
          )}
        </div>
        <div className="account-settings agent-form tool-editor">
          {selected?.builtin ? (
            <>
              <h2>{selected.title}</h2>
              <p>{selected.description}</p>
              <p>内置工具保留服务端实现，可启停，不可删除或覆盖。</p>
              <details>
                <summary>参数结构</summary>
                <pre>{JSON.stringify(selected.schema.parameters, null, 2)}</pre>
              </details>
              {canManage && (
                <button disabled={busy} onClick={() => void changeState()}>
                  {selected.enabled ? "停用 Tool" : "启用 Tool"}
                </button>
              )}
            </>
          ) : (
            <form
              onSubmit={(event) => {
                event.preventDefault();
                if (submitting) void submitReview();
                else void save();
              }}
            >
              <fieldset disabled={busy || (!canPublish && !submitting)}>
                <label>
                  工具标识
                  <input
                    required
                    pattern="[a-z][a-z0-9_]*"
                    maxLength={64}
                    readOnly={!!selected}
                    value={draft.name}
                    placeholder="例如：public_lookup"
                    onChange={(e) =>
                      setDraft({ ...draft, name: e.target.value })
                    }
                  />
                </label>
                <label>
                  显示名称
                  <input
                    required
                    maxLength={80}
                    value={draft.title}
                    onChange={(e) =>
                      setDraft({ ...draft, title: e.target.value })
                    }
                  />
                </label>
                <label>
                  用途说明
                  <textarea
                    required
                    maxLength={500}
                    value={draft.description}
                    onChange={(e) =>
                      setDraft({ ...draft, description: e.target.value })
                    }
                  />
                </label>
                <label>
                  HTTPS 查询地址
                  <input
                    required
                    type="url"
                    maxLength={2048}
                    value={draft.endpoint}
                    placeholder="https://api.example.com/search"
                    onChange={(e) =>
                      setDraft({ ...draft, endpoint: e.target.value })
                    }
                  />
                </label>
                <label>
                  参数定义（JSON 数组）
                  <textarea
                    className="tool-json"
                    aria-label="参数定义（JSON 数组）"
                    value={parameters}
                    maxLength={10000}
                    placeholder={
                      '[{"name":"q","type":"string","description":"查询关键词","required":true}]'
                    }
                    onChange={(e) => setParameters(e.target.value)}
                  />
                </label>
                <label>
                  密钥引用名称（可选）
                  <input
                    maxLength={64}
                    value={draft.secret_name}
                    placeholder="只填写服务端配置名称，不填写密钥"
                    onChange={(e) =>
                      setDraft({ ...draft, secret_name: e.target.value })
                    }
                  />
                </label>
                <label>
                  查询超时（秒）
                  <input
                    required
                    type="number"
                    min={1}
                    max={20}
                    value={draft.timeout_seconds}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        timeout_seconds: Number(e.target.value),
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
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        max_response_bytes: Number(e.target.value),
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
                    <label className="tool-checkbox" key={role}>
                      <input
                        type="checkbox"
                        checked={draft.required_roles.includes(role)}
                        onChange={(e) =>
                          setDraft({
                            ...draft,
                            required_roles: e.target.checked
                              ? [...draft.required_roles, role]
                              : draft.required_roles.filter(
                                  (item) => item !== role,
                                ),
                          })
                        }
                      />
                      {title}
                    </label>
                  ))}
                </fieldset>
              </fieldset>
              {(canPublish || submitting || (canManage && selected)) && (
                <div className="skill-actions">
                  {(canPublish || submitting) && (
                    <button disabled={busy} type="submit">
                      {submitting
                        ? "提交审核"
                        : selected
                          ? "发布新版本"
                          : "创建 Tool"}
                    </button>
                  )}
                  {selected && (
                    <>
                      <button
                        disabled={busy}
                        type="button"
                        onClick={() => void changeState()}
                      >
                        {selected.enabled ? "停用 Tool" : "启用 Tool"}
                      </button>
                      <button
                        disabled={busy}
                        type="button"
                        onClick={() => void changeState(true)}
                      >
                        删除 Tool
                      </button>
                    </>
                  )}
                </div>
              )}
            </form>
          )}
          {selected?.kind === "http" && (
            <>
              {selected.configuration_note && (
                <p className="field-hint">
                  {selected.configuration_note}
                  。配置就绪后才会向模型提供此工具。
                </p>
              )}
              {canManage && !selected.builtin && (
                <div className="tool-validation">
                  <label>
                    示例参数（仅校验）
                    <textarea
                      aria-label="示例参数（仅校验）"
                      value={sample}
                      maxLength={8000}
                      onChange={(e) => setSample(e.target.value)}
                    />
                  </label>
                  <button disabled={busy} onClick={() => void validate()}>
                    校验参数
                  </button>
                </div>
              )}
            </>
          )}
          {!canManage && <p>只有空间所有者和管理员可以维护工具。</p>}
          <p className="field-hint">
            停用或删除将撤销后续调用。工具本身不授予权限，仍需同时通过
            Agent、Skill 与空间角色授权。
          </p>
          {notice && <p role="status">{notice}</p>}
        </div>
      </div>
    </section>
  );
}
