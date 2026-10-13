// The built-in `table` UI component: {title?, columns, rows}, drawn as a
// sortable, filterable, paged HTML table with copy / CSV / full screen. The
// `show_table` tool (lovia/web/tables.py) produces it; any tool may, via
// `ui_result("table", props)`. Registered through the public registry, so it
// can be replaced with `lovia.registerRenderer('table', ...)`.
//
// Props come from the model or a tool — untrusted — so every value is written
// with textContent. Keys the renderer doesn't know are ignored: a tool may
// carry model-facing extras (row counts, the SQL) in the same props.
//
// No DOM access at import time (the pure helpers are unit-tested under node).

import { registerRenderer } from './components.js';
import { csvBlob, figure, save, staticFigure } from './figure.js';
import { icon } from './icons.js';
import { toast } from './toast.js';
import { escapeHtml } from './util.js';

// About a screenful inside a chat card; no nested scroll area.
export const PAGE_SIZE = 20;
// Small tables read at a glance; the filter box is clutter below this.
export const FILTER_MIN_ROWS = 10;

/** @typedef {string | number | boolean | null} Cell */
/** @typedef {{ title: string, columns: string[], rows: unknown[][] }} Table */

/**
 * Check and square up the props: string headings, one cell per column (short
 * rows padded with null, long ones cut), or a thrown Error saying why not.
 * @param {unknown} props
 * @returns {Table}
 */
export function normalizeTable(props) {
  const p = /** @type {any} */ (props);
  if (!p || typeof p !== 'object' || !Array.isArray(p.columns) || !p.columns.length) {
    throw new Error('the table has no columns');
  }
  if (!Array.isArray(p.rows)) throw new Error('the table has no rows array');
  const columns = p.columns.map((/** @type {unknown} */ c) => String(c ?? ''));
  const rows = p.rows.map((/** @type {unknown} */ r) => {
    const cells = Array.isArray(r) ? r : [r];
    return columns.map((_, i) => cells[i] ?? null);
  });
  return { title: typeof p.title === 'string' ? p.title : '', columns, rows };
}

/**
 * A cell as displayed: values as given (no locale grouping — a year or an id
 * would read as "2,026"), null as empty, anything structured as JSON.
 * @param {unknown} v
 * @returns {string}
 */
export function cellText(v) {
  if (v == null) return '';
  return typeof v === 'object' ? JSON.stringify(v) : String(v);
}

/**
 * Per column: are all its non-null cells numbers? Those align right and sort
 * as numbers.
 * @param {unknown[][]} rows
 * @param {number} width
 * @returns {boolean[]}
 */
export function numericColumns(rows, width) {
  return Array.from({ length: width }, (_, i) => {
    let seen = false;
    for (const r of rows) {
      const v = r[i];
      if (v == null) continue;
      if (typeof v !== 'number') return false;
      seen = true;
    }
    return seen;
  });
}

/**
 * Rows ordered by column `col` (`dir` 1 ascending, -1 descending), stable;
 * nulls last either way. Numbers compare as numbers, everything else with the
 * collator — `numeric: true` also orders numeric strings and ISO dates.
 * @param {unknown[][]} rows
 * @param {number} col
 * @param {1 | -1} dir
 * @param {Intl.Collator} collator
 * @returns {unknown[][]}
 */
export function sortRows(rows, col, dir, collator) {
  return rows
    .map((r, i) => /** @type {[unknown[], number]} */ ([r, i]))
    .sort(([a, i], [b, j]) => {
      const x = a[col];
      const y = b[col];
      if (x == null || y == null) return x == null && y == null ? i - j : x == null ? 1 : -1;
      const c =
        typeof x === 'number' && typeof y === 'number'
          ? x - y
          : collator.compare(cellText(x), cellText(y));
      return c * dir || i - j;
    })
    .map(([r]) => r);
}

/**
 * Rows with a cell containing `query`, case-insensitively.
 * @param {unknown[][]} rows
 * @param {string} query
 * @returns {unknown[][]}
 */
