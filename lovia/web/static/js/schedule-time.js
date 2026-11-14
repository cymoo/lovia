// schedule-time.js — a schedule's trigger in plain words, and the form's
// pickers ⇄ the API's (trigger_kind, trigger_expr). Pure: no DOM, so the
// conversions are unit-tested under node.
//
// The form offers four kinds of trigger, each one of the API's:
//   once     → at     (epoch seconds)
//   daily    → cron   `M H * * DOW` — weekdays and a time of day
//   interval → every  (seconds)
//   cron     → cron   (any expression; the fallback for the rest)
import { t } from './i18n.js';
import { formatTimeSmart, toDate } from './util.js';

/** Weekdays in the order the day chips show them (cron numbers, Sunday = 0). */
export const WEEK = [1, 2, 3, 4, 5, 6, 0];

const pad = (/** @type {number} */ n) => String(n).padStart(2, '0');
const isNum = (/** @type {string} */ s) => /^\d{1,2}$/.test(s);

/**
 * A cron day-of-week field as sorted day numbers (0–6), or null when it
 * isn't a plain list/range of days. `*` is every day.
 * @param {string} field
 * @returns {number[] | null}
 */
function parseDays(field) {
  if (field === '*') return [0, 1, 2, 3, 4, 5, 6];
  const days = new Set();
  for (const part of field.split(',')) {
    const range = part.match(/^([0-7])-([0-7])$/);
    if (range) {
      const [from, to] = [Number(range[1]), Number(range[2])];
      if (from > to) return null;
      for (let d = from; d <= to; d++) days.add(d % 7);
    } else if (/^[0-7]$/.test(part)) {
      days.add(Number(part) % 7);
    } else {
      return null;
    }
  }
  return [...days].sort((a, b) => a - b);
}

/**
 * A cron expression the "daily" picker can show — a fixed time on some
 * weekdays — or null when it needs the raw Cron tab.
 * @param {string} expr
 * @returns {{ days: number[], hour: number, minute: number } | null}
 */
export function parseDaily(expr) {
  const fields = expr.trim().split(/\s+/);
  if (fields.length !== 5) return null;
  const [min, hour, dom, month, dow] = fields;
  if (!isNum(min) || !isNum(hour) || dom !== '*' || month !== '*') return null;
  const [m, h] = [Number(min), Number(hour)];
  if (m > 59 || h > 23) return null;
  const days = parseDays(dow);
  return days && days.length ? { days, hour: h, minute: m } : null;
}

/**
 * The cron expression for the "daily" picker; runs of days read as ranges
 * (`1-5`), every day as `*`.
 * @param {{ days: number[], hour: number, minute: number }} daily
 */
export function dailyCron({ days, hour, minute }) {
  const sorted = [...new Set(days.map((d) => d % 7))].sort((a, b) => a - b);
  let dow = '*';
  if (sorted.length < 7) {
    /** @type {string[]} */
    const parts = [];
    for (let i = 0; i < sorted.length; ) {
      let j = i;
      while (j + 1 < sorted.length && sorted[j + 1] === sorted[j] + 1) j++;
      parts.push(j - i >= 2 ? `${sorted[i]}-${sorted[j]}` : sorted.slice(i, j + 1).join(','));
      i = j + 1;
    }
    dow = parts.join(',');
  }
  return `${minute} ${hour} * * ${dow}`;
}

/** @typedef {'second' | 'minute' | 'hour' | 'day'} IntervalUnit */
/** @type {[IntervalUnit, number][]} */
const UNITS = [
  ['day', 86400],
  ['hour', 3600],
  ['minute', 60],
];

/**
 * An interval in seconds as the largest whole unit; seconds when it is none.
 * @param {number} secs
 * @returns {{ n: number, unit: IntervalUnit }}
 */
export function splitInterval(secs) {
  for (const [unit, size] of UNITS) {
    if (secs >= size && secs % size === 0) return { n: secs / size, unit };
  }
  return { n: secs, unit: 'second' };
}

/** @param {IntervalUnit} unit */
export function unitSeconds(unit) {
  return UNITS.find(([u]) => u === unit)?.[1] ?? 1;
}

// ---- in plain words ---------------------------------------------------------

/** @param {number} d */
const dayName = (d) => t(`cron.day${d % 7}`);

/** @param {number[]} days sorted, 0–6 */
function daysLabel(days) {
  const key = days.join(',');
  if (days.length === 7) return t('cron.everyDay');
  if (key === '1,2,3,4,5') return t('cron.weekdays');
  if (key === '0,6') return t('cron.weekends');
  return WEEK.filter((d) => days.includes(d)).map(dayName).join(t('cron.daySep'));
}

/**
 * A cron expression in plain words, for the patterns people write; null for
 * anything fancier (the raw expression is shown instead).
 * @param {string} expr
 * @returns {string | null}
 */
