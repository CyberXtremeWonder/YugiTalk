import { useRef, type ReactNode } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import "./editor.css";

/** 新增表单内嵌显示，已有资源统一在可滚动的主题弹窗中编辑。 */
export function EditorPanel({
  editing,
  title,
  busy,
  onClose,
  children,
  inline = true,
}: {
  editing: boolean;
  title: string;
  busy: boolean;
  onClose: () => void;
  children: ReactNode;
  inline?: boolean;
}) {
  const trigger = useRef<HTMLElement | null>(null);
  return (
    <Dialog.Root
      open={editing}
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      {!editing && inline && children}
      {editing && inline && (
        <div className="editor-placeholder">
          <p>正在窗口中编辑。</p>
          <button disabled={busy} onClick={onClose}>
            返回新增
          </button>
        </div>
      )}
      <Dialog.Portal>
        <Dialog.Overlay className="agent-confirm-backdrop" />
        <Dialog.Content
          className="agent-confirm-dialog account-settings agent-form editor-dialog"
          onOpenAutoFocus={() => {
            trigger.current =
              document.activeElement instanceof HTMLElement
                ? document.activeElement
                : null;
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            if (trigger.current?.isConnected) trigger.current.focus();
            else document.getElementById("main-panel")?.focus();
          }}
          onEscapeKeyDown={(event) => {
            if (busy) event.preventDefault();
          }}
          onPointerDownOutside={(event) => {
            if (busy) event.preventDefault();
          }}
        >
          <div className="editor-dialog-heading">
            <Dialog.Title className="agent-confirm-title">{title}</Dialog.Title>
            <Dialog.Close asChild>
              <button type="button" disabled={busy} aria-label="关闭编辑窗口">
                关闭
              </button>
            </Dialog.Close>
          </div>
          <Dialog.Description className="field-hint">
            修改后请保存；关闭窗口会放弃尚未保存的修改。
          </Dialog.Description>
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
