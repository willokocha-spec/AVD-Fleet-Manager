import type { ReactNode } from "react";

import { cx } from "../utils";
import { Icon, type IconName } from "./Icon";

/* -------------------------------------------------------------------------- */
/* Spinner                                                                     */
/* -------------------------------------------------------------------------- */

export interface SpinnerProps {
  size?: "sm" | "md" | "lg";
  /** Screen-reader label; visually hidden. */
  label?: string;
  className?: string;
}

export function Spinner({ size = "md", label = "Loading", className }: SpinnerProps) {
  return (
    <span className={cx("ds-spinner", `ds-spinner--${size}`, className)} role="status">
      <span className="visually-hidden">{label}</span>
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* Skeleton                                                                    */
/* -------------------------------------------------------------------------- */

export interface SkeletonProps {
  variant?: "text" | "rect" | "circle";
  /** CSS width. */
  width?: number | string;
  /** CSS height. */
  height?: number | string;
  /** Render this many text lines, the last one shorter. */
  lines?: number;
  className?: string;
}

export function Skeleton({ variant = "text", width, height, lines = 1, className }: SkeletonProps) {
  if (variant === "text" && lines > 1) {
    return (
      <span className={cx("ds-skeleton-stack", className)} aria-hidden="true">
        {Array.from({ length: lines }, (_, index) => (
          <span
            key={index}
            className="ds-skeleton ds-skeleton--text"
            style={{ width: index === lines - 1 ? "62%" : "100%" }}
          />
        ))}
      </span>
    );
  }
  return (
    <span
      aria-hidden="true"
      className={cx("ds-skeleton", `ds-skeleton--${variant}`, className)}
      style={{ width, height }}
    />
  );
}

/* -------------------------------------------------------------------------- */
/* EmptyState                                                                  */
/* -------------------------------------------------------------------------- */

export interface EmptyStateProps {
  title: string;
  description?: ReactNode;
  icon?: IconName;
  action?: ReactNode;
  /** Tight layout used inside cards/tables. */
  compact?: boolean;
  className?: string;
}

export function EmptyState({
  title,
  description,
  icon = "info",
  action,
  compact = false,
  className,
}: EmptyStateProps) {
  return (
    <div className={cx("ds-empty", compact && "ds-empty--compact", className)}>
      <span className="ds-empty__icon" aria-hidden="true">
        <Icon name={icon} size={compact ? 20 : 26} />
      </span>
      <h3 className="ds-empty__title">{title}</h3>
      {description ? <p className="ds-empty__description">{description}</p> : null}
      {action ? <div className="ds-empty__actions">{action}</div> : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* ProgressBar                                                                 */
/* -------------------------------------------------------------------------- */

export interface ProgressBarProps {
  /** 0..max; omit (or `indeterminate`) for an animated bar. */
  value?: number;
  max?: number;
  label?: string;
  /** Show the percentage on the right when determinate. */
  showValue?: boolean;
  tone?: "accent" | "success" | "warning" | "danger";
  indeterminate?: boolean;
  className?: string;
}

export function ProgressBar({
  value,
  max = 100,
  label,
  showValue = false,
  tone = "accent",
  indeterminate = false,
  className,
}: ProgressBarProps) {
  const isIndeterminate = indeterminate || value === undefined || !Number.isFinite(value);
  const safeMax = max > 0 ? max : 100;
  const clamped = isIndeterminate ? 0 : Math.min(Math.max(value ?? 0, 0), safeMax);
  const percent = isIndeterminate ? 0 : Math.round((clamped / safeMax) * 100);
  return (
    <div className={cx("ds-progress", className)}>
      {label || showValue ? (
        <div className="ds-progress__header">
          {label ? <span className="ds-progress__label">{label}</span> : <span />}
          {showValue && !isIndeterminate ? (
            <span className="ds-progress__value">{percent}%</span>
          ) : null}
        </div>
      ) : null}
      <div
        className={cx("ds-progress__track", `ds-progress--${tone}`)}
        role="progressbar"
        aria-label={label ?? "Progress"}
        aria-valuemin={0}
        aria-valuemax={safeMax}
        aria-valuenow={isIndeterminate ? undefined : clamped}
        aria-valuetext={isIndeterminate ? "In progress" : `${percent}%`}
      >
        <div
          className={cx("ds-progress__fill", isIndeterminate && "ds-progress__fill--indeterminate")}
          style={isIndeterminate ? undefined : { width: `${percent}%` }}
        />
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* InlineNotice                                                                */
/* -------------------------------------------------------------------------- */

export type NoticeTone = "info" | "success" | "warning" | "danger";

export interface InlineNoticeProps {
  tone?: NoticeTone;
  title?: string;
  children: ReactNode;
  action?: ReactNode;
  className?: string;
}

const NOTICE_ICONS: Record<NoticeTone, IconName> = {
  info: "info",
  success: "check-circle",
  warning: "alert-triangle",
  danger: "alert-circle",
};

export function InlineNotice({ tone = "info", title, children, action, className }: InlineNoticeProps) {
  return (
    <div className={cx("ds-notice", `ds-notice--${tone}`, className)} role={tone === "danger" ? "alert" : "note"}>
      <span className="ds-notice__icon" aria-hidden="true">
        <Icon name={NOTICE_ICONS[tone]} size={18} />
      </span>
      <div className="ds-notice__body">
        {title ? <p className="ds-notice__title">{title}</p> : null}
        <div className="ds-notice__content">{children}</div>
      </div>
      {action ? <div className="ds-notice__action">{action}</div> : null}
    </div>
  );
}
