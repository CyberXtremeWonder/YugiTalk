import { useNotice } from "../shared/useNotice";
import { Fragment, useEffect, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { allItems, identityRequest, type Identity } from "../identity/api";
import { type Account } from "../accounts/api";
import {
  documentStatuses,
  chunkProfiles,
  documentChunkLabel,
  type ChunkProfile,
  uploadFiles,
  type UploadBatch,
  type Document,
  type KnowledgeBase,
  type Source,
} from "./api";
import { Sources } from "./Sources";
import { GraphPanel } from "./GraphPanel";
import { DocumentIndexProgress, DocumentProgress } from "./DocumentProgress";
import { KnowledgeMenu } from "./KnowledgeMenu";
import "./knowledge.css";

/** 管理知识库、文件与文件夹上传、处理流程及资料检索。 */
export function KnowledgeView({
  identity,
  account,
  base,
  setBase,
}: {
  identity: Identity;
  account: Account;
  base: string;
  setBase: (id: string) => void;
}) {
  const [bases, setBases] = useState<KnowledgeBase[]>([]);
  const [name, setName] = useState("");
  const [documents, setDocuments] = useState<Document[]>([]);
  const { notice, setNotice, noticeIsError, reportError } = useNotice();
  const [busy, setBusy] = useState(false);
  const [chunkProfile, setChunkProfile] = useState<ChunkProfile>("auto");
  const [profileOverrides, setProfileOverrides] = useState<
    Record<string, ChunkProfile | "">
  >({});
  const [deleteTarget, setDeleteTarget] = useState<KnowledgeBase | null>(null);
  const cancelDelete = useRef<HTMLButtonElement>(null);
  const [query, setQuery] = useState("");
  const [sources, setSources] = useState<Source[]>([]);
  const [graphMode, setGraphMode] = useState(false);
  const [graphDocument, setGraphDocument] = useState("");
  const [fileQuery, setFileQuery] = useState("");
  const [fileFilter, setFileFilter] = useState("all");
  const [expandedDocument, setExpandedDocument] = useState("");
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);
  const [uploadBatch, setUploadBatch] = useState<UploadBatch | null>(null);
  const [batchDocumentIds, setBatchDocumentIds] = useState<string[]>([]);
  const manage = true;
  useEffect(() => {
    let active = true;
    void allItems<KnowledgeBase>(`/me/knowledge-bases`)
      .then((rows) => {
        if (active) {
          setBases(rows);
          if (!rows.some((row) => row.id === base)) setBase(rows[0]?.id || "");
        }
      })
      .catch((error) => {
        if (active) reportError(error.message);
      });
    return () => {
      active = false;
    };
  }, [account.id, base, setBase]);
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
        // 上传结束不代表索引结束；仍有后台任务时继续轮询，全部稳定后停止。
        if (
          busy ||
          rows.some((row) =>
            ["queued", "parsing", "indexing", "deleting"].includes(row.status),
          )
        )
          timer = setTimeout(() => void refresh(), 2000);
      } catch (error) {
        if (active)
          reportError(error instanceof Error ? error.message : "加载失败");
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
            // 上传去重可能返回已有文档 ID，先替换同 ID 条目再追加，避免列表出现两份。
            setBatchDocumentIds((ids) => [...new Set([...ids, doc.id])]);
            setDocuments((rows) => [
              ...rows.filter((row) => row.id !== doc.id),
              doc,
            ]);
          }
        },
        chunkProfile,
      );
      setNotice(
        result.total
          ? `上传${result.stopped ? "已停止" : "完成"}：成功 ${result.uploaded}，失败 ${result.failed}，跳过 ${result.skipped}。相同文件自动去重。`
          : "所选文件夹中没有支持的 TXT、Markdown、PDF、Excel、Word 或图片文件。",
      );
    } catch (error) {
      reportError(error instanceof Error ? error.message : "上传失败");
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
      reportError(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }
  /** 确认后删除当前知识库，并清空该库关联的页面状态。 */
  async function deleteBase() {
    const selected = deleteTarget;
    if (!selected || busy) return;
    setBusy(true);
    setNotice("");
    try {
      await identityRequest(
        `/knowledge-bases/${selected.id}`,
        "DELETE",
        undefined,
        identity.csrf_token,
      );
      const remaining = bases.filter((row) => row.id !== selected.id);
      setBases(remaining);
      setBase(remaining[0]?.id || "");
      setDocuments([]);
      setSources([]);
      setQuery("");
      setUploadBatch(null);
      setBatchDocumentIds([]);
      setDeleteTarget(null);
      setNotice("知识库已删除。");
    } catch (error) {
      reportError(error instanceof Error ? error.message : "删除知识库失败");
    } finally {
      setBusy(false);
    }
  }
  const workflowDocuments = documents.filter(
    // 进行中的导入始终可见；失败项优先展示本轮上传，清理失败另行提供重试入口。
    (doc) =>
      ["queued", "parsing", "indexing"].includes(doc.status) ||
      (doc.status === "failed" &&
        (!batchDocumentIds.length || batchDocumentIds.includes(doc.id))),
  );
  const visibleDocuments = documents.filter(
    (doc) => !["deleting", "deleted"].includes(doc.status),
  );
  const cleanupFailures = documents.filter(
    (doc) => doc.status === "deleting" && doc.error_code,
  );
  const graphDocuments = visibleDocuments.filter(
    (doc) => doc.status === "ready",
  );
  const selectedGraphDocument =
    graphDocuments.find((doc) => doc.id === graphDocument) || graphDocuments[0];
  if (graphMode)
    return (
      <section
        className="secondary-view knowledge-view knowledge-graph-view"
        aria-labelledby="view-title"
      >
        <div className="knowledge-page-heading">
          <div>
            <h1 id="view-title">知识图谱</h1>
            <p>探索实体关系，追溯原文证据</p>
          </div>
          <div className="knowledge-mode-switch">
            <button onClick={() => setGraphMode(false)}>文档管理</button>
            <button aria-pressed="true">知识图谱</button>
          </div>
        </div>
        <div className="graph-document-toolbar">
          <label>
            当前知识库
            <select
              aria-label="当前知识库"
              value={base}
              onChange={(event) => {
                setBase(event.target.value);
                setDocuments([]);
                setGraphDocument("");
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
          <label>
            图谱文档
            <select
              aria-label="图谱文档"
              value={selectedGraphDocument?.id || ""}
              onChange={(event) => setGraphDocument(event.target.value)}
            >
              {!graphDocuments.length && (
                <option value="">暂无已索引文档</option>
              )}
              {graphDocuments.map((doc) => (
                <option key={doc.id} value={doc.id}>
                  {doc.filename}
                </option>
              ))}
            </select>
          </label>
          <p>选择文档，查看实体关系与原文。</p>
        </div>
        {selectedGraphDocument ? (
          <GraphPanel
            key={`${selectedGraphDocument.id}-${selectedGraphDocument.index_version}`}
            documentId={selectedGraphDocument.id}
            csrf={identity.csrf_token}
            expanded
          />
        ) : (
          <div className="graph-no-document">
            <h2>从一份资料开始探索</h2>
            <p>先上传并完成文档索引，再构建知识图谱。</p>
            <button onClick={() => setGraphMode(false)}>前往文档管理</button>
          </div>
        )}
      </section>
    );
  const groups = [
    { key: "all", label: "全部", matches: () => true },
    {
      key: "ready",
      label: "可用",
      matches: (doc: Document) => doc.status === "ready",
    },
    {
      key: "processing",
      label: "处理中",
      matches: (doc: Document) =>
        ["queued", "parsing", "indexing"].includes(doc.status),
    },
    {
      key: "failed",
      label: "失败",
      matches: (doc: Document) => doc.status === "failed",
    },
  ];
  const selectedGroup =
    groups.find((group) => group.key === fileFilter) || groups[0];
  const filteredDocuments = visibleDocuments.filter(
    (doc) =>
      selectedGroup.matches(doc) &&
      doc.filename.toLowerCase().includes(fileQuery.trim().toLowerCase()),
  );
  return (
    <section
      className="secondary-view knowledge-view knowledge-manager"
      aria-labelledby="view-title"
    >
      <div className="knowledge-page-heading">
        <div>
          <h1 id="view-title">知识库</h1>
          <p>让每一份资料，都成为可追溯的知识。</p>
        </div>
        <div className="knowledge-mode-switch">
          <button aria-pressed="true">文档管理</button>
          <button onClick={() => setGraphMode(true)}>知识图谱</button>
        </div>
      </div>
      <div className="km-context">
        <label className="km-base-picker">
          <span aria-hidden="true">▣</span>
          <span className="sr-only">当前知识库</span>
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
              setFileQuery("");
              setFileFilter("all");
              setExpandedDocument("");
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
        {manage && (
          <form
            className="km-create-form"
            onSubmit={(event) => {
              event.preventDefault();
              if (!name.trim()) return;
              setBusy(true);
              setNotice("");
              void identityRequest<KnowledgeBase>(
                "/me/knowledge-bases",
                "POST",
                { name: name.trim() },
                identity.csrf_token,
              )
                .then(async (created) => {
                  setBases(
                    await allItems<KnowledgeBase>("/me/knowledge-bases"),
                  );
                  setBase(created.id);
                  setDocuments([]);
                  setSources([]);
                  setUploadBatch(null);
                  setBatchDocumentIds([]);
                  setName("");
                  setNotice("知识库已创建。");
                })
                .catch((error) => reportError(error.message))
                .finally(() => setBusy(false));
            }}
          >
            <label>
              知识库名称
              <input
                required
                maxLength={80}
                value={name}
                onChange={(event) => setName(event.target.value)}
                autoComplete="off"
              />
            </label>
            <button disabled={busy}>创建知识库</button>
          </form>
        )}
        {manage && (
          <div className="km-base-actions">
            <button
              type="button"
              className="km-delete-base km-danger"
              disabled={!base || busy}
              onClick={() => {
                setNotice("");
                setDeleteTarget(bases.find((row) => row.id === base) || null);
              }}
            >
              删除知识库
            </button>
          </div>
        )}
      </div>
      {notice && (
        <p
          className={`km-notice ${noticeIsError ? "km-error" : ""}`}
          role={noticeIsError ? "alert" : "status"}
        >
          {notice}
        </p>
      )}
      <div className="km-layout">
        <div className="km-main">
          <section
            className="km-card km-documents"
            aria-labelledby="files-title"
          >
            <header className="km-card-heading">
              <h2 id="files-title">
                文档 <span className="km-count">{visibleDocuments.length}</span>
              </h2>
              <label className="km-search">
                <span aria-hidden="true">⌕</span>
                <input
                  aria-label="搜索文件名称"
                  placeholder="搜索文件名称…"
                  value={fileQuery}
                  onChange={(event) => setFileQuery(event.target.value)}
                />
              </label>
            </header>
            <div className="km-filters" aria-label="文档状态筛选">
              {groups.map((group) => (
                <button
                  key={group.key}
                  aria-pressed={fileFilter === group.key}
                  onClick={() => setFileFilter(group.key)}
                >
                  {group.label}{" "}
                  <span>{visibleDocuments.filter(group.matches).length}</span>
                </button>
              ))}
            </div>
            <div className="km-table-scroll">
              <table className="km-file-table">
                <thead>
                  <tr>
                    <th>文件名称</th>
                    <th>状态</th>
                    <th>大小</th>
                    <th>导入时间</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredDocuments.map((doc) => (
                    <Fragment key={doc.id}>
                      <tr>
                        <td>
                          <div className="km-file-name">
                            <span
                              className="km-file-icon"
                              data-extension={doc.filename
                                .split(".")
                                .pop()
                                ?.toLowerCase()}
                              aria-hidden="true"
                            >
                              {doc.filename
                                .split(".")
                                .pop()
                                ?.slice(0, 4)
                                .toUpperCase()}
                            </span>
                            <div>
                              <button
                                className="km-document-name"
                                title={doc.filename}
                                onClick={() =>
                                  setExpandedDocument((value) =>
                                    value === doc.id ? "" : doc.id,
                                  )
                                }
                              >
                                {doc.filename}
                              </button>
                              <small
                                title={documentChunkLabel(doc.chunk_strategy)}
                              >
                                {documentChunkLabel(doc.chunk_strategy)} · v
                                {doc.index_version}
                              </small>
                            </div>
                          </div>
                        </td>
                        <td>
                          <span className="km-status" data-status={doc.status}>
                            <span aria-hidden="true">●</span>
                            {documentStatuses[doc.status] || "未知状态"}
                          </span>
                          {["queued", "parsing", "indexing"].includes(
                            doc.status,
                          ) && <DocumentIndexProgress doc={doc} />}
                        </td>
                        <td className="km-file-size">
                          {doc.size >= 1024 * 1024
                            ? `${(doc.size / 1024 / 1024).toFixed(1)} MB`
                            : `${(doc.size / 1024).toFixed(1)} KB`}
                        </td>
                        <td className="km-file-date">
                          {doc.created_at ? (
                            <time dateTime={doc.created_at}>
                              {new Date(doc.created_at).toLocaleString(
                                "zh-CN",
                                {
                                  month: "2-digit",
                                  day: "2-digit",
                                  hour: "2-digit",
                                  minute: "2-digit",
                                },
                              )}
                            </time>
                          ) : (
                            "—"
                          )}
                        </td>
                        <td>
                          <div className="km-file-actions">
                            <KnowledgeMenu
                              label={`文件选项：${doc.filename}`}
                              disabled={busy}
                            >
                              <a
                                data-close
                                href={`/api/v1/documents/${doc.id}/download`}
                              >
                                下载文件
                              </a>
                              <button
                                data-close
                                onClick={() =>
                                  setExpandedDocument((value) =>
                                    value === doc.id ? "" : doc.id,
                                  )
                                }
                              >
                                处理详情
                              </button>
                              {doc.status === "ready" && (
                                <button
                                  data-close
                                  onClick={() => {
                                    setGraphDocument(doc.id);
                                    setGraphMode(true);
                                  }}
                                >
                                  查看知识图谱
                                </button>
                              )}
                              {["failed", "ready"].includes(doc.status) && (
                                <>
                                  <label>
                                    重新分块
                                    <select
                                      aria-label={`分块类型：${doc.filename}`}
                                      disabled={busy}
                                      value={profileOverrides[doc.id] || ""}
                                      onChange={(event) =>
                                        setProfileOverrides((values) => ({
                                          ...values,
                                          [doc.id]: event.target.value as
                                            ChunkProfile | "",
                                        }))
                                      }
                                    >
                                      <option value="">保留当前规则</option>
                                      {Object.entries(chunkProfiles).map(
                                        ([value, label]) => (
                                          <option key={value} value={value}>
                                            {label}
                                          </option>
                                        ),
                                      )}
                                    </select>
                                  </label>
                                  <button
                                    data-close
                                    disabled={busy}
                                    onClick={() => {
                                      setBatchDocumentIds([doc.id]);
                                      setUploadBatch(null);
                                      const override = profileOverrides[doc.id];
                                      void action(
                                        `/documents/${doc.id}/retry${override ? `?chunk_strategy=${override}` : ""}`,
                                      );
                                    }}
                                  >
                                    {doc.status === "ready"
                                      ? "重新索引"
                                      : "重试"}
                                  </button>
                                </>
                              )}
                            </KnowledgeMenu>
                            <button
                              className="km-delete-file km-danger"
                              aria-label={`删除文件：${doc.filename}`}
                              disabled={busy}
                              onClick={() =>
                                void action(`/documents/${doc.id}`, "DELETE")
                              }
                            >
                              删除
                            </button>
                          </div>
                        </td>
                      </tr>
                      {expandedDocument === doc.id && (
                        <tr className="km-detail-row">
                          <td colSpan={5}>
                            <strong>{doc.filename}</strong>
                            <p>
                              {documentChunkLabel(doc.chunk_strategy)} · 版本{" "}
                              {doc.index_version}
                            </p>
                            <DocumentProgress doc={doc} />
                            {doc.status === "ready" && (
                              <p>
                                资料已可检索。可在文件菜单中查看知识图谱或调整分块重新索引。
                              </p>
                            )}
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
              {!filteredDocuments.length && (
                <div className="km-empty">
                  <span aria-hidden="true">▤</span>
                  <h3>
                    {visibleDocuments.length
                      ? "没有匹配的文件"
                      : "从第一份资料开始"}
                  </h3>
                  <p>
                    {visibleDocuments.length
                      ? "试试其他文件名称或状态筛选。"
                      : base
                        ? "在右侧添加文件，让知识库拥有可检索的内容。"
                        : "先新建一个知识库，再添加资料。"}
                  </p>
                </div>
              )}
            </div>
            <footer className="km-table-footer">
              共 {visibleDocuments.length} 个文件
              {filteredDocuments.length !== visibleDocuments.length
                ? ` · 当前显示 ${filteredDocuments.length} 个`
                : ""}
            </footer>
          </section>
          <section
            className="km-card km-retrieval"
            aria-labelledby="retrieval-title"
          >
            <h2 id="retrieval-title">检索验证</h2>
            <p>输入一个问题，检查资料是否能被找到。</p>
            <form
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
                      result.items.length
                        ? "检索完成。"
                        : "未找到足够相关证据。",
                    );
                  })
                  .catch((error) => reportError(error.message))
                  .finally(() => setBusy(false));
              }}
            >
              <input
                aria-label="检索问题"
                placeholder="例如：Plan-and-Solve 如何减少推理错误？"
                required
                maxLength={2000}
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
              <button disabled={!base || busy}>检索资料</button>
            </form>
            <Sources
              key={sources.map((source) => source.chunk_id).join(",")}
              sources={sources}
            />
          </section>
        </div>
        <aside className="km-rail" aria-label="添加资料与导入任务">
          <section className="km-card km-upload" aria-labelledby="import-title">
            <h2 id="import-title">添加资料</h2>
            <p>支持文件与文件夹批量导入</p>
            <div
              className="km-dropzone"
              data-dragging={dragging}
              data-disabled={!base || busy}
              onDragOver={(event) => {
                event.preventDefault();
                if (base && !busy) setDragging(true);
              }}
              onDragLeave={(event) => {
                if (
                  !event.currentTarget.contains(
                    event.relatedTarget as Node | null,
                  )
                )
                  setDragging(false);
              }}
              onDrop={(event) => {
                event.preventDefault();
                setDragging(false);
                if (base && !busy && event.dataTransfer.files.length)
                  void importFiles(Array.from(event.dataTransfer.files));
              }}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M12 16V3m-5 5 5-5 5 5M4 15v6h16v-6" />
              </svg>
              <strong>将文件拖到这里</strong>
              <span>或选择文件 / 文件夹</span>
              <div className="km-upload-buttons">
                <button
                  type="button"
                  disabled={!base || busy}
                  onClick={() => fileInput.current?.click()}
                >
                  选择文件
                </button>
                <button
                  type="button"
                  disabled={!base || busy}
                  onClick={() => folderInput.current?.click()}
                >
                  选择文件夹
                </button>
              </div>
              <small>PDF、Word、Excel、TXT、图片等</small>
              <input
                className="sr-only"
                ref={fileInput}
                type="file"
                multiple
                aria-label="上传资料（JSONL 最多 5 GB，其他文件最多 20 MB）"
                accept=".txt,.md,.jsonl,.pdf,.xlsx,.xls,.docx,.doc,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp"
                disabled={!base || busy}
                onChange={(event) => {
                  const files = Array.from(event.target.files || []);
                  if (files.length) void importFiles(files);
                  event.target.value = "";
                }}
              />
              <input
                className="sr-only"
                type="file"
                multiple
                aria-label="上传文件夹"
                disabled={!base || busy}
                ref={(node) => {
                  folderInput.current = node;
                  node?.setAttribute("webkitdirectory", "");
                  node?.setAttribute("directory", "");
                }}
                onChange={(event) => {
                  const files = Array.from(event.target.files || []);
                  if (files.length) void importFiles(files);
                  event.target.value = "";
                }}
              />
            </div>
            <label className="km-chunk-picker">
              文档分块类型
              <select
                aria-label="文档分块类型"
                value={chunkProfile}
                disabled={busy}
                onChange={(event) =>
                  setChunkProfile(event.target.value as ChunkProfile)
                }
              >
                {Object.entries(chunkProfiles).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <details className="km-upload-help">
              <summary>查看支持格式与大小限制</summary>
              <p>
                支持 TXT、Markdown、JSONL、PDF、Excel、Word 和图片。JSONL
                单文件最多 5 GB，其他文件最多 20 MB；文件夹递归导入，一次最多
                200 个支持的文件。
              </p>
              <p>
                自动识别章节结构、文本长度和语言；手动分块设置应用于本次上传。
              </p>
            </details>
          </section>
          <section className="km-card km-tasks" aria-label="导入任务">
            <h2>导入任务</h2>
            {uploadBatch && (
              <section
                className="km-upload-batch"
                aria-label="批量上传进度"
                aria-live="polite"
              >
                <p>
                  已上传 {uploadBatch.completed}/{uploadBatch.total} · 成功{" "}
                  {uploadBatch.uploaded} · 失败 {uploadBatch.failed} · 跳过{" "}
                  {uploadBatch.skipped}
                </p>
                {uploadBatch.current && <p>正在上传：{uploadBatch.current}</p>}
                {uploadBatch.stopped && (
                  <p>
                    上传已停止，尚有 {uploadBatch.total - uploadBatch.completed}{" "}
                    个文件未上传。
                  </p>
                )}
                {!!uploadBatch.errors.length && (
                  <details>
                    <summary>查看上传错误</summary>
                    <ul>
                      {uploadBatch.errors.map((row, i) => (
                        <li key={i}>
                          {row.name}：{row.message}
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
              </section>
            )}
            {workflowDocuments.length ? (
              workflowDocuments.map((doc) => (
                <article
                  className="km-task"
                  key={doc.id}
                  aria-label={`文件流程：${doc.filename}`}
                >
                  <strong>{doc.filename}</strong>
                  <DocumentIndexProgress doc={doc} />
                  <details>
                    <summary>处理详情</summary>
                    <DocumentProgress doc={doc} />
                  </details>
                </article>
              ))
            ) : (
              <p className="km-task-empty">
                当前没有进行中的导入。添加资料后，会在这里显示处理进度。
              </p>
            )}
            {!!cleanupFailures.length && (
              <div className="km-task">
                <p>有 {cleanupFailures.length} 个文件的后台清理暂未完成。</p>
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
        </aside>
      </div>
      <Dialog.Root
        open={deleteTarget !== null}
        onOpenChange={(open) => {
          if (busy) return;
          setDeleteTarget(
            open ? (bases.find((row) => row.id === base) ?? null) : null,
          );
          if (open) setNotice("");
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="agent-confirm-backdrop" />
          <Dialog.Content
            className="agent-confirm-dialog"
            onOpenAutoFocus={(event) => {
              event.preventDefault();
              cancelDelete.current?.focus();
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
            <Dialog.Title className="agent-confirm-title">
              删除知识库
            </Dialog.Title>
            <Dialog.Description className="agent-confirm-description">
              确定删除知识库“{deleteTarget?.name}
              ”吗？库内所有文件也会被删除，此操作无法撤销。
            </Dialog.Description>
            {notice && (
              <p role={noticeIsError ? "alert" : "status"}>{notice}</p>
            )}
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
                onClick={() => void deleteBase()}
              >
                {busy ? "删除中…" : "确认删除"}
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </section>
  );
}
