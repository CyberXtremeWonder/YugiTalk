import { useEffect, useState } from "react";
import { identityRequest } from "../identity/api";

interface GraphData {
  status: string;
  mode?: string;
  error_code?: string;
  entity_count?: number;
  has_more?: boolean;
  facts: {
    source: string;
    target: string;
    relation: string;
    quote: string;
    chunk_id: string;
  }[];
}

const statuses: Record<string, string> = {
  unavailable: "尚未构建",
  queued: "等待构建",
  building: "构建中",
  ready: "已完成",
  failed: "构建失败",
  cancelled: "已取消",
};

/** 按需展示文档关系与原文依据，补建图谱不触发向量重新索引。 */
export function GraphPanel({
  documentId,
  csrf,
}: {
  documentId: string;
  csrf: string;
}) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<GraphData | null>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!open) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    /** 仅在面板展开且任务未结束时刷新，离开资料后停止更新。 */
    const refresh = async () => {
      try {
        const result = await identityRequest<GraphData>(
          `/documents/${documentId}/graph`,
        );
        if (!active) return;
        setData(result);
        setNotice("");
        if (["queued", "building"].includes(result.status))
          timer = setTimeout(refresh, 3000);
      } catch (error) {
        if (active)
          setNotice(error instanceof Error ? error.message : "读取图谱失败");
      }
    };
    void refresh();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [open, documentId, revision]);

  /** 显式请求后台构建并刷新状态，错误不影响已有资料检索。 */
  const rebuild = async () => {
    setBusy(true);
    try {
      await identityRequest(
        `/documents/${documentId}/graph`,
        "POST",
        undefined,
        csrf,
      );
      setData({ status: "queued", facts: [] });
      setRevision((value) => value + 1);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "提交失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <details onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>知识图谱与原文依据</summary>
      {notice && <p role="alert">{notice}</p>}
      {data && (
        <>
          <p>
            图谱：{statuses[data.status] || "暂不可用"}
            {data.entity_count !== undefined &&
              ` · ${data.entity_count} 个实体`}
          </p>
          {data.mode === "mock" && <p>当前为演示模式，未调用模型抽取关系。</p>}
          {data.status === "failed" && (
            <p>
              {data.error_code === "graph_document_limit"
                ? "资料超过第一版图谱的 64 个分块上限，请拆分文件。"
                : "图谱未构建成功，可检查模型服务后重试。普通知识问答仍可使用。"}
            </p>
          )}
          {!["queued", "building"].includes(data.status) && (
            <button disabled={busy} onClick={() => void rebuild()}>
              {data.status === "ready" ? "重新构建图谱" : "构建图谱"}
            </button>
          )}
          <ul>
            {data.facts.map((fact, index) => (
              <li key={`${fact.chunk_id}-${index}`}>
                <strong>
                  {fact.source} → {fact.relation} → {fact.target}
                </strong>
                <blockquote>{fact.quote}</blockquote>
              </li>
            ))}
          </ul>
          {data.status === "ready" && !data.facts.length && (
            <p>没有抽取到可用关系。</p>
          )}
          {data.has_more && <p>此处仅展示前 100 条关系依据。</p>}
        </>
      )}
    </details>
  );
}