export function humanizeCron(expr) {
  const daily = parseDaily(expr);
  if (daily) {
    return t('cron.daysAt', {
      days: daysLabel(daily.days),
      t: `${pad(daily.hour)}:${pad(daily.minute)}`,
    });
  }
  const fields = expr.trim().split(/\s+/);
  if (fields.length !== 5) return null;
  const [min, hour, dom, month, dow] = fields;
  if (month !== '*') return null;
  const step = (/** @type {string} */ s) => (/^\*\/\d+$/.test(s) ? s.slice(2) : null);
  const rest = dom === '*' && dow === '*';
  if (min === '*' && hour === '*' && rest) return t('cron.everyMinute');
  if (step(min) && hour === '*' && rest) return t('cron.everyNMin', { n: step(min) });
  if (isNum(min) && step(hour) && rest) {
    return t('cron.everyNHours', { n: step(hour), m: pad(Number(min)) });
  }
  if (isNum(min) && hour === '*' && rest) return t('cron.hourlyAt', { m: pad(Number(min)) });
  if (isNum(min) && isNum(hour) && isNum(dom) && dow === '*') {
    return t('cron.monthlyAt', { d: dom, t: `${pad(Number(hour))}:${pad(Number(min))}` });
  }
  return null;
}

/** @param {number} secs */
export function humanizeEvery(secs) {
  const { n, unit } = splitInterval(secs);
  return t(`sched.every.${unit}`, { n });
}

/**
 * A moment as near as it is worth saying: "today 09:00", "tomorrow 09:00",
 * "Wed 09:00" within the week, else the date.
 * @param {number} ts epoch seconds
 * @param {number} [now] milliseconds
 */
export function formatWhen(ts, now = Date.now()) {
  const d = toDate(ts);
  if (Number.isNaN(d.getTime())) return '';
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const today = new Date(now);
  today.setHours(0, 0, 0, 0);
  const day = new Date(d);
  day.setHours(0, 0, 0, 0);
  const diff = Math.round((day.getTime() - today.getTime()) / 86400000);
  if (diff === 0) return t('when.today', { t: hm });
  if (diff === 1) return t('when.tomorrow', { t: hm });
  if (diff === -1) return t('when.yesterday', { t: hm });
  if (diff > 1 && diff < 7) return t('when.weekday', { d: dayName(d.getDay()), t: hm });
  return formatTimeSmart(ts);
}

/**
 * @typedef {{ id: string, name?: string | null, input: string, active: boolean,
 *   trigger_kind: string, trigger_expr: string, next_fire: number,
 *   finished_reason?: string | null }} Schedule
 */

/** @param {Schedule} s */
export function describeTrigger(s) {
  if (s.trigger_kind === 'every') {
    const secs = Number(s.trigger_expr);
    return Number.isFinite(secs) ? humanizeEvery(secs) : `every ${s.trigger_expr}`;
  }
  if (s.trigger_kind === 'cron') return humanizeCron(s.trigger_expr) || `cron ${s.trigger_expr}`;
  if (s.trigger_kind === 'at') return t('sched.once', { time: formatWhen(Number(s.trigger_expr)) });
  return `${s.trigger_kind} ${s.trigger_expr}`;
}

/**
 * What the list calls a schedule: its name, else its instruction's first line.
 * @param {Schedule} s
 */
export function displayName(s) {
  if (s.name) return s.name;
  const line = s.input.split('\n').find((l) => l.trim()) ?? s.input;
  const text = line.trim();
  return text.length > 80 ? `${text.slice(0, 79)}…` : text;
}

/**
 * Done, not paused: the scheduler (or the agent) stopped it for a reason, or
 * it is a one-shot whose moment has passed. Resuming either only means
 * editing it first, so neither gets a switch.
 * @param {Schedule} s
 * @param {number} [now] milliseconds
 */
export function isDone(s, now = Date.now()) {
  if (s.active) return false;
  if (s.finished_reason) return true;
  return s.trigger_kind === 'at' && Number(s.trigger_expr) * 1000 <= now;
}

/**
 * @param {Schedule} s
 * @param {{ has(id: string): boolean }} firing ids with a live fired run
 * @param {number} [now] milliseconds
 * @returns {'running' | 'active' | 'paused' | 'done'}
 */
export function statusOf(s, firing, now = Date.now()) {
  if (firing.has(s.id)) return 'running';
  if (s.active) return 'active';
  return isDone(s, now) ? 'done' : 'paused';
}

/**
 * Why a schedule stopped, in the page's language where the scheduler wrote
 * it; an agent's own reason stays as written.
 * @param {string} reason
 */
export function finishedReason(reason) {
  if (reason === 'max fires reached') return t('sched.reasonMaxFires');
  if (reason === 'expired') return t('sched.reasonExpired');
  return reason;
}
