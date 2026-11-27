/** Small, dependency-free helpers shared by the GUI. */

export type ClassValue = string | false | null | undefined | 0 | ClassValue[];

/** Join truthy class names. `cx("a", cond && "b", ["c"])` → `"a b c"`. */
export function cx(...values: ClassValue[]): string {
  const out: string[] = [];
  const visit = (value: ClassValue): void => {
    if (!value) return;
    if (Array.isArray(value)) {
      value.forEach(visit);
    } else {
      out.push(value);
    }
  };
  values.forEach(visit);
  return out.join(" ");
}

/** "google_apis_playstore" → "Google APIs Playstore". */
export function humanize(value: string | null | undefined): string {
  if (!value) return "";
  return value
    .replace(/[._-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, { timeStyle: "medium" }).format(date);
}

export function formatRelativeTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const deltaSeconds = Math.round((date.getTime() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  const units: Array<[Intl.RelativeTimeFormatUnit, number]> = [
    ["second", 60],
    ["minute", 60],
    ["hour", 24],
    ["day", 7],
    ["week", 4.348],
    ["month", 12],
    ["year", Number.POSITIVE_INFINITY],
  ];
  let valueLeft = deltaSeconds;
  for (const [unit, span] of units) {
    if (Math.abs(valueLeft) < span || unit === "year") {
      return formatter.format(Math.round(valueLeft), unit);
    }
    valueLeft = valueLeft / span;
  }
  return formatter.format(Math.round(deltaSeconds), "second");
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "—";
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  if (minutes < 60) return `${minutes}m ${rest.toString().padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${(minutes % 60).toString().padStart(2, "0")}m`;
}

export function formatBytes(bytes: number | null | undefined, fractionDigits = 1): string {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return "—";
  if (bytes === 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  const exponent = Math.min(Math.floor(Math.log(Math.abs(bytes)) / Math.log(1024)), units.length - 1);
  const value = bytes / 1024 ** exponent;
  return `${value.toFixed(exponent === 0 ? 0 : fractionDigits)} ${units[exponent]}`;
}

export function formatPercent(value: number, fractionDigits = 0): string {
  if (!Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(fractionDigits)}%`;
}

export function pluralize(count: number, singular: string, plural = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : plural}`;
}

export function truncate(value: string, maxLength = 80): string {
  if (value.length <= maxLength) return value;
  return `${value.slice(0, Math.max(0, maxLength - 1))}…`;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Copy text to the clipboard, returning false when the browser denies it. */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to the legacy path */
  }
  try {
    if (typeof document === "undefined") return false;
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "true");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}

/** Initials for avatars/chips: "avd-pixel7-01" → "AP". */
export function initials(value: string): string {
  const parts = value.split(/[^a-zA-Z0-9]+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0]!.slice(0, 2).toUpperCase();
  return (parts[0]![0]! + parts[1]![0]!).toUpperCase();
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Strip the password (if any) from a proxy URL for display. The API never
 * returns passwords; this protects against pasted/imported values in the UI.
 */
export function maskProxyUrl(url: string): string {
  return url.replace(/\/\/[^/@\s]+@/, "//***:***@");
}
