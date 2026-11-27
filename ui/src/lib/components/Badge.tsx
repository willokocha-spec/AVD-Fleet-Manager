import type { ReactNode } from "react";

import { cx, humanize } from "../utils";
import { Icon, type IconName } from "./Icon";

export type BadgeTone = "neutral" | "info" | "success" | "warning" | "danger" | "accent";
export type BadgeSize = "sm" | "md";

export interface BadgeProps {
  tone?: BadgeTone;
  size?: BadgeSize;
  icon?: IconName;
  dot?: boolean;
  children: ReactNode;
  className?: string;
  title?: string;
}

export function Badge({ tone = "neutral", size = "sm", icon, dot = false, children, className, title }: BadgeProps) {
  return (
    <span
      className={cx("ds-badge", `ds-badge--${tone}`, `ds-badge--${size}`, className)}
      title={title}
    >
      {dot ? <span className="ds-badge__dot" aria-hidden="true" /> : null}
      {icon ? <Icon name={icon} size={13} aria-hidden="true" /> : null}
      <span className="ds-badge__label">{children}</span>
    </span>
  );
}

/**
 * Canonical status vocabulary. Keep this mapping in one place so every page
 * colours "running", "verified", "succeeded"… identically.
 */
const SUCCESS = new Set([
  "ok",
  "online",
  "running",
  "succeeded",
  "success",
  "verified",
  "ready",
  "active",
  "available",
  "connected",
  "healthy",
  "pass",
  "installed",
  "authenticated",
  "enabled",
  "complete",
  "completed",
]);
const WARNING = new Set([
  "warn",
  "warning",
  "booting",
  "starting",
  "queued",
  "pending",
  "degraded",
  "unverified",
  "reboot_required",
  "reconnecting",
  "connecting",
  "checking",
  "partial",
  "loading",
  "in_progress",
]);
const DANGER = new Set([
  "fail",
  "failed",
  "error",
  "offline",
  "timed_out",
  "timeout",
  "unsupported",
  "unreachable",
  "disconnected",
]);
const INFO = new Set(["info", "defined", "assigned"]);
const NEUTRAL = new Set([
  "skip",
  "stopped",
  "unknown",
  "disabled",
  "idle",
  "cancelled",
  "canceled",
  "none",
  "not_installed",
]);

export function statusTone(status: string | null | undefined): BadgeTone {
  if (!status) return "neutral";
  const key = status.trim().toLowerCase().replace(/[\s-]+/g, "_");
  if (SUCCESS.has(key)) return "success";
  if (WARNING.has(key)) return "warning";
  if (DANGER.has(key)) return "danger";
  if (INFO.has(key)) return "info";
  if (NEUTRAL.has(key)) return "neutral";
  return "neutral";
}

const TONE_ICON: Record<BadgeTone, IconName | null> = {
  success: "check-circle",
  warning: "alert-triangle",
  danger: "alert-circle",
  info: "info",
  accent: "zap",
  neutral: null,
};

export interface StatusPillProps {
  /** Raw state coming from the API, e.g. "running", "ok", "verified". */
  status: string | null | undefined;
  /** Friendly label; defaults to a humanised form of `status`. */
  label?: ReactNode;
  /** Force a tone instead of deriving it from `status`. */
  tone?: BadgeTone;
  /** Animate the dot for live/transitional states. */
  pulse?: boolean;
  icon?: IconName | false;
  size?: BadgeSize;
  className?: string;
  title?: string;
}

/** Dot + label pill used for daemon health, auth, instances, runs, OOBE… */
export function StatusPill({
  status,
  label,
  tone,
  pulse = false,
  icon,
  size = "sm",
  className,
  title,
}: StatusPillProps) {
  const resolvedTone = tone ?? statusTone(status);
  const glyph = icon === false ? null : (icon ?? TONE_ICON[resolvedTone]);
  return (
    <span
      className={cx(
        "ds-status",
        `ds-status--${resolvedTone}`,
        `ds-status--${size}`,
        pulse && "ds-status--pulse",
        className,
      )}
      data-status={status ?? "unknown"}
      title={title ?? (status ?? undefined)}
    >
      <span className="ds-status__dot" aria-hidden="true" />
      {glyph ? <Icon name={glyph} size={12} aria-hidden="true" className="ds-status__icon" /> : null}
      <span className="ds-status__label">{label ?? humanize(status) ?? "Unknown"}</span>
    </span>
  );
}
