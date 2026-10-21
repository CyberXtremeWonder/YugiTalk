import { documentError, type Document } from "./api";

const steps = [
  ["queued", "等待处理"],
  ["reading", "读取原文"],
  ["parsing", "解析与分块"],
  ["preparing", "准备索引"],
  ["embedding", "Embedding 编码"],
  ["writing", "写入 Milvus"],
  ["ready", "完成"],
] as const;
type StepState = "pending" | "active" | "done" | "failed" | "paused";
const stateLabels = {
  pending: "待执行",
  active: "进行中",
  done: "已完成",
  failed: "失败",
  paused: "已暂停",
};
const symbols = {
  pending: "○",
  active: "◉",
  done: "✓",
  failed: "×",
  paused: "Ⅱ",
};

// 编码与写入交替按批次进行；只有全部分块成功后才把对应步骤标记完成。
/** 根据资料状态、当前步骤和失败位置确定单项流程状态。 */
function stepState(doc: Document, key: string): StepState {
  if (doc.status === "ready") return "done";
  const progress = doc.progress || {};
  const stage =
    progress.stage || (doc.status === "queued" ? "queued" : "unknown");
  const failedStage =
    progress.failed_stage ||
    (doc.error_code === "queue_unavailable" ? "queued" : stage);
  const failed = doc.status === "failed";
  if (failed && key === failedStage) return "failed";
  const total = progress.total_chunks || 0;
  if (key === "embedding" || key === "writing") {
    // 编码和写入会交替发生，不能只看当前步骤的位置；各自以已完成块数判断是否结束。
    const count =
      (key === "embedding"
        ? progress.embedded_chunks
        : progress.indexed_chunks) || 0;
    if (total > 0 && count >= total) return "done";
    if (failed && count > 0) return "paused";
    if (!failed && (stage === key || count > 0)) return "active";
    return "pending";
  }
  const index = steps.findIndex(([value]) => value === key);
  const current = steps.findIndex(([value]) => value === stage);
  if (index < current) return "done";
  return key === stage && !failed ? "active" : "pending";
}

/** 文件名下显示按已写入分块计算的索引进度，解析前不虚构百分比。 */
export function DocumentIndexProgress({ doc }: { doc: Document }) {
  if (!["queued", "parsing", "indexing", "failed"].includes(doc.status))
    return null;
  const progress = doc.progress || {};
  const total = progress.total_chunks || 0;
  const indexed = Math.min(total, Math.max(0, progress.indexed_chunks || 0));
  const stage = progress.failed_stage || progress.stage || doc.status;
  const title = steps.find(([key]) => key === stage)?.[1] || "索引处理";
  const percent = total > 0 ? Math.floor((indexed / total) * 100) : null;
  // 尚未解析出块总数时 progress 不设置 value，显示不确定进度，而不是伪造完成百分比。
  return (
    <div
      className="document-index-progress"
      data-failed={doc.status === "failed"}
    >
      <progress
        aria-label={`索引进度：${doc.filename}`}
        max={total || 1}
        value={total > 0 ? indexed : doc.status === "failed" ? 0 : undefined}
      />
      <span>
        {title}
        {doc.status === "failed" ? "失败" : "中"}
        {percent !== null
          ? ` · ${percent}% · 已索引 ${indexed}/${total} 块`
          : " · 尚未确定分块总数"}
      </span>
    </div>
  );
}

/** 竖向展示资料处理步骤、批次计数与准确失败位置。 */
export function DocumentProgress({ doc }: { doc: Document }) {
  // 成功导入后恢复普通文件列表，失败流程继续保留供用户定位。
  if (["ready", "deleting", "deleted"].includes(doc.status)) return null;
  const progress = doc.progress || {};
  const total = progress.total_chunks || 0;
  const failed = doc.status === "failed";
  const currentStage =
    progress.failed_stage ||
    progress.stage ||
    (doc.error_code === "queue_unavailable" ? "queued" : "unknown");
  const title = steps.find(([key]) => key === currentStage)?.[1];
  return (
    <section
      className="import-progress"
      aria-label={`导入流程：${doc.filename}`}
      aria-live="polite"
    >
      <ol className="import-steps">
        {steps.map(([key, label]) => {
          const state = stepState(doc, key);
          return (
            <li
              key={key}
              data-state={state}
              aria-current={state === "active" ? "step" : undefined}
            >
              <span aria-hidden="true">{symbols[state]}</span>
              {label}
              <small>{stateLabels[state]}</small>
            </li>
          );
        })}
      </ol>
      {total > 0 && (
        <div className="import-counts">
          <label>
            Embedding：{progress.embedded_chunks || 0}/{total} 块
            <progress max={total} value={progress.embedded_chunks || 0} />
          </label>
          <label>
            Milvus：{progress.indexed_chunks || 0}/{total} 块
            <progress max={total} value={progress.indexed_chunks || 0} />
          </label>
          {doc.status === "indexing" && (
            <p>
              当前第 {progress.batch_number || 1} 批，每批最多 16
              块；按已完成批次更新。
            </p>
          )}
        </div>
      )}
      {failed && (
        <p className="import-error">
          {title ? `失败步骤：${title}` : "导入失败，历史任务未记录具体步骤"}。
          {documentError(doc.error_code)}
          {total > 0 && "已完成数量保留，重试将重新构建此文件的索引。"}
        </p>
      )}
    </section>
  );
}
