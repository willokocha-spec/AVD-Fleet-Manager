/**
 * Shared presentation helpers for the catalogue pages (Images, Templates,
 * Proxies). Page-private modules live outside the `pages/*.tsx` route glob so
 * they are never registered as pages.
 *
 * These helpers intentionally only compose the U0 design system plus a small
 * amount of inline layout CSS: page agents are not allowed to edit the global
 * stylesheets.
 */
import type { CSSProperties, ReactNode } from "react";

import { Badge } from "@/lib/components";
import { cx } from "@/lib/utils";

const css = (value: CSSProperties): CSSProperties => value;

export const styles = {
  stack: css({ display: "flex", flexDirection: "column", gap: "var(--space-4)" }),
  stackTight: css({ display: "flex", flexDirection: "column", gap: "var(--space-2)" }),
  row: css({ display: "flex", alignItems: "center", gap: "var(--space-3)", flexWrap: "wrap" }),
  rowTight: css({ display: "flex", alignItems: "center", gap: "var(--space-2)", flexWrap: "wrap" }),
  rowBetween: css({
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: "var(--space-3)",
    flexWrap: "wrap",
  }),
  actions: css({
    display: "flex",
    alignItems: "center",
    justifyContent: "flex-end",
    gap: "var(--space-1)",
    flexWrap: "nowrap",
  }),
  toolbar: css({
    display: "flex",
    alignItems: "flex-end",
    gap: "var(--space-3)",
    flexWrap: "wrap",
    marginBottom: "var(--space-4)",
  }),
  toolbarSpacer: css({ flex: "1 1 auto" }),
  fieldGrid: css({
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(230px, 1fr))",
    gap: "var(--space-4)",
  }),
  fieldGridTwo: css({
    display: "grid",
    gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
    gap: "var(--space-4)",
  }),
  code: css({
    fontFamily: "var(--font-mono)",
    fontSize: "var(--fs-xs)",
    color: "var(--text-secondary)",
    overflowWrap: "anywhere",
  }),
  subtext: css({ color: "var(--text-muted)", fontSize: "var(--fs-xs)", lineHeight: 1.5 }),
  entity: css({ display: "flex", flexDirection: "column", gap: "2px", minWidth: 0 }),
  entityTitle: css({ fontWeight: 600, color: "var(--text)", lineHeight: 1.35 }),
  scrollBox: css({
    display: "flex",
    flexDirection: "column",
    gap: "var(--space-2)",
    maxHeight: "220px",
    overflowY: "auto",
    padding: "var(--space-3)",
    border: "1px solid var(--border)",
    borderRadius: "var(--radius-sm)",
    background: "var(--surface-sunken)",
  }),
  cardBody: css({ display: "flex", flexDirection: "column", gap: "var(--space-3)" }),
  footerNote: css({ margin: 0, color: "var(--text-subtle)", fontSize: "var(--fs-2xs)", lineHeight: 1.5 }),
  previewBox: css({
    display: "flex",
    flexDirection: "column",
    gap: "var(--space-3)",
    padding: "var(--space-3)",
    border: "1px solid var(--border)",
    borderRadius: "var(--radius-sm)",
    background: "var(--surface-sunken)",
  }),
  cardGrid: css({
    display: "grid",
    gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))",
    gap: "var(--space-4)",
  }),
  cardStack: css({ display: "flex", flexDirection: "column", gap: "var(--space-3)", minHeight: "100%" }),
  spacer: css({ flex: "1 1 auto" }),
  fullWidth: css({ width: "100%" }),
} as const;

/** Vertical stack of blocks, page-specific spacing. */
export function Stack({ children, gap = "var(--space-4)" }: { children: ReactNode; gap?: string }) {
  return <div style={{ display: "flex", flexDirection: "column", gap }}>{children}</div>;
}

/** Horizontal wrapping row. */
export function Row({ children, gap = "var(--space-3)" }: { children: ReactNode; gap?: string }) {
  return <div style={{ display: "flex", alignItems: "center", gap, flexWrap: "wrap" }}>{children}</div>;
}

/** `<code>` styled with the monospace token (ids, paths, packages). */
export function Mono({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <code style={styles.code} title={title}>
      {children}
    </code>
  );
}

/** Muted secondary text. */
export function SubText({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span style={styles.subtext} title={title}>
      {children}
    </span>
  );
}

/** Tag chips with an em-dash empty state. */
export function TagList({ tags, empty = "—" }: { tags?: string[] | null; empty?: ReactNode }) {
  const list = (tags ?? []).filter((tag) => Boolean(tag));
  if (list.length === 0) {
    return <span className="muted small">{empty}</span>;
  }
  return (
    <span style={styles.rowTight}>
      {list.map((tag) => (
        <Badge key={tag} tone="neutral">
          {tag}
        </Badge>
      ))}
    </span>
  );
}

/** Two-line identity cell for tables: primary label + muted reference. */
export function EntityCell({
  title,
  subtitle,
  subtitleTitle,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  subtitleTitle?: string;
}) {
  return (
    <div style={styles.entity}>
      <span style={styles.entityTitle}>{title}</span>
      {subtitle ? (
        <span style={styles.subtext} title={subtitleTitle}>
          {subtitle}
        </span>
      ) : null}
    </div>
  );
}

export interface DetailItem {
  label: ReactNode;
  value: ReactNode;
}

/** Definition list using the shared `.kv-list` styles. */
export function DetailList({ items, className }: { items: DetailItem[]; className?: string }) {
  return (
    <dl className={cx("kv-list", className)} style={{ margin: 0 }}>
      {items.map((item, index) => (
        <div className="kv-list__row" key={index}>
          <dt>{item.label}</dt>
          <dd>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** Small footnote-style hint. */
export function Hint({ children }: { children: ReactNode }) {
  return <p style={styles.footerNote}>{children}</p>;
}

/** Message for a failure caught in a page/dialog. */
export function errorMessage(error: unknown): string {
  if (error && typeof error === "object" && "message" in error) {
    const record = error as { message?: unknown; hint?: unknown };
    const message =
      typeof record.message === "string" && record.message.trim() ? record.message : "Request failed";
    const hint = typeof record.hint === "string" && record.hint.trim() ? ` ${record.hint.trim()}` : "";
    return `${message}${hint}`;
  }
  return String(error);
}

/** Compact form-section legend used by the large dialogs. */
export function FormSection({
  title,
  description,
  children,
}: {
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
}) {
  return (
    <fieldset
      style={{
        border: 0,
        margin: 0,
        padding: 0,
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-3)",
      }}
    >
      <legend
        style={{
          padding: 0,
          marginBottom: "var(--space-1)",
          fontFamily: "var(--font-mono)",
          fontSize: "var(--fs-2xs)",
          letterSpacing: "0.08em",
          textTransform: "uppercase",
          color: "var(--text-subtle)",
        }}
      >
        {title}
      </legend>
      {description ? <p style={styles.footerNote}>{description}</p> : null}
      {children}
    </fieldset>
  );
}
