import { useEffect, useState } from "react";
import { allItems, identityRequest, type Identity } from "../identity/api";
import { type Account } from "../accounts/api";
import {
  documentStatuses,
  documentError,
  uploadFiles,
  type UploadBatch,
  type Document,
  type KnowledgeBase,
  type Source,
} from "./api";
import { Sources } from "./Sources";
import { GraphPanel } from "./GraphPanel";
import { DocumentIndexProgress, DocumentProgress } from "./DocumentProgress";

/** 管理知识库、文件与文件夹上传、处理流程及资料检索。 */
export function KnowledgeView({
  identity,
  account,
}: {
  identity: Identity;
  account: Account;
}) {
  const [bases, setBases] = useState<KnowledgeBase[]>([]);
  const [base, setBase] = useState("");
  const [name, setName] = useState("");
  const [documents, setDocuments] = useState<Document[]>([]);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");
  const [sources, setSources] = useState<Source[]>([]);
  const [uploadBatch, setUploadBatch] = useState<UploadBatch | null>(null);
  const [batchDocumentIds, setBatchDocumentIds] = useState<string[]>([]);
  const manage = true;
  useEffect(() => {
    let active = true;
    void allItems<KnowledgeBase>(`/me/knowledge-bases`)
      .then((rows) => {
        if (active) {
          setBases(rows);
          setBase(rows[0]?.id || "");
        }
      })
      .catch((error) => {
        if (active) setNotice(error.message);
      });
    return () => {
      active = false;
    };
  }, [account.id]);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    /** 重新读取当前功能的授权数据，并同步页面显示。 */
    const refresh = async () => {
      if (!base) {
        setDocuments([]);
        return;
      }
      try {
        const rows = await allItems<Document>(
          `/knowledge-bases/${base}/documents`,
        );
        if (!active) return;
        setDocuments(rows);
        if (
          busy ||
          rows.some((row) =>
            ["queued", "parsing", "indexing", "deleting"].includes(row.status),
          )
        )
          timer = setTimeout(() => void refresh(), 2000);
      } catch (error) {
        if (active)
          setNotice(error instanceof Error ? error.message : "加载失败");
        // 暂时断网后继续轮询，避免流程停在最后一次读到的进度。
        if (active) timer = setTimeout(() => void refresh(), 2000);
      }
    };
    void refresh();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [base, busy]);
  useEffect(() => {
    if (
      !busy &&
      uploadBatch &&
      !uploadBatch.current &&
      !uploadBatch.failed &&
      !uploadBatch.stopped &&
      uploadBatch.completed === uploadBatch.total &&
      batchDocumentIds.length > 0 &&
      batchDocumentIds.every((id) =>
        documents.some(
          (doc) =>
            doc.id === id &&
            ["ready", "failed", "deleting", "deleted"].includes(doc.status),
        ),
      )
    ) {
      // 上传响应不代表索引结束；本轮索引结束后重置统计，失败流程单独保留。
      setUploadBatch(null);
    }
  }, [busy, uploadBatch, batchDocumentIds, documents]);
  /** 提交选中文件队列，更新上传进度并刷新资料列表。 */
  async function importFiles(files: File[]) {
    setBusy(true);
    setUploadBatch(null);
    setBatchDocumentIds([]);
    setNotice("正在上传，已提交的资料会独立显示索引进度…");
    try {
      const result = await uploadFiles(
        base,
        files,
        identity.csrf_token,
        (batch, doc) => {
          setUploadBatch(batch);
          if (doc) {
            setBatchDocumentIds((ids) => [...new Set([...ids, doc.id])]);
            setDocuments((rows) => [
              ...rows.filter((row) => row.id !== doc.id),
              doc,
            ]);
          }
        },
      );
      setNotice(
        result.total
          ? `上传${result.stopped ? "已停止" : "完成"}：成功 ${result.uploaded}，失败 ${result.failed}，跳过 ${result.skipped}。相同文件自动去重。`
          : "所选文件夹中没有支持的 TXT、Markdown 或 PDF 文件。",
      );
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "上传失败");
    } finally {
      setBusy(false);
    }
  }
  /** 执行资料重试或删除操作，完成后同步列表与提示。 */
  async function action(path: string, method = "POST") {
    setBusy(true);
    setNotice("");
    try {
      const updated = await identityRequest<Document>(
        path,
        method,
        undefined,
        identity.csrf_token,
      );
      if (updated?.id)
        setDocuments((rows) =>
          rows.map((row) => (row.id === updated.id ? updated : row)),
        );
      if (method === "DELETE" && updated?.id)
        setSources((rows) =>
          rows.filter((source) => source.document_id !== updated.id),
        );
      setNotice("操作已提交。");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }
  const workflowDocuments = documents.filter(
    (doc) =>
      ["queued", "parsing", "indexing", "failed"].includes(doc.status) &&
      (!batchDocumentIds.length || batchDocumentIds.includes(doc.id)),
  );
  const visibleDocuments = documents.filter(
    (doc) => !["deleting", "deleted"].includes(doc.status),
  );
  const cleanupFailures = documents.filter(
    (doc) => doc.status === "deleting" && doc.error_code,
  );
  return (
    <section
      className="secondary-view knowledge-view"
      aria-labelledby="view-title"
    >
      <h1 id="view-title">知识库</h1>
      <p>
        导入资料，检索片段，在对话中核对来源。支持 UTF-8 TXT、Markdown 和文本
        PDF。
      </p>
      <div className="knowledge-toolbar">
        {manage && (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              setBusy(true);
              void identityRequest<KnowledgeBase>(
                `/me/knowledge-bases`,
                "POST",
                { name },
                identity.csrf_token,
              )
                .then(async (created) => {
                  setBases(
                    await allItems<KnowledgeBase>(`/me/knowledge-bases`),
                  );
                  setBase(created.id);
                  setName("");
                  setNotice("知识库已创建。");
                })
                .catch((error) => setNotice(error.message))
                .finally(() => setBusy(false));
            }}
          >
            <label>
              知识库名称
              <input
                value={name}
                onChange={(event) => setName(event.target.value)}
                required
                maxLength={80}
              />
            </label>
            <button disabled={busy}>创建知识库</button>
          </form>
        )}
        <label>
          当前知识库
          <select
            aria-label="当前知识库"
            value={base}
            disabled={busy}
            onChange={(event) => {
              setBase(event.target.value);
              setSources([]);
              setDocuments([]);
              setUploadBatch(null);
              setBatchDocumentIds([]);
            }}
          >
            {!bases.length && <option value="">暂无知识库</option>}
            {bases.map((row) => (
              <option key={row.id} value={row.id}>
                {row.name}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="knowledge-columns">
        <section
          className="knowledge-import-panel"
          aria-labelledby="import-title"
        >
          <h2 id="import-title">上传流程</h2>
          <div className="knowledge-toolbar">
            {manage && (
              <>
                <label>
                  上传资料（最多 20 MB）
                  <input
                    type="file"
                    accept=".txt,.md,.pdf"
                    disabled={!base || busy}
                    onChange={(event) => {
                      const files = Array.from(event.target.files || []);
                      if (!files.length) return;
                      void importFiles(files);
                      event.target.value = "";
                    }}
                  />
                </label>
                <label>
                  上传文件夹
                  <input
                    type="file"
                    multiple
                    aria-label="上传文件夹"
                    disabled={!base || busy}
                    ref={(node) => {
                      node?.setAttribute("webkitdirectory", "");
                    }}
                    onChange={(event) => {
                      const files = Array.from(event.target.files || []);
                      if (!files.length) return;
                      void importFiles(files);
                      event.target.value = "";
                    }}
                  />
                  <small>
                    递归导入子文件夹中的 TXT、Markdown 和 PDF，每个文件最多 20
                    MB，一次最多 200 个支持的文件。
                  </small>
                </label>
              </>
            )}
          </div>
          <p role="status">{notice}</p>
          {uploadBatch && (
            <section
              className="folder-upload-report"
              aria-label="批量上传进度"
              aria-live="polite"
            >
              <p>
                上传进度：{uploadBatch.completed}/{uploadBatch.total} · 成功{" "}
                {uploadBatch.uploaded} · 失败 {uploadBatch.failed} · 跳过{" "}
                {uploadBatch.skipped} 个不支持的文件
              </p>
              {uploadBatch.current && <p>正在上传：{uploadBatch.current}</p>}
              {uploadBatch.stopped && (
                <p>
                  后续 {uploadBatch.total - uploadBatch.completed}{" "}
                  个文件尚未上传，请处理权限或资料数量限制后重试。
                </p>
              )}
              <p>上传成功后，各文件继续解析和索引。相同内容自动去重。</p>
              {!!uploadBatch.errors.length && (
                <ul>
                  {uploadBatch.errors.map((row, index) => (
                    <li key={index}>
                      {row.name}：{row.message}
                    </li>
                  ))}
                </ul>
              )}
            </section>
          )}
          <div className="import-workflows">
            {workflowDocuments.length ? (
              workflowDocuments.map((doc) => (
                <article
                  className="import-workflow"
                  key={doc.id}
                  aria-label={`文件流程：${doc.filename}`}
                >
                  <strong>{doc.filename}</strong>
                  <DocumentIndexProgress doc={doc} />
                  <small> · v{doc.index_version}</small>
                  <DocumentProgress doc={doc} />
                </article>
              ))
            ) : (
              <p className="import-empty">
                上传文件或文件夹后在这里展示流程，全部成功后自动清空。
              </p>
            )}
          </div>
          {manage && !!cleanupFailures.length && (
            <div className="folder-upload-report">
              <p>
                已移除的资料有 {cleanupFailures.length}{" "}
                个后台清理任务失败，请重试清理。
              </p>
              <button
                disabled={busy}
                onClick={() => {
                  void (async () => {
                    for (const doc of cleanupFailures)
                      await action(`/documents/${doc.id}/retry`);
                  })();
                }}
              >
                重试清理
              </button>
            </div>
          )}
        </section>
        <section
          className="knowledge-files-panel"
          aria-labelledby="files-title"
        >
          <h2 id="files-title">知识库文件</h2>
          <ul className="document-list">
            {visibleDocuments.map((doc) => (
              <li key={doc.id}>
                <div className="document-content">
                  <strong>{doc.filename}</strong>
                  <p>
                    {documentStatuses[doc.status]} · v{doc.index_version} ·{" "}
                    {(doc.size / 1024).toFixed(1)} KB
                  </p>
                  {doc.status === "failed" && (
                    <p className="import-error">
                      {documentError(doc.error_code)}
                    </p>
                  )}
                  {doc.status === "ready" && (
                    <GraphPanel
                      key={`${doc.id}-${doc.index_version}`}
                      documentId={doc.id}
                      csrf={identity.csrf_token}
                    />
                  )}
                  {doc.status === "deleting" && doc.error_code && (
                    <p className="import-error">
                      清理失败，资料已停止检索，请重试。
                    </p>
                  )}
                </div>
                <div className="document-actions">
                  {!["deleting", "deleted"].includes(doc.status) && (
                    <a href={`/api/v1/documents/${doc.id}/download`}>下载</a>
                  )}
                  {manage && (
                    <>
                      {["failed", "ready", "deleting"].includes(doc.status) && (
                        <button
                          disabled={busy}
                          onClick={() => {
                            setBatchDocumentIds([doc.id]);
                            setUploadBatch(null);
                            void action(`/documents/${doc.id}/retry`);
                          }}
                        >
                          {doc.status === "ready" ? "重新索引" : "重试"}
                        </button>
                      )}
                      {!["deleting", "deleted"].includes(doc.status) && (
                        <button
                          disabled={busy}
                          onClick={() =>
                            void action(`/documents/${doc.id}`, "DELETE")
                          }
                        >
                          删除
                        </button>
                      )}
                    </>
                  )}
                </div>
              </li>
            ))}
          </ul>
          <form
            className="retrieval-form"
            onSubmit={(event) => {
              event.preventDefault();
              setBusy(true);
              setNotice("");
              void identityRequest<{ items: Source[] }>(
                `/knowledge-bases/${base}/search`,
                "POST",
                { query },
                identity.csrf_token,
              )
                .then((result) => {
                  setSources(result.items);
                  setNotice(
                    result.items.length ? "检索完成。" : "未找到足够相关证据。",
                  );
                })
                .catch((error) => setNotice(error.message))
                .finally(() => setBusy(false));
            }}
          >
            <label>
              检索问题
              <input
                required
                maxLength={2000}
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
            <button disabled={!base || busy}>检索资料</button>
          </form>
          <Sources
            key={sources.map((source) => source.chunk_id).join(",")}
            sources={sources}
          />
          {!visibleDocuments.length && (
            <p className="import-empty">当前知识库暂无文件。</p>
          )}
        </section>
      </div>
    </section>
  );
}
