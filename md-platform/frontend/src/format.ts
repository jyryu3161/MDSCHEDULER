// Small presentation helpers shared across pages. Pure functions only.

// The backend serializes naive UTC timestamps (e.g. "2026-06-17T05:14:03", no tz suffix).
// `new Date()` parses a tz-less string as LOCAL time, shifting every timestamp by the viewer's
// UTC offset (e.g. KST +9 -> "9h ago" for something just created). Treat a tz-less string as UTC.
function parseServerDate(iso: string): Date {
  const hasTz = /([zZ])$|[+-]\d{2}:?\d{2}$/.test(iso);
  return new Date(hasTz ? iso : `${iso}Z`);
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = parseServerDate(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

const SKEW_TOLERANCE_MS = 5000;

function humanizeSpan(ms: number): string {
  const sec = Math.round(ms / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h`;
  const day = Math.round(hr / 24);
  return `${day}d`;
}

export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = parseServerDate(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const diff = Date.now() - d.getTime();
  // Small negative diffs are clock skew between server and client: show "just
  // now". Genuinely future timestamps render as "in X"; past timestamps as
  // "X ago".
  if (diff < 0) {
    if (diff > -SKEW_TOLERANCE_MS) return "just now";
    return `in ${humanizeSpan(-diff)}`;
  }
  if (diff < 1000) return "just now";
  return `${humanizeSpan(diff)} ago`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "—";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return rem ? `${m}m ${rem}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const mRem = m % 60;
  if (h < 24) return mRem ? `${h}h ${mRem}m` : `${h}h`;
  const d = Math.floor(h / 24);
  const hRem = h % 24;
  return hRem ? `${d}d ${hRem}h` : `${d}d`;
}

export function formatGb(gb: number | null | undefined, digits = 1): string {
  if (gb == null || !Number.isFinite(gb)) return "—";
  return `${gb.toFixed(digits)} GB`;
}

export function formatNumber(
  value: number | null | undefined,
  digits = 2,
): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toFixed(digits);
}

export function formatScore(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toFixed(1);
}

export function titleCase(s: string): string {
  return s
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}
