import { ApiError } from "../identity/api";
import { monitoredFetch } from "../shared/monitoring";

export interface KnowledgeBase {
  id: string;
  name: string;
}
export interface Document {
  id: string;
  filename: string;
  size: number;
  status: string;
  error_code: string | null;
  index_version: number;
  progress?: {
    stage?: string;
    failed_stage?: string;
    total_chunks?: number;
    embedded_chunks?: number;
    indexed_chunks?: number;
    batch_number?: number;
  };
}
export interface Source {
  label: string;
  chunk_id: string;
  document_id: string;
  filename: string;
  page: number | null;
  paragraph: number;
  content?: string;
  score?: number;
  evidence_id?: string;
  index_version?: number;
  parent_id?: string | null;
  merged?: boolean;
}

export const documentStatuses: Record<string, string> = {
  queued: "待处理",
  parsing: "解析中",
  indexing: "索引中",
  ready: "可用",
  failed: "失败",
  deleting: "清理中",
  deleted: "已删除",
};

export interface UploadBatch {
  total: number;
  completed: number;
  uploaded: number;
  failed: number;
  skipped: number;
  current: string;
  stopped: boolean;
  errors: { name: string; message: string }[];
}

// 浏览器文件夹选择器提供递归文件列表；逐个上传，避免同时读入多个大文件。
/** 顺序上传支持的文件，累计成功、失败和不支持格式的跳过数量。 */
export async function uploadFiles(
  base: string,
  files: File[],
  csrf: string,
  onProgress: (batch: UploadBatch, doc?: Document) => void,
): Promise<UploadBatch> {
  const supported = files.filter((file) => /\.(txt|md|pdf)$/i.test(file.name));
  if (supported.length > 200)
    throw new Error("一次最多导入 200 个支持的文件，请拆分文件夹。");
  const batch: UploadBatch = {
    total: supported.length,
    completed: 0,
    uploaded: 0,
    failed: 0,
    skipped: files.length - supported.length,
    current: "",
    stopped: false,
    errors: [],
  };
  /** 通知调用方当前上传进度，复制状态避免后续修改污染历史。 */
  const publish = (doc?: Document) =>
    onProgress({ ...batch, errors: [...batch.errors] }, doc);
  publish();
  for (const file of supported) {
    // 展示相对目录供定位，但请求只传 file.name；不会把用户本机目录当作服务器存储路径。
    batch.current = file.webkitRelativePath || file.name;
    publish();
    try {
      const doc = await uploadFile(base, file, csrf);
      batch.uploaded += 1;
      // 此处“成功”仅表示资料已被接受；解析和索引状态由文档轮询独立追踪。
      batch.completed += 1;
      publish(doc);
    } catch (error) {
      batch.failed += 1;
      batch.completed += 1;
      batch.errors.push({
        name: batch.current,
        message: error instanceof Error ? error.message : "上传失败",
      });
      // 登录失效、权限撤销或空间资料达到上限时停止后续请求，其余单文件失败继续。
      if (error instanceof ApiError && [401, 403, 409].includes(error.status))
        batch.stopped = true;
      publish();
      if (batch.stopped) break;
    }
  }
  batch.current = "";
  publish();
  return { ...batch, errors: [...batch.errors] };
}
/** 将资料处理错误码转换为对应步骤的中文说明。 */
export function documentError(code: string | null): string {
  const errors: Record<string, string> = {
    pdf_requires_ocr: "扫描 PDF 暂不支持，请使用含文本的 PDF。",
    invalid_pdf: "PDF 文件格式损坏。",
    invalid_text: "请上传 UTF-8 文本文件。",
    empty_document: "文件中没有可用文本。",
    pdf_encrypted_or_too_many_pages: "不支持加密 PDF 或超过 200 页的 PDF。",
    embedding_configuration_changed: "嵌入模型已变化，请重新索引。",
    document_cleanup_failed: "清理失败，资料已停止检索，可重试清理。",
    queue_unavailable: "队列不可用，请重试。",
    document_storage_read_failed: "读取原文失败，请检查文件存储服务。",
    document_parse_failed: "文件解析失败，请检查文件是否完整。",
    document_parse_timeout: "文件解析超时，请缩小文件后重试。",
    document_index_timeout:
      "文件索引任务超过执行时限，已完成数量保留；请调整索引时限或拆分文件后重试。",
    document_worker_lost: "导入 Worker 多次中断，请检查 Worker 后重试。",
    vector_prepare_failed: "准备向量索引失败，请检查 Milvus 状态和集合维度。",
    vector_schema_mismatch:
      "Milvus 集合结构与当前配置不兼容，请检查向量维度、文本分词器和 BM25 索引。",
    vector_store_unavailable:
      "Milvus 返回服务错误，暂时无法准备索引，请检查服务日志后重试。",
    vector_prepare_timeout: "Milvus 索引准备请求超时，请检查服务负载后重试。",
    document_chunk_storage_failed:
      "准备分块时写入数据库失败，请检查提取文本及数据库日志；并非向量编码失败。",
    vector_write_failed: "写入向量失败，请检查 Milvus 状态后重试。",
    embedding_timeout: "Embedding 请求超时，请检查模型服务或稍后重试。",
    embedding_auth_failed: "Embedding 鉴权失败，请检查模型服务密钥。",
    embedding_rate_limited: "Embedding 服务限流，请稍后重试。",
    embedding_service_failed:
      "Embedding 调用失败，请检查模型服务是否启动及其日志。",
    embedding_dimension_mismatch: "Embedding 返回的向量维度或数值不符合配置。",
    embedding_response_invalid: "Embedding 返回格式或分块顺序不正确。",
    embedding_zero_vector: "Embedding 返回空向量，无法建立索引。",
    embedding_input_invalid: "Embedding 输入不符合限制，请检查分块内容。",
    text_too_large: "文本超过 50 万字符，请拆分文件。",
    too_many_chunks: "分块超过 1000 个，请拆分文件。",
    pdf_page_too_large: "PDF 单页内容过大，请拆分或重新导出。",
    empty_or_oversized_file: "文件为空或超过大小限制。",
    unsupported_file_type: "不支持该文件类型。",
  };
  return code
    ? errors[code] || "导入失败，请检查嵌入服务和存储状态后重试。"
    : "";
}

// 二进制上传独立于 JSON 请求，凭证保持同源 Cookie，文件名只作为查询参数。
/** 发送原始文件内容及安全文件名，并处理资料上传响应。 */
export async function uploadFile(
  base: string,
  file: File,
  csrf: string,
): Promise<Document> {
  if (!file.size || file.size > 20 * 1024 * 1024)
    throw new Error("请选择非空且不超过 20 MB 的文件。");
  const response = await monitoredFetch(
    `/api/v1/knowledge-bases/${base}/documents?filename=${encodeURIComponent(file.name)}`,
    {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/octet-stream",
        "X-CSRF-Token": csrf,
      },
      body: file,
    },
  );
  if (!response.ok)
    throw new ApiError(
      response.status,
      response.status === 413
        ? "文件超过 20 MB。"
        : response.status === 401
          ? "登录已失效，请重新登录后上传。"
          : response.status === 409
            ? "知识库已达到 200 个资料上限，请删除部分资料或新建知识库。"
            : response.status === 403
              ? "没有上传权限，请联系空间管理员。"
              : "上传失败，请检查文件格式和服务状态。",
    );
  return response.json();
}
