import { useId, useState, type ReactNode } from "react";

/** 文档和知识库共用原生浮层，支持 Escape、点击外部收起及滚动区域外显示。 */
export function KnowledgeMenu({
  label,
  disabled,
  children,
}: {
  label: string;
  disabled: boolean;
  children: ReactNode;
}) {
  const id = useId();
  const [position, setPosition] = useState({ left: 0, top: 0 });
  return (
    <>
      <button
        type="button"
        className="km-more"
        aria-label={label}
        title={label}
        disabled={disabled}
        popoverTarget={id}
        onClick={(event) => {
          const rect = event.currentTarget.getBoundingClientRect();
          setPosition({
            left: Math.max(8, Math.min(rect.right - 240, innerWidth - 248)),
            top: Math.max(8, Math.min(rect.bottom + 5, innerHeight - 330)),
          });
        }}
      >
        ···
      </button>
      <div
        id={id}
        className="library-action-menu"
        popover="auto"
        style={position}
        onClick={(event) => {
          if ((event.target as Element).closest("[data-close]"))
            event.currentTarget.hidePopover();
        }}
      >
        {children}
      </div>
    </>
  );
}
