import { useNotice } from "../shared/useNotice";
import { ToolChoices } from "../tools/ToolChoices";
import { useCallback, useEffect, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { allItems, identityRequest, type Identity } from "../identity/api";
import { type Account } from "../accounts/api";
import type { Agent } from "./api";
import type { ToolEntry } from "../tools/api";
import "./agents.css";

const toolNames: Record<string, string> = {
  text_stats: "文本统计",
  format_report: "报告排版",
  weather_query: "天气查询",
  traffic_query: "路况查询",
  web_search: "网页搜索",
};

/** 编辑并发布助手版本，展示模型能力和有界执行配置。 */
export function AgentsView({
  identity,
  account,
}: {
  identity: Identity;
  account: Account;
}) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selected, setSelected] = useState<Agent | null>(null);
  const [name, setName] = useState("通用助手");
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const keepsKey = Boolean(
    // 服务端不回显密钥；只有连接目标未变时，留空才表示沿用已保存的密钥。
    selected?.config.has_api_key &&
    provider === selected.config.provider &&
    baseUrl.trim().replace(/\/+$/, "") === selected.config.base_url,
  );
  const [prompt, setPrompt] = useState(
    "你是可靠的中文助手。根据任务选择授权工具，清晰回答并说明不确定性。",
  );
  const [tools, setTools] = useState([
    "text_stats",
    "format_report",
    "weather_query",
    "traffic_query",
    "web_search",
  ]);
  const [closedLoop, setClosedLoop] = useState(false);
  const [budget, setBudget] = useState(30000);
  const defaults = {
    max_steps: 6,
    max_tool_calls: 10,
    max_output_tokens: 2048,
    timeout_seconds: 120,
  };
  const [limits, setLimits] = useState(defaults);
  const [busy, setBusy] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Agent | null>(null);
  const [search, setSearch] = useState("");
  const [tab, setTab] = useState("basic");
  const [toolCatalog, setToolCatalog] = useState<ToolEntry[]>([]);
  const cancelDelete = useRef<HTMLButtonElement>(null);
  const deleteTrigger = useRef<HTMLButtonElement>(null);
  const newAgent = useRef<HTMLButtonElement>(null);
  const { notice, setNotice, noticeIsError, reportError } = useNotice();
  const path = `/me/agents`;
  const editAgent = useCallback((agent: Agent) => {
    setSelected(agent);
    setName(agent.name);
    setProvider(
      agent.config.base_url === undefined ? "" : agent.config.provider,
    );
    setModel(agent.config.model);
    setBaseUrl(agent.config.base_url ?? "");
    setApiKey("");
    setPrompt(agent.config.system_prompt);
    setTools(agent.config.tools);
    setClosedLoop(agent.config.closed_loop ?? false);
    setBudget(agent.config.token_budget);
    setLimits({
      max_steps: agent.config.max_steps,
      max_tool_calls: agent.config.max_tool_calls,
      max_output_tokens: agent.config.max_output_tokens,
      timeout_seconds: agent.config.timeout_seconds,
    });
    setTab("basic");
  }, []);
  useEffect(() => {
    let active = true;
    void allItems<Agent>(path)
      .then((rows) => {
        if (active) {
          setAgents(rows);
          if (rows[0]) editAgent(rows[0]);
        }
      })
      .catch((error) => {
        if (active) reportError(error.message);
      });
    return () => {
      active = false;
    };
  }, [path, editAgent]);
  /** 清空已选助手和草稿，删除后不保留旧助手配置。 */
  function resetAgent() {
    setTab("basic");
    setNotice("");
    setSelected(null);
    setName("通用助手");
    setProvider("");
    setModel("");
    setBaseUrl("");
    setApiKey("");
    setPrompt("你是可靠的中文助手。");
    setTools([
      "text_stats",
      "format_report",
      "weather_query",
      "traffic_query",
      "web_search",
    ]);
    setClosedLoop(false);
    setBudget(30000);
    setLimits(defaults);
  }
  /** 确认删除所选助手，历史会话和任务保持可查。 */
  async function removeAgent() {
    if (!deleteTarget || busy) return;
    const target = deleteTarget;
    setBusy(true);
    setNotice("");
    try {
      await identityRequest(
        `/agents/${target.id}`,
        "DELETE",
        undefined,
        identity.csrf_token,
      );
      setAgents((rows) => rows.filter((agent) => agent.id !== target.id));
      setDeleteOpen(false);
      if (selected?.id === target.id) resetAgent();
      setNotice("助手已删除，历史会话和任务已保留。");
    } catch (error) {
      reportError(error instanceof Error ? error.message : "删除助手失败");
    } finally {
      setBusy(false);
    }
  }
  const titleForTool = (key: string) =>
    toolCatalog.find((tool) => tool.name === key)?.title ||
    toolNames[key] ||
    key;
  return (
    <Dialog.Root
      open={deleteOpen}
      onOpenChange={(open) => {
        if (!busy) {
          setDeleteOpen(open);
          if (open) setNotice("");
        }
      }}
    >
      <section
        className="secondary-view agents-workspace"
        aria-labelledby="view-title"
      >
        <header className="av-heading">
          <div>
            <h1 id="view-title">Agent</h1>
            <p>为不同任务，配置合适的助手。</p>
          </div>
        </header>
        {notice && (
          <p
            className={`av-notice ${noticeIsError ? "av-error" : ""}`}
            role={noticeIsError ? "alert" : "status"}
          >
            {notice}
          </p>
        )}
        <div className="av-layout">
          <aside className="av-assistants" aria-label="我的助手">
            <h2>
              我的助手 <span>{agents.length}</span>
            </h2>
            <button
              className="av-primary"
              ref={newAgent}
              disabled={busy}
              onClick={resetAgent}
            >
              ＋ 新助手
            </button>
            <label className="av-search">
              <span aria-hidden="true">⌕</span>
              <input
                aria-label="搜索助手"
                placeholder="搜索助手…"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
            </label>
            <div className="av-agent-items">
              {agents
                .filter((agent) =>
                  agent.name
                    .toLowerCase()
                    .includes(search.trim().toLowerCase()),
                )
                .map((agent, index) => (
                  <div
                    className="av-agent-entry"
                    key={agent.id}
                    data-selected={selected?.id === agent.id}
                  >
                    <button
                      className="av-select-agent"
                      disabled={busy}
                      aria-pressed={selected?.id === agent.id}
                      onClick={() => {
                        editAgent(agent);
                        setNotice("");
                      }}
                    >
                      <span
                        className="av-avatar"
                        data-color={index % 3}
                        aria-hidden="true"
                      >
                        <svg viewBox="0 0 24 24">
                          <path d="M8 7h8a4 4 0 0 1 4 4v8H4v-8a4 4 0 0 1 4-4ZM12 3v4M8 12h.01M16 12h.01M9 16h6" />
                        </svg>
                      </span>
                      <span className="av-agent-label">
                        <strong>{agent.name}</strong>
                        <small>已发布 · 版本 {agent.number}</small>
                      </span>
                    </button>
                  </div>
                ))}
              {!agents.length && (
                <p className="av-empty">还没有助手，先创建一个。</p>
              )}
              {!!agents.length &&
                !agents.some((agent) =>
                  agent.name
                    .toLowerCase()
                    .includes(search.trim().toLowerCase()),
                ) && <p className="av-empty">没有匹配的助手。</p>}
            </div>
          </aside>
          <form
            id="agent-config-form"
            className="av-editor"
            noValidate
            onSubmit={(event) => {
              event.preventDefault();
              if (busy) return;
              const invalid = event.currentTarget.querySelector<
                HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement
              >(":invalid");
              if (invalid) {
                setTab(
                  invalid.closest<HTMLElement>("[data-agent-panel]")?.dataset
                    .agentPanel || "basic",
                );
                requestAnimationFrame(() => {
                  invalid.focus();
                  invalid.reportValidity();
                });
                return;
              }
              setBusy(true);
              setNotice("");
              const targetId = selected?.id;
              const config = {
                name: name.trim(),
                ...(provider
                  ? { provider, model, base_url: baseUrl, api_key: apiKey }
                  : {}),
                system_prompt: prompt,
                closed_loop: closedLoop,
                tools,
                token_budget: budget,
                ...limits,
              };
              void identityRequest<{ id?: string }>(
                selected ? `/agents/${selected.id}/versions` : path,
                "POST",
                config,
                identity.csrf_token,
              )
                .then(async (published) => {
                  const rows = await allItems<Agent>(path);
                  setAgents(rows);
                  const fresh = rows.find(
                    (agent) => agent.id === (targetId || published.id),
                  );
                  if (fresh) editAgent(fresh);
                  setApiKey("");
                  setNotice("版本发布成功，可以回到对话页选择助手。");
                })
                .catch((error) => reportError(error.message))
                .finally(() => setBusy(false));
            }}
          >
            <div className="av-tabs" aria-label="助手配置分区">
              {[
                ["basic", "基础配置"],
                ["tools", "工具权限"],
                ["limits", "运行限制"],
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
            <div
              className="av-panel"
              data-agent-panel="basic"
              hidden={tab !== "basic"}
            >
              <section className="av-card">
                <h2>基础信息</h2>
                <p>名称和模型决定助手如何工作。</p>
                <label>
                  助手名称
                  <input
                    required
                    maxLength={80}
                    value={name}
                    disabled={busy}
                    onChange={(event) => setName(event.target.value)}
                  />
                </label>
                <label>
                  模型提供商
                  <select
                    aria-label="模型提供商"
                    value={provider}
                    disabled={busy}
                    onChange={(event) => {
                      setProvider(event.target.value);
                      setModel(event.target.value === "mock" ? "mock-v1" : "");
                      setBaseUrl("");
                      setApiKey("");
                    }}
                  >
                    <option value="">使用服务器默认配置</option>
                    <option value="openai_compatible">OpenAI 兼容供应商</option>
                    <option value="anthropic">Anthropic</option>
                    <option value="mock">Mock（离线演示）</option>
                  </select>
                </label>
                {provider && (
                  <>
                    <label>
                      模型名称
                      <input
                        required
                        maxLength={200}
                        value={model}
                        disabled={busy}
                        onChange={(event) => setModel(event.target.value)}
                      />
                    </label>
                    {provider !== "mock" && (
                      <>
                        <label>
                          模型 URL
                          <input
                            type="url"
                            required
                            maxLength={2048}
                            value={baseUrl}
                            disabled={busy}
                            placeholder="https://模型服务地址/v1"
                            onChange={(event) => setBaseUrl(event.target.value)}
                          />
                        </label>
                        <p className="av-help">
                          填写 API 根地址，无需追加 /chat/completions 或
                          /messages。 开发环境的本机 Ollama 可使用
                          http://host.docker.internal:11434/v1。
                        </p>
                        <label>
                          模型 API Key
                          <input
                            type="password"
                            autoComplete="new-password"
                            required={!keepsKey}
                            value={apiKey}
                            disabled={busy}
                            placeholder={
                              keepsKey
                                ? "已保存，留空保留原密钥"
                                : "请输入 API Key"
                            }
                            onChange={(event) => setApiKey(event.target.value)}
                          />
                        </label>
                      </>
                    )}
                  </>
                )}
                <small className="av-help">修改后将在发布新版本时生效。</small>
              </section>
              <section className="av-card">
                <div className="av-card-title">
                  <h2>角色与指令</h2>
                  <span>系统提示词</span>
                </div>
                <label className="av-prompt">
                  <span className="sr-only">系统提示词</span>
                  <textarea
                    aria-label="系统提示词"
                    required
                    maxLength={4000}
                    value={prompt}
                    disabled={busy}
                    onChange={(event) => setPrompt(event.target.value)}
                  />
                  <small>{prompt.length} / 4000</small>
                </label>
                <p className="av-help">用于设定助手的职责、语气和工作边界。</p>
              </section>
              <section className="av-card">
                <h2>执行方式</h2>
                <div className="av-execution">
                  <label className="av-switch">
                    <span>规划与检查</span>
                    <input
                      type="checkbox"
                      role="switch"
                      checked={closedLoop}
                      disabled={busy}
                      onChange={(event) => setClosedLoop(event.target.checked)}
                    />
                    <span className="av-switch-track" aria-hidden="true" />
                  </label>
                  <p>适合需要多步骤执行和结果检查的任务。</p>
                </div>
                <details className="av-execution-help">
                  <summary>查看执行说明</summary>
                  <p>
                    最多规划 5 步，最多修正 1
                    次。规划、执行与检查共享调用上限和预算；启用后建议将预算调至
                    100000，并按任务调整调用上限。
                  </p>
                </details>
              </section>
              <button
                className="av-advanced"
                type="button"
                onClick={() => setTab("limits")}
              >
                <span>›</span>
                <strong>高级运行参数</strong>
                <small>预算、调用轮次、输出长度与超时</small>
              </button>
            </div>
            <section
              className="av-card av-panel"
              data-agent-panel="tools"
              hidden={tab !== "tools"}
            >
              <h2>工具权限</h2>
              <p>仅授权完成任务所需的工具。</p>
              <fieldset disabled={busy}>
                <legend className="sr-only">授权工具</legend>
                <ToolChoices
                  accountId={account.id}
                  value={tools}
                  onChange={setTools}
                  onCatalog={setToolCatalog}
                />
              </fieldset>
            </section>
            <section
              className="av-card av-panel"
              data-agent-panel="limits"
              hidden={tab !== "limits"}
            >
              <h2>运行限制</h2>
              <p>为每次任务设置合适的资源上限。</p>
              <label>
                单次任务预算（保守额度）
                <input
                  type="number"
                  min={512}
                  max={100000}
                  required
                  value={budget}
                  disabled={busy}
                  onChange={(event) => setBudget(Number(event.target.value))}
                />
              </label>
              <p className="av-help">
                真实 Token 用量与预算额度分别统计。Mock 不产生供应商费用。
              </p>
              {(
                [
                  ["max_steps", "模型决策上限", 1, 12],
                  ["max_output_tokens", "每次输出 Token 上限", 64, 100000],
                  ["timeout_seconds", "任务超时（秒）", 1, 300],
                ] as const
              ).map(([key, title, min, max]) => (
                <label key={key}>
                  {title}
                  <input
                    type="number"
                    required
                    min={min}
                    max={max}
                    disabled={busy}
                    value={limits[key]}
                    onChange={(event) =>
                      setLimits((values) => ({
                        ...values,
                        [key]: Number(event.target.value),
                      }))
                    }
                  />
                </label>
              ))}
            </section>
          </form>
          <aside className="av-summary-rail" aria-label="配置摘要与发布">
            <section className="av-card">
              <h2>配置摘要</h2>
              <div className="av-summary-identity">
                <span className="av-summary-avatar" aria-hidden="true">
                  ◎
                </span>
                <strong>{name || "未命名助手"}</strong>
                <span className="av-version" data-published={!!selected}>
                  {selected ? `已发布版本 v${selected.number}` : "未发布"}
                </span>
              </div>
              <dl className="av-summary-fields">
                <div>
                  <dt>模型</dt>
                  <dd>{provider ? model || "未填写" : "服务器默认"}</dd>
                </div>
                <div>
                  <dt>授权工具</dt>
                  <dd>{tools.length} 项</dd>
                </div>
                <div>
                  <dt>执行方式</dt>
                  <dd>{closedLoop ? "规划与检查" : "标准执行"}</dd>
                </div>
                <div>
                  <dt>任务预算</dt>
                  <dd>{budget.toLocaleString("zh-CN")}</dd>
                </div>
              </dl>
              <p className="av-help">保守额度，与实际 Token 用量分开统计。</p>
              <div className="av-authorized">
                <h3>已授权工具</h3>
                <div className="av-tool-chips">
                  {tools.length ? (
                    tools.map((tool) => (
                      <span key={tool}>{titleForTool(tool)}</span>
                    ))
                  ) : (
                    <small>尚未授权工具</small>
                  )}
                </div>
                <button type="button" onClick={() => setTab("tools")}>
                  管理工具权限 →
                </button>
              </div>
            </section>
            <section className="av-card av-publish">
              <h2>发布更新</h2>
              <p>发布后生成新版本，已有任务继续使用原版本。</p>
              <button
                className="av-primary"
                form="agent-config-form"
                disabled={busy}
              >
                {busy ? "发布中…" : selected ? "发布新版本" : "创建助手"}
              </button>
              {selected && (
                <button
                  ref={deleteTrigger}
                  className="av-delete-action"
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    setDeleteTarget(selected);
                    setNotice("");
                    setDeleteOpen(true);
                  }}
                >
                  删除助手
                </button>
              )}
              <small>
                {selected
                  ? `当前编辑基于版本 ${selected.number}`
                  : "创建后发布第一个版本"}
              </small>
            </section>
          </aside>
        </div>
      </section>
      <Dialog.Portal>
        <Dialog.Overlay className="agent-confirm-backdrop" />
        <Dialog.Content
          className="agent-confirm-dialog"
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            cancelDelete.current?.focus();
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            (deleteTrigger.current?.isConnected
              ? deleteTrigger.current
              : newAgent.current
            )?.focus();
          }}
          onEscapeKeyDown={(event) => {
            if (busy) event.preventDefault();
          }}
          onPointerDownOutside={(event) => {
            if (busy) event.preventDefault();
          }}
        >
          <div className="agent-confirm-icon" aria-hidden="true">
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
            >
              <path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 10v7M14 10v7" />
            </svg>
          </div>
          <Dialog.Title className="agent-confirm-title">删除助手</Dialog.Title>
          <Dialog.Description className="agent-confirm-description">
            确定删除“{deleteTarget?.name}
            ”吗？删除后将无法再使用该助手创建新任务。
            历史会话和任务会保留，已有任务可继续执行。
          </Dialog.Description>
          {notice && <p role={noticeIsError ? "alert" : "status"}>{notice}</p>}
          <div className="agent-confirm-actions">
            <Dialog.Close asChild>
              <button ref={cancelDelete} type="button" disabled={busy}>
                取消
              </button>
            </Dialog.Close>
            <button
              type="button"
              className="agent-confirm-delete"
              disabled={busy}
              onClick={() => void removeAgent()}
            >
              {busy ? "删除中…" : "确认删除"}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
