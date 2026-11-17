import { useEffect, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { allItems, identityRequest } from "../identity/api";
import { useNotice } from "../shared/useNotice";
import type { Run } from "./api";
import { runError } from "./errors";
import "../shared/configuration.css";
import "./tasks.css";

type TaskRecord = Run & { conversation_id: string };
const statuses: Record<string, { label: string; tone: string }> = {
  queued: { label: "排队中", tone: "running" },
  running: { label: "运行中", tone: "running" },
  paused: { label: "已暂停", tone: "attention" },
  succeeded: { label: "已完成", tone: "success" },
  failed: { label: "失败", tone: "failed" },
  waiting_input: { label: "待补充", tone: "attention" },
  waiting_approval: { label: "待审批", tone: "attention" },
  cancelled: { label: "已停止", tone: "stopped" },
};
const filters = [
  { id: "all", label: "全部", statuses: [] },
  { id: "running", label: "运行中", statuses: ["running", "queued"] },
  { id: "succeeded", label: "已完成", statuses: ["succeeded"] },
  { id: "failed", label: "失败", statuses: ["failed"] },
  {
    id: "waiting",
    label: "待确认",
    statuses: ["waiting_input", "waiting_approval", "paused"],
  },
  { id: "cancelled", label: "已停止", statuses: ["cancelled"] },
];
function title(run: TaskRecord) {
  return run.conversation_title || run.prompt_preview || "未命名对话";
}
function time(value?: string) {
  return value
    ? new Date(value).toLocaleString("zh-CN", { hour12: false })
    : "未记录";
}
function tokens(value: number | null | undefined) {
  return value == null ? "未提供" : value.toLocaleString("zh-CN");
}
function TaskIcon({ tone }: { tone: string }) {
  const paths: Record<string, string> = {
    all: "M7 3h7l4 4v14H4V3h3Zm6 0v5h5M8 12h6m-6 4h6",
    running: "M12 3a9 9 0 1 1-9 9M3 4v5h5",
    success: "m7 12 3 3 7-7M21 12a9 9 0 1 1-9-9",
    attention: "M12 7v6m0 4h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z",
    failed: "m9 9 6 6m0-6-6 6M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z",
    stopped: "M9 9h6v6H9zM21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z",
  };
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[tone] || paths.all} />
    </svg>
  );
}

