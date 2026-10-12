// A sortable, filterable table component for the lovia chat UI, with an "Ask"
// button per row.
//
// Loaded by examples/31_web_components.py via ChatUI(scripts=[...]). Renders
// any tool output made with
//
//   ui_result("table", {"title": ..., "columns": [...], "rows": [[...], ...]})
//
// `table` is a built-in kind; registering it here replaces the built-in
// renderer, which shows how to swap one out.
//
// Props come from the model / tools — untrusted — so every value is written
// with textContent, never as HTML.

const STYLE = `
.x-table { font-size: 13px; }
.x-table-bar { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
.x-table-bar strong { flex: 1; }
.x-table-bar input { padding: 4px 8px; border: 1px solid var(--border); border-radius: 6px;
  background: var(--surface); color: var(--ink); font: inherit; }
.x-table table { width: 100%; border-collapse: collapse; }
.x-table th, .x-table td { padding: 6px 8px; border-bottom: 1px solid var(--border); text-align: left; }
.x-table th { cursor: pointer; user-select: none; color: var(--muted); font-weight: 600; white-space: nowrap; }
.x-table td.num { text-align: right; font-variant-numeric: tabular-nums; }
.x-table button { padding: 2px 8px; border: 1px solid var(--border); border-radius: 6px;
  background: var(--surface); color: var(--ink); cursor: pointer; font: inherit; }
.x-table-foot { margin-top: 6px; color: var(--muted); font-size: 12px; }
`;

function ensureStyle() {
  if (document.getElementById('x-table-style')) return;
  const style = document.createElement('style');
  style.id = 'x-table-style';
  style.textContent = STYLE;
  document.head.append(style);
}

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}

function renderTable(root, props, ctx) {
  const columns = Array.isArray(props?.columns) ? props.columns.map(String) : [];
  const rows = Array.isArray(props?.rows) ? props.rows.filter(Array.isArray) : [];
  ensureStyle();

  let sortCol = -1;
  let sortDir = 1;
  let filter = '';

  const wrap = el('div', undefined, 'x-table');
  const bar = el('div', undefined, 'x-table-bar');
  bar.append(el('strong', props?.title ?? ''));
  const search = el('input');
  search.placeholder = ctx.lang === 'zh' ? '筛选…' : 'Filter…';
  bar.append(search);
  const table = el('table');
  const foot = el('div', undefined, 'x-table-foot');
  wrap.append(bar, table, foot);

  function draw() {
    const needle = filter.toLowerCase();
    let shown = rows.filter((r) => !needle || r.some((v) => String(v).toLowerCase().includes(needle)));
    if (sortCol >= 0) {
      shown = [...shown].sort((a, b) => {
        const x = a[sortCol];
        const y = b[sortCol];
        const cmp = typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y));
        return cmp * sortDir;
      });
    }
    const head = el('tr');
    columns.forEach((name, i) => {
      const th = el('th', name + (i === sortCol ? (sortDir > 0 ? ' ▲' : ' ▼') : ''));
      th.addEventListener('click', () => {
        sortDir = i === sortCol ? -sortDir : 1;
        sortCol = i;
        draw();
      });
      head.append(th);
    });
    head.append(el('th'));
    const body = shown.map((r) => {
      const tr = el('tr');
      r.forEach((v) => tr.append(el('td', v, typeof v === 'number' ? 'num' : undefined)));
      const ask = el('button', ctx.lang === 'zh' ? '追问' : 'Ask');
      // A real click is a user gesture, so ctx.send is allowed to post.
      ask.addEventListener('click', () => {
        ctx.send(`Tell me more about ${columns[0] ?? 'row'} ${r[0]}.`);
      });
      const cell = el('td');
      cell.append(ask);
      tr.append(cell);
      return tr;
    });
    table.replaceChildren(head, ...body);
    foot.textContent = `${shown.length} / ${rows.length}`;
  }

  search.addEventListener('input', () => {
    filter = search.value;
    draw();
  });
  draw();
  root.append(wrap);
  // Nothing global to release: the card's DOM goes with it. A component that
  // holds observers, timers, or chart instances returns a cleanup function.
}

window.lovia.registerRenderer('table', renderTable);
