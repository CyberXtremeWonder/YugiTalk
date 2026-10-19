import { useState } from "react";
import { identityRequest } from "../identity/api";
import type { Source } from "./api";

/** 展示回复来源并按需读取原文，便于核对资料依据。 */
export function Sources({ sources }: { sources: Source[] }) {
  const [preview, setPreview] = useState("");
  if (!sources.length) return null;
  return (
    <details className="source-list">
      <summary>检索来源（{sources.length}）</summary>
      <ul>
        {sources.map((source) => (
          <li key={source.chunk_id}>
            <button
              onClick={() => {
                // 点击时重新授权，已删除或失去权限的原文不从浏览器缓存读取。
                const query = new URLSearchParams();
                if (source.evidence_id)
                  query.set("evidence_id", source.evidence_id);
                if (source.index_version)
                  query.set("index_version", String(source.index_version));
                if (source.merged && source.parent_id)
                  query.set("parent_id", source.parent_id);
                void identityRequest<Source>(
                  `/sources/${source.chunk_id}?${query}`,
                )
                  .then((row) => setPreview(row.content || ""))
                  .catch(() => setPreview("来源已删除、更新或无权访问。"));
              }}
            >
              {source.filename} ·{" "}
              {source.page
                ? `第 ${source.page} 页`
                : `第 ${source.paragraph} 段`}
            </button>
            {source.evidence_id && (
              <small title="绑定资料版本的证据标识">{source.evidence_id}</small>
            )}
          </li>
        ))}
      </ul>
      {preview && <p className="source-preview">{preview}</p>}
    </details>
  );
}
