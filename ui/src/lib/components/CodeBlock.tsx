import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";

import { copyText, cx } from "../utils";
import { Icon } from "./Icon";

/* -------------------------------------------------------------------------- */
/* Copy button (shared by CodeBlock / LogViewer)                               */
/* -------------------------------------------------------------------------- */

function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  const handleCopy = async (): Promise<void> => {
    const ok = await copyText(text);
    if (!ok) return;
    setCopied(true);
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setCopied(false), 1600);
  };

  return (
    <button type="button" className="ds-code__copy" onClick={() => void handleCopy()}>
      <Icon name={copied ? "check" : "copy"} size={14} aria-hidden="true" />
      <span>{copied ? "Copied" : label}</span>
    </button>
  );
}

/* -------------------------------------------------------------------------- */
/* CodeBlock                                                                   */
/* -------------------------------------------------------------------------- */

export interface CodeBlockProps {
  code: string;
  language?: string;
  filename?: string;
  maxHeight?: number | string;
  copyable?: boolean;
  wrap?: boolean;
  className?: string;
}

export function CodeBlock({
  code,
  language,
  filename,
  maxHeight = 320,
  copyable = true,
  wrap = false,
  className,
}: CodeBlockProps) {
  const style: CSSProperties = { maxHeight };
  return (
    <div className={cx("ds-code", className)}>
      {filename || language || copyable ? (
        <div className="ds-code__header">
          <span className="ds-code__filename">
            {filename ? <Icon name="file" size={14} aria-hidden="true" /> : null}
            {filename ?? language?.toUpperCase() ?? "Code"}
          </span>
          {copyable ? <CopyButton text={code} /> : null}
        </div>
      ) : null}
      <pre className={cx("ds-code__pre", wrap && "ds-code__pre--wrap")} style={style} tabIndex={0}>
        <code>{code}</code>
      </pre>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* LogViewer                                                                   */
/* -------------------------------------------------------------------------- */

export type LogStream = "stdout" | "stderr" | "system";

export interface LogLine {
  text: string;
  stream?: LogStream;
}

export interface LogViewerProps {
  lines: Array<LogLine | string>;
  title?: string;
  emptyLabel?: string;
  maxHeight?: number | string;
  autoScroll?: boolean;
  showLineNumbers?: boolean;
  className?: string;
  actions?: ReactNode;
}

/** Mono-spaced, scrollable viewer with stream colouring and copy-all. */
export function LogViewer({
  lines,
  title,
  emptyLabel = "No output yet.",
  maxHeight = 360,
  autoScroll = true,
  showLineNumbers = false,
  className,
  actions,
}: LogViewerProps) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const normalized: LogLine[] = lines.map((line) =>
    typeof line === "string" ? { text: line, stream: "stdout" } : line,
  );
  const plain = normalized.map((line) => line.text).join("\n");

  useEffect(() => {
    if (!autoScroll) return;
    const body = bodyRef.current;
    if (body) body.scrollTop = body.scrollHeight;
  }, [autoScroll, plain]);

  return (
    <div className={cx("ds-logs", className)}>
      <div className="ds-logs__header">
        <span className="ds-logs__title">{title ?? "Output"}</span>
        <div className="ds-logs__actions">
          {actions}
          {normalized.length > 0 ? <CopyButton text={plain} label="Copy all" /> : null}
        </div>
      </div>
      <div
        ref={bodyRef}
        className="ds-logs__body"
        style={{ maxHeight }}
        role="log"
        aria-label={title ?? "Command output"}
        aria-live="polite"
        tabIndex={0}
      >
        {normalized.length === 0 ? (
          <p className="ds-logs__empty">{emptyLabel}</p>
        ) : (
          normalized.map((line, index) => (
            <div key={index} className={cx("ds-logs__line", `ds-logs__line--${line.stream ?? "stdout"}`)}>
              {showLineNumbers ? (
                <span className="ds-logs__lineno" aria-hidden="true">
                  {index + 1}
                </span>
              ) : null}
              <span className="ds-logs__text">{line.text || "\u00a0"}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
