import { ToolChoices } from "../tools/ToolChoices";
import { useEffect, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { allItems, identityRequest, type Identity } from "../identity/api";
import { type Account } from "../accounts/api";
import type { Agent } from "./api";

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
  const cancelDelete = useRef<HTMLButtonElement>(null);
  const deleteTrigger = useRef<HTMLButtonElement>(null);
  const newAgent = useRef<HTMLButtonElement>(null);
  const [notice, setNotice] = useState("");
  const path = `/me/agents`;
  useEffect(() => {
    let active = true;
    void allItems<Agent>(path)
      .then((rows) => {
        if (active) setAgents(rows);
      })
      .catch((error) => {
        if (active) setNotice(error.message);
      });
    return () => {
      active = false;
    };
  }, [path]);
  const canManage = true;
  /** 清空已选助手和草稿，删除后不保留旧助手配置。 */
  function resetAgent() {
    setSelected(null);
    setName("通用助手");
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
    if (!selected) return;
    const target = selected;
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
      resetAgent();
      setNotice("助手已删除，历史会话和任务已保留。");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "删除助手失败");
    } finally {
      setBusy(false);
    }
  }
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
      <section className="secondary-view" aria-labelledby="view-title">
        <h1 id="view-title">Agent</h1>
        <p>配置助手，发布不可变版本。执行中的任务始终使用创建时的配置。</p>
        <div className="agent-layout">
          <div className="agent-list">
            <button ref={newAgent} disabled={busy} onClick={resetAgent}>
              ＋ 新助手
            </button>
            {agents.map((agent) => (
              <button
                key={agent.id}
                disabled={busy}
                aria-pressed={selected?.id === agent.id}
                onClick={() => {
                  setSelected(agent);
                  setName(agent.name);
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
                }}
              >
                {agent.name}
                <small>
                  版本 {agent.number} · {agent.config.provider}
                </small>
              </button>
            ))}
          </div>
          <form
            className="account-settings agent-form"
            onSubmit={(event) => {
              event.preventDefault();
              setBusy(true);
              setNotice("");
              const config = {
                name,
                system_prompt: prompt,
                closed_loop: closedLoop,
                tools,
                token_budget: budget,
                ...limits,
              };
              void identityRequest(
                selected ? `/agents/${selected.id}/versions` : path,
                "POST",
                config,
                identity.csrf_token,
              )
                .then(() => allItems<Agent>(path))
                .then((rows) => {
                  setAgents(rows);
                  setSelected(null);
                  setNotice("版本发布成功，可以回到对话页选择助手。");
                })
                .catch((error) => setNotice(error.message))
                .finally(() => setBusy(false));
            }}
          >
            <label>
              助手名称
              <input
                required
                maxLength={80}
                value={name}
                disabled={!canManage}
                onChange={(event) => setName(event.target.value)}
              />
            </label>
            <label>
              系统提示词
              <textarea
                required
                maxLength={4000}
                value={prompt}
                disabled={!canManage}
                onChange={(event) => setPrompt(event.target.value)}
              />
            </label>
            <label>
              <input
                type="checkbox"
                checked={closedLoop}
                onChange={(event) => setClosedLoop(event.target.checked)}
              />
              启用规划与检查闭环（最多 5 步，最多修正 1 次）
            </label>
            <p>
              规划、执行与检查共享模型调用上限和预算；检查通过后显示正式答复。
              启用后建议将预算调至 100000，复杂任务适当提高模型调用上限。
            </p>
            <fieldset disabled={!canManage}>
              <legend>授权只读工具</legend>
              <ToolChoices
                accountId={account.id}
                value={tools}
                onChange={setTools}
              />{" "}
            </fieldset>
            <label>
              单次任务预算（保守额度）
              <input
                type="number"
                min={512}
                max={100000}
                required
                value={budget}
                disabled={!canManage}
                onChange={(event) => setBudget(Number(event.target.value))}
              />
            </label>
            <p>
              真实 Token 用量与预算额度分别显示；Mock
              不产生供应商费用。普通成员只能使用已发布助手。
            </p>
            {(
              [
                ["max_steps", "模型决策上限", 1, 12],
                ["max_tool_calls", "工具调用上限", 0, 20],
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
                  disabled={!canManage}
                  value={limits[key]}
                  onChange={(event) =>
                    setLimits((previous) => ({
                      ...previous,
                      [key]: Number(event.target.value),
                    }))
                  }
                />
              </label>
            ))}
            <button disabled={busy || !canManage}>
              {busy ? "发布中…" : selected ? "发布新版本" : "创建助手"}
            </button>
            {selected && (
              <Dialog.Trigger asChild>
                <button ref={deleteTrigger} type="button" disabled={busy}>
                  删除助手
                </button>
              </Dialog.Trigger>
            )}
            {notice && <p role="status">{notice}</p>}
          </form>
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
            (deleteTrigger.current ?? newAgent.current)?.focus();
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
            确定删除“{selected?.name}”吗？删除后将无法再使用该助手创建新任务。
            历史会话和任务会保留，已有任务可继续执行。
          </Dialog.Description>
          {notice && <p role="alert">{notice}</p>}
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
