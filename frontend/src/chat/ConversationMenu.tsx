import { useState } from "react";
import "./conversation-menu.css";

/** 浏览器原生浮层支持点击外部和 Escape 收起，不受侧栏滚动区域裁切。 */
export function ConversationMenu({
  id,
  title,
  busy,
  onRename,
  onDelete,
}: {
  id: string;
  title: string;
  busy: boolean;
  onRename: () => void;
  onDelete: () => void;
}) {
  const [position, setPosition] = useState({ left: 0, top: 0 });
  const [formats, setFormats] = useState(false);
  const target = `conversation-menu-${id}`;
  return (
    <>
      <button
        type="button"
        className="conversation-menu-trigger"
        aria-label={`对话选项：${title}`}
        title="对话选项"
        popoverTarget={target}
        disabled={busy}
        onClick={(event) => {
          setFormats(false);
          const rect = event.currentTarget.getBoundingClientRect();
          setPosition({
            left: Math.max(
              8,
              Math.min(rect.right - 180, window.innerWidth - 188),
            ),
            top: Math.max(
              8,
              Math.min(rect.bottom + 4, window.innerHeight - 220),
            ),
          });
        }}
      >
        <span aria-hidden="true">…</span>
      </button>
      <div
        id={target}
        popover="auto"
        className="conversation-menu"
        style={position}
        aria-label={`${title}的对话选项`}
        onClick={(event) => {
          if (!(event.target as HTMLElement).closest("[data-keep-open]"))
            event.currentTarget.hidePopover();
        }}
      >
        {formats ? (
          <>
            <button
              type="button"
              data-keep-open
              onClick={() => setFormats(false)}
            >
              ‹ 返回
            </button>
            {(
              [
                ["pdf", "PDF"],
                ["docx", "Word"],
                ["txt", "TXT"],
                ["json", "JSON"],
              ] as const
            ).map(([format, label]) => (
              <a
                key={format}
                href={`/api/v1/conversations/${encodeURIComponent(id)}/export?format=${format}`}
                download
              >
                {label}
              </a>
            ))}
          </>
        ) : (
          <>
            <button type="button" onClick={onRename}>
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="m15 4 5 5M4 20l5-1L21 7l-5-5L4 14v6Z" />
              </svg>
              重命名
            </button>
            <button
              type="button"
              data-keep-open
              onClick={() => setFormats(true)}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M12 3v12m-4-4 4 4 4-4M4 14v6h16v-6" />
              </svg>
              导出对话
            </button>
            <button
              type="button"
              className="conversation-menu-delete"
              onClick={onDelete}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7" />
              </svg>
              删除对话
            </button>
          </>
        )}
      </div>
    </>
  );
}
