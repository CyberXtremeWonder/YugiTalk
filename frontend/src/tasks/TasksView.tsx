import { useEffect, useRef, useState } from "react";
import { allItems, identityRequest } from "../identity/api";
import type { Run } from "./api";
import { runError } from "./errors";
/** 展示当前用户的任务状态、用量和允许恢复的失败记录。 */
export function TasksView({
  accountId,
  onOpen,
  csrf,
}: {
  accountId: string;
  onOpen: (id: string) => void;
  csrf: string;
}) {
  const [runs, setRuns] = useState<(Run & { conversation_id: string })[]>([]);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const retryKeys = useRef<Record<string, string>>({});
  const [busy, setBusy] = useState("");
  useEffect(() => {
    let active = true;
    void allItems<Run & { conversation_id: string }>(`/me/runs`)
      .then((rows) => {
        if (active) setRuns(rows);
      })
      .catch((failure) => {
        if (active) setError(failure.message);
      });
    return () => {
      active = false;
    };
  }, [accountId, refresh]);
  return (
    <section className="secondary-view" aria-labelledby="view-title">
      <h1 id="view-title">任务记录</h1>
      <p>仅显示你自己的任务。打开会话可查看回答与执行状态。</p>
      <button onClick={() => setRefresh((previous) => previous + 1)}>
        刷新记录
      </button>
      {error && <p role="alert">{error}</p>}
      {!runs.length && <p>尚无任务记录</p>}
      <div className="task-list">
        {runs.map((run) => (
          <article key={run.id}>
            <button onClick={() => onOpen(run.conversation_id)}>
              <strong>
                {run.status} · {run.provider}/{run.model}
              </strong>
              <small>
                {run.id} · 保守额度 {run.charged_tokens}
                {run.error_code ? ` · ${runError(run.error_code)}` : ""}
              </small>
            </button>
            {run.retryable && (
              <div className="task-retry">
                <p>
                  从已保存的流程恢复。中断的模型调用可能再次计费，已完成的工具不会重复执行。
                </p>
                <button
                  disabled={Boolean(busy)}
                  onClick={() => {
                    setBusy(run.id);
                    setError("");
                    const key = (retryKeys.current[run.id] ||=
                      crypto.randomUUID());
                    void identityRequest(
                      `/runs/${run.id}/retry`,
                      "POST",
                      undefined,
                      csrf,
                      { "Idempotency-Key": key },
                    )
                      .then(() => {
                        delete retryKeys.current[run.id];
                        onOpen(run.conversation_id);
                      })
                      .catch((failure) => setError(failure.message))
                      .finally(() => setBusy(""));
                  }}
                >
                  确认恢复任务
                </button>
              </div>
            )}
          </article>
        ))}
      </div>
    </section>
  );
}
