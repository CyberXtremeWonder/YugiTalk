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
  if (progress.streaming) {
    const bytes = progress.processed_bytes || 0;
    const size = progress.source_bytes || doc.size;
    return (
      <div
        className="document-index-progress"
        data-failed={doc.status === "failed"}
      >
        <progress
          aria-label={`索引进度：${doc.filename}`}
          max={size || 1}
          value={bytes}
        />
        <span>
          {doc.status === "failed" ? "导入失败" : "逐行导入中"}
          {` · 已处理 ${progress.processed_records || 0} 条记录，已索引 ${progress.indexed_chunks || 0} 块`}
        </span>
      </div>
    );
  }
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
  // 有计时的已完成导入保留流程，供用户比较各阶段耗时。
  if (["deleting", "deleted"].includes(doc.status)) return null;
  if (doc.status === "ready" && !doc.progress?.stage_seconds) return null;
  const progress = doc.progress || {};
  const timings = progress.stage_seconds;
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
              {timings?.[key] !== undefined && (
                <small>累计 {timings[key].toFixed(2)} 秒</small>
              )}
            </li>
          );
        })}
      </ol>
      {timings && (
        <div className="import-counts">
          {timings.sql !== undefined && <p>SQL 事务累计：{timings.sql.toFixed(2)} 秒</p>}
          <p>累计阶段工作耗时；阶段可并行，不可相加作为总耗时，不含排队。</p>
        </div>
      )}
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
