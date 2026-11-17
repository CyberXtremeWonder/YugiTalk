import type { Attachment } from "./api";
import "./attachments.css";

/** 文件链接仅接受后端附件接口路径，来源失效时不再提供下载。 */
export function Attachments({ files }: { files: Attachment[] }) {
  if (!files.length) return null;
  return (
    <ul className="chat-attachments" aria-label="生成的文件">
      {files.map((file, index) => (
        <li key={index}>
          <span className="attachment-icon" aria-hidden="true">
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
            >
              <path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8Z" />
              <path d="M14 3v5h5M8 12h8M8 16h6" />
            </svg>
          </span>
          <span className="attachment-info">
            <span className="attachment-name" title={file.filename}>
              {file.filename}
            </span>
            <small>{Math.ceil(file.size / 1024)} KB</small>
          </span>
          {file.available &&
          file.download_url &&
          /^\/api\/v1\/research\/[a-f0-9-]+\/files\/(docx|pdf|md|zip|picture-[0-5])$/.test(
            file.download_url,
          ) ? (
            <a
              className="attachment-download"
              href={file.download_url}
              download={file.filename}
              aria-label={`下载 ${file.filename}`}
            >
              下载
            </a>
          ) : (
            <span className="attachment-unavailable">
              {file.available ? "下载链接无效，请刷新页面" : "来源已失效"}
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}