export function filterRows(rows, query) {
  const q = query.trim().toLocaleLowerCase();
  if (!q) return rows;
  return rows.filter((r) => r.some((v) => cellText(v).toLocaleLowerCase().includes(q)));
}

/**
 * Tab-separated text — what spreadsheets take on paste. Tabs and newlines in
 * a cell become spaces: the format has no quoting that every app honors. As in
 * `toCsv`, a string a spreadsheet would run as a formula gets a leading `'`.
 * @param {unknown[][]} rows
 * @returns {string}
 */
export function toTsv(rows) {
  const cell = (/** @type {unknown} */ v) => {
    const s = cellText(v).replace(/[\t\r\n]+/g, ' ');
    return typeof v === 'string' && /^[=+\-@]/.test(s) ? `'${s}` : s;
  };
  return rows.map((r) => r.map(cell).join('\t')).join('\n');
}

const LABELS = {
  en: {
    copy: 'Copy (paste into a spreadsheet)',
    copied: 'Copied',
    csv: 'Download CSV',
    filter: 'Filter',
    count: (/** @type {number} */ n, /** @type {number} */ m) => (n === m ? `${m} rows` : `${n} of ${m} rows`),
    empty: 'No rows',
    noMatch: 'No matching rows',
    prev: 'Previous page',
    next: 'Next page',
    sort: (/** @type {string} */ c) => `Sort by ${c}`,
  },
  zh: {
    copy: '复制（可粘贴到表格软件）',
    copied: '已复制',
    csv: '下载 CSV',
    filter: '筛选',
    count: (/** @type {number} */ n, /** @type {number} */ m) => (n === m ? `${m} 行` : `${n} / ${m} 行`),
    empty: '没有数据',
    noMatch: '没有匹配的行',
    prev: '上一页',
    next: '下一页',
    sort: (/** @type {string} */ c) => `按 ${c} 排序`,
  },
};

