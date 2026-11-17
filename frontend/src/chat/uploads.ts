import { ApiError } from "../identity/api";
import { monitoredFetch } from "../shared/monitoring";

export interface ChatFile {
  filename: string;
  size: number;
  text: string;
  truncated: boolean;
}

/** 上传用户选定的单文件，仅在服务端成功解析后允许随消息发送。 */
export async function uploadChatFile(
  file: File,
  csrf: string,
): Promise<ChatFile> {
  if (!/\.(docx|pdf|txt|md|csv|json|log|rst)$/i.test(file.name))
    throw new Error(
      "支持 Word .docx、PDF、TXT、Markdown、CSV、JSON 等文本文件；旧 .doc 请先转换。",
    );
  if (file.size > 20 * 1024 * 1024) throw new Error("单个文件最多 20 MiB。");
  const response = await monitoredFetch("/api/v1/chat/uploads", {
    signal: AbortSignal.timeout(60000),
    method: "POST",
    credentials: "same-origin",
    headers: {
      "X-CSRF-Token": csrf,
      "X-Filename": encodeURIComponent(file.name),
      "Content-Type": "application/octet-stream",
    },
    body: file,
  });
  if (!response.ok)
    throw new ApiError(
      response.status,
      response.status === 401
        ? "登录已过期，请重新登录。"
        : response.status === 413
          ? "单个文件最多 20 MiB。"
          : "文件无法解析，请检查格式、加密或编码；扫描版 PDF 需先识别文字。",
    );
  return response.json();
}
