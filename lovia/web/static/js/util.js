// util.js — tiny dependency-free helpers shared across modules.

export function escapeHtml(s) {
  return String(s).replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );
}

// Timestamps arrive as epoch seconds (floats from the backend) or millis.
export function toDate(ts) {
  return new Date(ts > 1e12 ? ts : ts * 1000);
}

const pad = (n) => String(n).padStart(2, '0');

// Full form: "2026-07-05 14:32" (+":07" with seconds). Tooltips, schedules.
export function formatDateTime(ts, { seconds = false } = {}) {
  if (ts == null) return '';
  const d = toDate(ts);
  if (Number.isNaN(d.getTime())) return String(ts);
  const base =
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return seconds ? `${base}:${pad(d.getSeconds())}` : base;
}

// Compact form for timelines: "14:32" today, "07-01 09:15" this year,
// "2025-12-31 23:59" otherwise. Pair with formatDateTime in a tooltip.
export function formatTimeSmart(ts) {
  if (ts == null) return '';
  const d = toDate(ts);
  if (Number.isNaN(d.getTime())) return String(ts);
  const now = new Date();
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (d.toDateString() === now.toDateString()) return hm;
  const md = `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  if (d.getFullYear() === now.getFullYear()) return `${md} ${hm}`;
  return `${d.getFullYear()}-${md} ${hm}`;
}