/** 当前用户的任务概览、筛选与详情；恢复沿用原接口和幂等键。 */
export function TasksView({
  accountId,
  onOpen,
  csrf,
}: {
  accountId: string;
  onOpen: (id: string) => void;
  csrf: string;
}) {
  const [runs, setRuns] = useState<TaskRecord[]>([]);
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState("");
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const [retryId, setRetryId] = useState("");
  const [busy, setBusy] = useState("");
  const retryKeys = useRef<Record<string, string>>({});
  const { notice, setNotice, noticeIsError, reportError } = useNotice();
  useEffect(() => {
    let active = true;
    setLoading(true);
    setNotice("");
    void allItems<TaskRecord>("/me/runs")
      .then((rows) => {
        if (active)
          setRuns(
            rows.sort(
              (a, b) =>
                (Date.parse(b.created_at || "") || 0) -
                (Date.parse(a.created_at || "") || 0),
            ),
          );
      })
      .catch((failure: Error) => {
        if (active) reportError(failure.message);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [accountId, refresh]);
  const count = (states: string[]) =>
    states.length
      ? runs.filter((run) => states.includes(run.status)).length
      : runs.length;
  const activeFilter = filters.find((item) => item.id === filter)!;
  const filtered = runs.filter(
    (run) =>
      (!activeFilter.statuses.length ||
        activeFilter.statuses.includes(run.status)) &&
      `${title(run)} ${run.prompt_preview || ""} ${run.id}`
        .toLowerCase()
        .includes(search.trim().toLowerCase()),
  );
  const selected = filtered.find((run) => run.id === selectedId) || filtered[0];
  const selectedStatus =
    selected &&
    (statuses[selected.status] || { label: selected.status, tone: "stopped" });
  const retryTarget = runs.find((run) => run.id === retryId);
  const metrics = [
    { label: "全部任务", tone: "all", value: runs.length },
    { label: "运行中", tone: "running", value: count(["queued", "running"]) },
    { label: "已完成", tone: "success", value: count(["succeeded"]) },
    {
      label: "需处理",
      tone: "attention",
      value: count(["failed", "waiting_input", "waiting_approval", "paused"]),
    },
  ];
  async function retry() {
    if (!retryTarget?.retryable || busy) return;
    const target = retryTarget;
    setBusy(target.id);
    setNotice("");
    // 网络结果不确定时保留同一恢复键；成功后下一次操作再生成新键。
    const key = (retryKeys.current[target.id] ||= crypto.randomUUID());
    try {
      await identityRequest(
        `/runs/${target.id}/retry`,
        "POST",
        undefined,
        csrf,
        { "Idempotency-Key": key },
      );
      delete retryKeys.current[target.id];
      setRetryId("");
      onOpen(target.conversation_id);
    } catch (failure) {
      reportError(failure instanceof Error ? failure.message : "恢复任务失败");
    } finally {
      setBusy("");
    }
  }
  return (
    <section
      className="secondary-view configuration-workspace task-workspace"
      aria-labelledby="view-title"
    >
      <header className="cf-heading">
        <div>
          <h1 id="view-title">任务记录</h1>
          <p>追踪每次执行，快速回到对话。</p>
        </div>
        <button
          className="cf-secondary th-refresh"
          disabled={loading || !!busy}
          onClick={() => setRefresh((previous) => previous + 1)}
        >
          <span aria-hidden="true">↻</span>
          {loading ? "刷新中…" : "刷新记录"}
        </button>
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
      <div className="th-metrics" aria-label="当前记录概览" aria-busy={loading}>
        {metrics.map((metric) => (
          <div className="th-metric" key={metric.label} data-tone={metric.tone}>
            <span className="th-icon">
              <TaskIcon tone={metric.tone} />
            </span>
            <div>
              <span>{metric.label}</span>
              <strong>{loading ? "—" : metric.value}</strong>
            </div>
          </div>
        ))}
      </div>
      <p className="th-retention">
        保留最近 100 条任务记录 · 最新任务优先 · 运行中的任务会先保留
      </p>
      <div className="th-layout">
        <section className="cf-card th-directory" aria-label="最近任务">
          <div className="cf-card-title">
            <h2>最近任务</h2>
            <input
              className="cf-search"
              aria-label="搜索任务名称或内容"
              placeholder="搜索任务名称或内容…"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>
          <div className="th-filters" aria-label="任务状态筛选">
            {filters.map((item) => (
              <button
                key={item.id}
                aria-pressed={filter === item.id}
                onClick={() => setFilter(item.id)}
              >
                {item.label}
                <span>{loading ? "—" : count(item.statuses)}</span>
              </button>
            ))}
          </div>
          <div
            className="th-table-scroll"
            role="region"
            aria-label="任务记录表格"
            tabIndex={0}
          >
            <table className="th-table" aria-busy={loading}>
              <thead>
                <tr>
                  <th>任务</th>
                  <th>状态</th>
                  <th>创建时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((run) => {
                  const state = statuses[run.status] || {
                    label: run.status,
                    tone: "stopped",
                  };
                  return (
                    <tr key={run.id} data-selected={selected?.id === run.id}>
                      <td>
                        <button
                          className="th-task"
                          aria-label={`查看任务：${title(run)}`}
                          aria-pressed={selected?.id === run.id}
                          disabled={!!busy}
                          onClick={() => setSelectedId(run.id)}
                        >
                          <span className="th-icon" data-tone={state.tone}>
                            <TaskIcon tone={state.tone} />
                          </span>
                          <span>
                            <strong>{title(run)}</strong>
                            {run.prompt_preview && (
                              <span className="th-preview">
                                {run.prompt_preview}
                              </span>
                            )}
                            <small>{run.model || "未记录模型"}</small>
                          </span>
                        </button>
                      </td>
                      <td>
                        <span className="th-status" data-tone={state.tone}>
                          <TaskIcon tone={state.tone} />
                          {state.label}
                        </span>
                        {run.error_code && run.status === "failed" && (
                          <small
                            className="th-error-preview"
                            title={runError(run.error_code)}
                          >
                            {runError(run.error_code)}
                          </small>
                        )}
                      </td>
                      <td>
                        <time dateTime={run.created_at}>
                          {time(run.created_at)}
                        </time>
                      </td>
                      <td>
                        <button
                          className="cf-link"
                          aria-label={`查看详情：${title(run)}`}
                          disabled={!!busy}
                          onClick={() => setSelectedId(run.id)}
                        >
                          查看 <span aria-hidden="true">›</span>
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {!filtered.length && (
            <p className="th-empty">
              {loading
                ? "正在加载任务记录…"
                : noticeIsError && !runs.length
                  ? "任务记录加载失败，请刷新重试。"
                  : !runs.length
                    ? "尚无任务记录"
                    : "没有符合筛选条件的任务。"}
            </p>
          )}
          <footer className="th-list-footer">
            <span>
              共 {runs.length} 条任务记录
              {filtered.length !== runs.length &&
                ` · 当前显示 ${filtered.length} 条`}
            </span>
            <span>点击任务查看详情</span>
          </footer>
        </section>
        <aside className="cf-card th-details" aria-label="任务详情">
          <h2>任务详情</h2>
          {selected && selectedStatus ? (
            <>
              <div className="th-detail-title">
                <span className="th-icon" data-tone={selectedStatus.tone}>
                  <TaskIcon tone={selectedStatus.tone} />
                </span>
                <strong>{title(selected)}</strong>
                <span className="th-status" data-tone={selectedStatus.tone}>
                  {selectedStatus.label}
                </span>
              </div>
              <h3>任务内容</h3>
              <blockquote>
                {selected.prompt_preview || "此任务没有可用的内容摘要。"}
              </blockquote>
              <dl className="th-metadata">
                <div>
                  <dt>创建时间</dt>
                  <dd>{time(selected.created_at)}</dd>
                </div>
                <div>
                  <dt>模型</dt>
                  <dd>
                    {selected.model || "未记录"}
                    <small>{selected.provider || "未记录供应商"}</small>
                  </dd>
                </div>
                <div>
                  <dt>执行次数</dt>
                  <dd>{selected.attempts ?? "未提供"}</dd>
                </div>
                {selected.skill && (
                  <div>
                    <dt>技能版本</dt>
                    <dd>
                      {selected.skill.name} · v{selected.skill.number}
                    </dd>
                  </div>
                )}
                <div>
                  <dt>任务 ID</dt>
                  <dd className="th-task-id">
                    <code title={selected.id}>{selected.id}</code>
                    <button
                      className="cf-link"
                      aria-label="复制任务 ID"
                      onClick={async () => {
                        try {
                          await navigator.clipboard.writeText(selected.id);
                          setNotice("任务 ID 已复制。");
                        } catch {
                          reportError("复制失败，请手动选择任务 ID。");
                        }
                      }}
                    >
                      复制
                    </button>
                  </dd>
                </div>
              </dl>
              <h3>Token 用量</h3>
              <div className="th-tokens">
                <div>
                  <span>输入</span>
                  <strong>{tokens(selected.input_tokens)}</strong>
                </div>
                <div>
                  <span>输出</span>
                  <strong>{tokens(selected.output_tokens)}</strong>
                </div>
                <div>
                  <span>保守额度</span>
                  <strong>{tokens(selected.charged_tokens)}</strong>
                </div>
              </div>
              <p className="th-help">保守额度与真实 Token 用量分别显示。</p>
              {selected.error_code && (
                <div
                  className="th-feedback"
                  data-tone={
                    selected.status === "cancelled" ? "stopped" : "failed"
                  }
                >
                  <TaskIcon
                    tone={
                      selected.status === "cancelled" ? "stopped" : "failed"
                    }
                  />
                  <div>
                    <strong>{runError(selected.error_code)}</strong>
                    {selected.retryable && <p>可以从已保存的流程恢复任务。</p>}
                  </div>
                </div>
              )}
              {selected.clarification && (
                <div className="th-feedback" data-tone="attention">
                  <TaskIcon tone="attention" />
                  <div>
                    <strong>任务等待补充信息</strong>
                    <p>{selected.clarification.prompt}</p>
                  </div>
                </div>
              )}
              <button
                className="cf-primary cf-wide"
                disabled={!!busy}
                onClick={() => onOpen(selected.conversation_id)}
              >
                打开对话
              </button>
              {selected.retryable && (
                <>
                  <button
                    className="cf-secondary cf-wide"
                    disabled={!!busy}
                    onClick={() => {
                      setNotice("");
                      setRetryId(selected.id);
                    }}
                  >
                    恢复任务
                  </button>
                  <p className="th-help">恢复前会再次确认。</p>
                </>
              )}
            </>
          ) : (
            <p className="th-empty">
              {loading
                ? "正在读取任务…"
                : "选择一条任务，查看内容、状态和用量。"}
            </p>
          )}
        </aside>
      </div>
      <Dialog.Root
        open={!!retryTarget}
        onOpenChange={(open) => {
          if (!open && !busy) setRetryId("");
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="agent-confirm-backdrop" />
          <Dialog.Content
            className="agent-confirm-dialog"
            onEscapeKeyDown={(event) => {
              if (busy) event.preventDefault();
            }}
            onPointerDownOutside={(event) => {
              if (busy) event.preventDefault();
            }}
          >
            <Dialog.Title className="agent-confirm-title">
              恢复任务
            </Dialog.Title>
            <Dialog.Description className="agent-confirm-description">
              确认恢复“{retryTarget && title(retryTarget)}
              ”吗？从已保存的流程恢复。中断的模型调用可能再次计费，已完成的工具不会重复执行。
            </Dialog.Description>
            {noticeIsError && <p role="alert">{notice}</p>}
            <div className="agent-confirm-actions">
              <Dialog.Close asChild>
                <button disabled={!!busy}>取消</button>
              </Dialog.Close>
              <button
                className="cf-primary th-confirm"
                disabled={!!busy || !retryTarget?.retryable}
                onClick={() => void retry()}
              >
                {busy ? "恢复中…" : "确认恢复任务"}
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </section>
  );
}