/** @type {import('./components.js').Render} */
function renderTable(el, props, ctx) {
  const { title, columns, rows } = normalizeTable(props); // throws → the failure notice
  const L = LABELS[ctx.lang] || LABELS.en;
  const numeric = numericColumns(rows, columns.length);
  const collator = new Intl.Collator(ctx.lang, { numeric: true, sensitivity: 'base' });
  const { body, tool, filename } = figure(el, {
    kind: 'table',
    heading: title ? { text: title } : null,
    lang: ctx.lang,
  });

  let sortCol = -1;
  /** @type {1 | -1} */
  let sortDir = 1;
  let query = '';
  let page = 0;
  /** @type {unknown[][]} */
  let view = rows;

  const copy = tool(L.copy, '', async () => {
    // Loaded on use: ui.js reads `window` at import, which the node tests lack.
    const { copyToClipboard } = await import('./ui.js');
    await copyToClipboard(toTsv([columns, ...view]));
    toast(L.copied);
  });
  copy.innerHTML = icon('copy', { size: 12 });
  tool(L.csv, 'CSV', () => save(csvBlob([columns, ...view]), filename('csv')));

  /** @type {HTMLInputElement | null} */
  let input = null;
  /** @type {HTMLElement | null} */
  let count = null;
  if (rows.length > FILTER_MIN_ROWS) {
    const bar = document.createElement('div');
    bar.className = 'table-bar';
    input = document.createElement('input');
    input.type = 'search';
    input.placeholder = L.filter;
    input.setAttribute('aria-label', L.filter);
    count = document.createElement('span');
    count.className = 'table-count';
    bar.append(input, count);
    body.append(bar);
  }

  const scroll = document.createElement('div');
  scroll.className = 'table-scroll';
  const table = document.createElement('table');
  table.className = 'data-table';
  const headRow = table.createTHead().insertRow();
  const ths = columns.map((name, i) => {
    const th = document.createElement('th');
    th.scope = 'col';
    if (numeric[i]) th.className = 'num';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.title = L.sort(name);
    const label = document.createElement('span');
    label.textContent = name;
    const arrow = document.createElement('span');
    arrow.className = 'sort-arrow';
    arrow.setAttribute('aria-hidden', 'true');
    btn.append(label, arrow);
    btn.addEventListener('click', () => {
      // Ascending → descending → original order.
      if (sortCol !== i) [sortCol, sortDir] = [i, 1];
      else if (sortDir === 1) sortDir = -1;
      else sortCol = -1;
      page = 0;
      update();
    });
    th.append(btn);
    headRow.append(th);
    return th;
  });
  const tbody = table.createTBody();
  scroll.append(table);
  body.append(scroll);

  const pager = document.createElement('div');
  pager.className = 'table-pager';
  const prev = document.createElement('button');
  const next = document.createElement('button');
  const where = document.createElement('span');
  for (const [b, label, name, step] of /** @type {const} */ ([
    [prev, L.prev, 'chevron-left', -1],
    [next, L.next, 'chevron-right', 1],
  ])) {
    b.type = 'button';
    b.className = 'tool-action';
    b.title = label;
    b.setAttribute('aria-label', label);
    b.innerHTML = icon(name, { size: 12 });
    b.addEventListener('click', () => {
      page += step;
      update();
    });
  }
  pager.append(prev, where, next);
  body.append(pager);

  const fmt = new Intl.NumberFormat(ctx.lang);
  const update = () => {
    const filtered = filterRows(rows, query);
    view = sortCol < 0 ? filtered : sortRows(filtered, sortCol, sortDir, collator);
    const pages = Math.max(1, Math.ceil(view.length / PAGE_SIZE));
    page = Math.min(Math.max(page, 0), pages - 1);

    const start = page * PAGE_SIZE;
    const slice = view.slice(start, start + PAGE_SIZE);
    if (!slice.length) {
      const td = document.createElement('td');
      td.className = 'table-empty';
      td.colSpan = columns.length;
      td.textContent = rows.length ? L.noMatch : L.empty;
      const tr = document.createElement('tr');
      tr.append(td);
      tbody.replaceChildren(tr);
    } else {
      tbody.replaceChildren(
        ...slice.map((r) => {
          const tr = document.createElement('tr');
          r.forEach((v, i) => {
            const td = tr.insertCell();
            if (numeric[i]) td.className = 'num';
            td.textContent = cellText(v);
          });
          return tr;
        }),
      );
    }

    ths.forEach((th, i) => {
      const dir = i === sortCol ? sortDir : 0;
      // Only the sorted column declares it; the others omit the attribute.
      if (dir) th.setAttribute('aria-sort', dir === 1 ? 'ascending' : 'descending');
      else th.removeAttribute('aria-sort');
      /** @type {HTMLElement} */ (th.querySelector('.sort-arrow')).textContent =
        dir === 1 ? '↑' : dir === -1 ? '↓' : '';
    });
    if (count) count.textContent = L.count(view.length, rows.length);
    pager.hidden = view.length <= PAGE_SIZE;
    where.textContent = `${fmt.format(start + 1)}–${fmt.format(start + slice.length)} / ${fmt.format(view.length)}`;
    prev.disabled = page === 0;
    next.disabled = page >= pages - 1;
  };

  input?.addEventListener('input', () => {
    query = /** @type {HTMLInputElement} */ (input).value;
    page = 0;
    update();
  });
  update();
}

/**
 * Every row, as a plain table: an export is an archive, so nothing is paged
 * away.
 * @type {import('./components.js').Snapshot}
 */
export function tableSnapshot(props) {
  const { title, columns, rows } = normalizeTable(props);
  const numeric = numericColumns(rows, columns.length);
  const cell = (/** @type {string} */ tag, /** @type {string} */ text, /** @type {number} */ i) =>
    `<${tag}${numeric[i] ? ' class="num"' : ''}>${escapeHtml(text)}</${tag}>`;
  const head = columns.map((c, i) => cell('th', c, i)).join('');
  const body = rows.map((r) => `<tr>${r.map((v, i) => cell('td', cellText(v), i)).join('')}</tr>`);
  const table = `<table><thead><tr>${head}</tr></thead><tbody>${body.join('')}</tbody></table>`;
  return staticFigure('table', title ? { text: title } : null, table);
}

registerRenderer('table', { render: renderTable, snapshot: tableSnapshot });
