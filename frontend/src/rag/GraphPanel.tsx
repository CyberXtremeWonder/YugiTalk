import { useNotice } from "../shared/useNotice";
import { useEffect, useState } from "react";
import { identityRequest } from "../identity/api";
import { GraphExplorer } from "./GraphExplorer";

interface GraphData {
  status: string;
  mode?: string;
  index_version?: number;
  error_code?: string;
  entity_count?: number;
  has_more?: boolean;
  completed_chunks?: number;
  total_chunks?: number;
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
  expanded = false,
}: {
  documentId: string;
  csrf: string;
  expanded?: boolean;
}) {
  const [open, setOpen] = useState(expanded);
  const [data, setData] = useState<GraphData | null>(null);
  const { notice, setNotice, noticeIsError, reportError } = useNotice();
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
        if (active) {
          setData(null);
          reportError(error instanceof Error ? error.message : "读取图谱失败");
        }
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
      // revision 触发 effect 重新启动轮询；仅更新 data 不会重新建立刷新循环。
      setRevision((value) => value + 1);
    } catch (error) {
      reportError(error instanceof Error ? error.message : "提交失败");
    } finally {
      setBusy(false);
    }
  };

  const content = (
    <>
      {!data && !notice && <p role="status">正在读取知识图谱…</p>}
      {notice && <p role={noticeIsError ? "alert" : "status"}>{notice}</p>}
      {data && (
        <>
          <div className="graph-build-status">
            <p>
              图谱：{statuses[data.status] || "暂不可用"}
              {data.entity_count !== undefined &&
                ` · ${data.entity_count} 个实体`}
            </p>
            {["queued", "building"].includes(data.status) &&
              data.total_chunks !== undefined && (
                <p aria-live="polite">
                  已处理 {data.completed_chunks ?? 0}/{data.total_chunks} 个分块
                </p>
              )}
            {data.mode === "mock" && (
              <p>当前为演示模式，未调用模型抽取关系。</p>
            )}
            {data.status === "failed" && (
              <p>
                图谱未构建成功，可检查模型服务后重试。普通知识问答仍可使用。
              </p>
            )}
            {expanded && !["queued", "building"].includes(data.status) && (
              <button disabled={busy} onClick={() => void rebuild()}>
                {data.status === "ready" ? "重新构建图谱" : "构建图谱"}
              </button>
            )}
          </div>
          {!!data.facts.length && (
            <GraphExplorer
              facts={data.facts}
              indexVersion={data.index_version}
            />
          )}
          {data.status === "ready" && !data.facts.length && (
            <p>没有抽取到可用关系。</p>
          )}
          {data.has_more && <p>此处仅展示前 100 条关系依据。</p>}
        </>
      )}
    </>
  );
  return expanded ? (
    <section className="graph-panel">{content}</section>
  ) : (
    <details
      className="graph-panel"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>知识图谱与原文依据</summary>
      {open && content}
    </details>
  );
}
