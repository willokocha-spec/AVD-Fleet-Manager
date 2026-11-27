import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { cx } from "../utils";
import { Button, IconButton } from "./Button";

/** Selector for elements that can receive keyboard focus inside a dialog. */
const FOCUSABLE =
  'a[href], button:not([disabled]), textarea, input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  size?: "sm" | "md" | "lg";
  /** Allow Escape / backdrop click to close (default true). */
  dismissible?: boolean;
}

/**
 * Accessible modal dialog rendered in a portal: focus is trapped, Escape
 * closes, background scroll is locked and focus returns to the trigger.
 */
export function Modal({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = "md",
  dismissible = true,
}: ModalProps) {
  const generatedId = useId();
  const titleId = `modal-title-${generatedId}`;
  const descriptionId = `modal-description-${generatedId}`;
  const dialogRef = useRef<HTMLDivElement>(null);
  const previouslyFocused = useRef<Element | null>(null);

  useEffect(() => {
    if (!open) return;
    previouslyFocused.current = document.activeElement;
    const originalOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const focusTimer = window.setTimeout(() => {
      const dialog = dialogRef.current;
      if (!dialog) return;
      const first = dialog.querySelector<HTMLElement>(FOCUSABLE);
      (first ?? dialog).focus();
    }, 0);

    const onKeyDown = (event: globalThis.KeyboardEvent): void => {
      if (event.key === "Escape" && dismissible) {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (element) => element.offsetParent !== null || element === document.activeElement,
      );
      if (focusable.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0]!;
      const last = focusable[focusable.length - 1]!;
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      window.clearTimeout(focusTimer);
      document.removeEventListener("keydown", onKeyDown, true);
      document.body.style.overflow = originalOverflow;
      const target = previouslyFocused.current;
      if (target instanceof HTMLElement && target.isConnected) target.focus();
    };
  }, [dismissible, onClose, open]);

  if (!open || typeof document === "undefined") return null;

  return createPortal(
    <div
      className="ds-modal-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && dismissible) onClose();
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        className={cx("ds-modal", `ds-modal--${size}`)}
        tabIndex={-1}
      >
        <header className="ds-modal__header">
          <div>
            <h2 className="ds-modal__title" id={titleId}>
              {title}
            </h2>
            {description ? (
              <p className="ds-modal__description" id={descriptionId}>
                {description}
              </p>
            ) : null}
          </div>
          {dismissible ? <IconButton label="Close dialog" icon="close" size="sm" onClick={onClose} /> : null}
        </header>
        {children ? <div className="ds-modal__body">{children}</div> : null}
        {footer ? <footer className="ds-modal__footer">{footer}</footer> : null}
      </div>
    </div>,
    document.body,
  );
}

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description?: ReactNode;
  children?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "danger" | "primary";
  onConfirm: () => void | Promise<void>;
  onCancel: () => void;
}

/** Destructive-action confirmation built on `Modal`. */
export function ConfirmDialog({
  open,
  title,
  description,
  children,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  tone = "danger",
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const [pending, setPending] = useState(false);

  const handleConfirm = async (): Promise<void> => {
    setPending(true);
    try {
      await onConfirm();
    } finally {
      setPending(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onCancel}
      title={title}
      description={description}
      size="sm"
      dismissible={!pending}
      footer={
        <>
          <Button variant="ghost" onClick={onCancel} disabled={pending}>
            {cancelLabel}
          </Button>
          <Button
            variant={tone === "danger" ? "danger" : "primary"}
            onClick={() => void handleConfirm()}
            loading={pending}
          >
            {confirmLabel}
          </Button>
        </>
      }
    >
      {children}
    </Modal>
  );
}
