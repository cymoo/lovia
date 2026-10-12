// The built-in `chart` UI component: an Apache ECharts option, drawn by the
// vendored ECharts (loaded on the first chart — sessions without charts never
// fetch it). The `show_chart` tool (lovia/web/charts.py) produces it; any tool
// may, via `ui_result("chart", option)`. Registered through the same public
// registry users get, so it can be replaced wholesale with
// `lovia.registerRenderer('chart', ...)`.
//
// The option comes from the model or a tool — untrusted — so it is sanitized
// here, the trust boundary: replays of old data and non-show_chart tools never
// passed the server-side checks.
//
// The card around the chart (figure.js) is HTML: a heading (the option's
// title, lifted out — see liftTitle) and a toolbar to download the chart
// (PNG / SVG), its data (CSV), or view it full screen.
//
// No DOM access at import time (the pure helpers are unit-tested under node).

import { registerRenderer } from './components.js';
import { csvBlob, figure, save } from './figure.js';

// Same as SERIES_TYPES in lovia/web/charts.py (a test keeps them equal).
export const SERIES_TYPES = new Set([
  'line', 'bar', 'pie', 'scatter', 'effectScatter', 'radar', 'tree', 'treemap',
  'sunburst', 'boxplot', 'candlestick', 'heatmap', 'parallel', 'funnel',
  'gauge', 'sankey', 'graph', 'themeRiver',
]);
// Top-level components that draw data. Left out on purpose: `graphic` (images,
// arbitrary shapes), `toolbox`, `timeline`, `geo` (needs registered maps).
const COMPONENTS = new Set([
  'title', 'legend', 'grid', 'xAxis', 'yAxis', 'polar', 'radiusAxis',
  'angleAxis', 'radar', 'dataZoom', 'visualMap', 'tooltip', 'axisPointer',
  'dataset', 'series', 'color', 'textStyle', 'aria', 'calendar', 'parallel',
  'parallelAxis', 'singleAxis',
]);
// `link`/`sublink` reach window.open (a javascript: URL would run); `image`
// (pattern fills, backgrounds) fetches remote URLs — a tracking pixel.
const DROP_KEYS = new Set(['link', 'sublink', 'image']);
// Strings that make the renderer fetch: `image://` symbols, and CSS/SVG paint
// URLs — a `url(...)` colour lands in an SVG `fill` attribute as is.
const FETCHING = /^\s*(?:image:\/\/|url\s*\()/i;
const MAX_DEPTH = 32;

const ECHARTS_FILE = 'vendor/echarts.min.js';
// Pinned like the template's <script> tags (tests/web/test_vendor_integrity.py).
const ECHARTS_SRI = 'sha384-C2iskrW/uPW46KzOjrvJIQo4YkV8lkD+QS0CrDN18IIPIpT/g2USu8bTP3nvmIAD';

/**
 * @param {unknown} value
 * @param {number} depth
 * @returns {unknown} `undefined` drops the value.
 */
function clean(value, depth) {
  if (depth > MAX_DEPTH) return undefined;
  if (typeof value === 'string') return FETCHING.test(value) ? undefined : value;
  if (Array.isArray(value)) {
    // null, not removal: a dropped cell must not shift a dataset row.
    return value.map((v) => clean(v, depth + 1) ?? null);
  }
  if (value && typeof value === 'object') {
    /** @type {Record<string, unknown>} */
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      if (DROP_KEYS.has(k)) continue;
      const c = clean(v, depth + 1);
      if (c !== undefined) out[k] = c;
    }
    return out;
  }
  return value;
}

/**
 * A copy of `option` safe to hand to ECharts, or a thrown Error saying why
 * it can't be drawn.
 * @param {unknown} option
 * @returns {Record<string, any>}
 */
export function sanitizeOption(option) {
  if (!option || typeof option !== 'object' || Array.isArray(option)) {
    throw new Error('the chart option must be an object');
  }
  /** @type {Record<string, any>} */
  const out = {};
  for (const [k, v] of Object.entries(option)) {
    if (COMPONENTS.has(k)) out[k] = clean(v, 1);
  }
  const series = Array.isArray(out.series) ? out.series : out.series ? [out.series] : [];
  if (!series.length) throw new Error('the chart option has no series');
  for (const s of series) {
    if (!SERIES_TYPES.has(s?.type)) throw new Error(`unsupported series type: ${s?.type}`);
  }
  out.series = series;
  // An HTML-mode tooltip renders formatter strings as markup; rich text
  // doesn't. `confine` keeps it inside the card. A rich-text tooltip is part
  // of the drawing: no `hideDelay`, so an image export can hide it at once.
  if (out.tooltip) {
    const rich = (/** @type {any} */ t) => ({ ...t, renderMode: 'richText', confine: true, hideDelay: 0 });
    out.tooltip = Array.isArray(out.tooltip) ? out.tooltip.map(rich) : rich(out.tooltip);
  }
  out.aria = { enabled: true, ...out.aria };
  return out;
}

// ---- Heading ------------------------------------------------------------------

// A `top` this close to the edge is padding, not a placement in the plot.
const TOP_BAND = { px: 60, '%': 10 };

/** @param {unknown} v */
function atTop(v) {
  if (v == null || v === 'top') return true;
  const m = /^(\d+(?:\.\d+)?)(px|%)?$/.exec(String(v).trim());
  return !!m && Number(m[1]) <= TOP_BAND[/** @type {'px' | '%'} */ (m[2] || 'px')];
}

/**
 * Lift a top-anchored title out of `option`, to show as the card's HTML
 * heading. ECharts has no title/legend collision avoidance, and models
 * hard-code `legend.top` offsets that assume a font size — they overlap. A
 * title placed elsewhere (a donut's centre label, one per pie) stays put.
 * @param {Record<string, any>} option A sanitized option; not mutated.
 * @returns {{ heading: { text: string, subtext: string } | null, option: Record<string, any> }}
 */
export function liftTitle(option) {
  const all = option.title;
  const t = Array.isArray(all) ? (all.length === 1 ? all[0] : null) : all;
  if (
    !t || typeof t !== 'object' || typeof t.text !== 'string' || !t.text.trim() ||
    t.show === false || t.bottom != null || !atTop(t.top)
  ) {
    return { heading: null, option };
  }
  const { title: _, ...rest } = option;
  const subtext = typeof t.subtext === 'string' ? t.subtext : '';
  return { heading: { text: t.text, subtext }, option: rest };
}

// ---- Data export ----------------------------------------------------------------

// Hierarchies and networks have no table shape; their data downloads as JSON.
const NON_TABULAR = new Set(['tree', 'treemap', 'sunburst', 'sankey', 'graph']);

/** @param {unknown} v */
const first = (v) => (Array.isArray(v) ? v[0] : v);
/** @param {unknown} v */
const isScalar = (v) => v === null || ['string', 'number', 'boolean'].includes(typeof v);
/** A data item's value: `{value, name}` items unwrap; bare values pass. */
const valueOf = (/** @type {any} */ d) =>
  d && typeof d === 'object' && !Array.isArray(d) ? d.value : d;

/** @param {Record<string, any>} ds @returns {unknown[][] | null} */
function datasetRows(ds) {
  const src = ds.source;
  const dims = Array.isArray(ds.dimensions)
    ? ds.dimensions.map((/** @type {any} */ d) => (d && typeof d === 'object' ? d.name : d))
    : null;
  if (Array.isArray(src)) {
    if (src.every(Array.isArray)) {
      // Dimensions name the columns unless the first row already does —
      // `sourceHeader` says so, or, like ECharts, guess: no numbers in it.
      const headed =
        ds.sourceHeader != null
          ? !!ds.sourceHeader
          : !src[0]?.some((/** @type {unknown} */ v) => typeof v === 'number');
      return dims && !headed ? [dims, ...src] : src;
    }
    const keys = dims ?? [...new Set(src.flatMap((r) => (r && typeof r === 'object' ? Object.keys(r) : [])))];
    return [keys, ...src.map((r) => keys.map((/** @type {string} */ k) => r?.[k] ?? null))];
  }
  if (src && typeof src === 'object') {
    // Column-major: {"month": [...], "sales": [...]}.
    const keys = Object.keys(src);
    const cols = keys.map((k) => (Array.isArray(src[k]) ? src[k] : []));
    const n = Math.max(0, ...cols.map((c) => c.length));
    return [keys, ...Array.from({ length: n }, (_, i) => cols.map((c) => c[i] ?? null))];
  }
  return null;
}

/**
 * The chart's data as a table — header row first — or null when it has no
 * table shape. Read from `dataset.source` when present; otherwise from the
 * series: wide (one column per series) over a category axis, else long (one
 * row per data item: series, name, value…).
 * @param {Record<string, any>} option A sanitized option.
 * @returns {unknown[][] | null}
 */
export function optionRows(option) {
  const ds = first(option.dataset);
  if (ds?.source) return datasetRows(ds);
  /** @type {any[]} */
  const series = [option.series].flat().filter(Boolean);
  if (!series.length || series.some((s) => NON_TABULAR.has(s.type) || !Array.isArray(s.data))) {
    return null;
  }
  const names = series.map((s, i) => s.name ?? (series.length > 1 ? `series ${i + 1}` : 'value'));
  const axis = [first(option.xAxis), first(option.yAxis)].find((a) => Array.isArray(a?.data));
  const cats = axis ? axis.data.map(valueOf) : null;
  const values = series.map((s) => s.data.map(valueOf));
  if (cats && values.every((vs) => vs.every(isScalar))) {
    return [
      [axis.name ?? '', ...names],
      ...cats.map((/** @type {unknown} */ c, /** @type {number} */ i) => [
        c,
        ...values.map((vs) => vs[i] ?? null),
      ]),
    ];
  }
  /** @type {unknown[][]} */
  const rows = [];
  let width = 1;
  for (const [k, s] of series.entries()) {
    for (const [i, d] of s.data.entries()) {
      const v = valueOf(d);
      const vals = Array.isArray(v) ? v : [v];
      if (!vals.every(isScalar)) return null;
      width = Math.max(width, vals.length);
      rows.push([names[k], d?.name ?? cats?.[i] ?? null, ...vals]);
    }
  }
  // Drop the series column for one series, the name column when unnamed.
  const keep = [series.length > 1, rows.some((r) => r[1] != null && r[1] !== '')];
  const pick = (/** @type {unknown[]} */ r) => r.filter((_, j) => j > 1 || keep[j]);
  const valueHead = width === 1 ? ['value'] : Array.from({ length: width }, (_, i) => `value${i + 1}`);
  return [pick(['series', 'name', ...valueHead]), ...rows.map(pick)];
}

// ---- Loading ----------------------------------------------------------------

/** @type {Promise<any> | null} */
let _loading = null;

function staticBase() {
  try {
    const cfg = JSON.parse(document.getElementById('app-config')?.textContent || '{}');
    if (typeof cfg.static_base === 'string') return cfg.static_base;
  } catch { /* fall through */ }
  return '/static/';
}

/** @returns {Promise<any>} The ECharts global, injected on first use. */
function loadEcharts() {
  if (typeof echarts !== 'undefined') return Promise.resolve(echarts);
  _loading ??= new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = staticBase() + ECHARTS_FILE;
    s.integrity = ECHARTS_SRI;
    s.onload = () =>
      typeof echarts !== 'undefined' ? resolve(echarts) : reject(new Error('ECharts did not load'));
    s.onerror = () => {
      _loading = null; // a later chart may retry (e.g. after a network blip)
      reject(new Error('could not load ECharts'));
    };
    document.head.append(s);
  });
  return _loading;
}

// ---- Theme --------------------------------------------------------------------

// The palette and inks are CSS variables (styles.css `--chart-*`), so a
// deployer retheme is a few lines in the `head` template block.
function themeFromCss() {
  const cs = getComputedStyle(document.documentElement);
  const v = (/** @type {string} */ name) => cs.getPropertyValue(name).trim();
  const color = [1, 2, 3, 4, 5, 6, 7, 8].map((i) => v(`--chart-${i}`)).filter(Boolean);
  const text = v('--chart-text');
  const strong = v('--chart-title');
  const axis = v('--chart-axis');
  const grid = v('--chart-grid');
  // Adjacent fills (slices, tiles) part with a hairline of the card surface.
  const gap = { itemStyle: { borderColor: v('--surface'), borderWidth: 2 } };
  const axisTheme = {
    axisLine: { lineStyle: { color: axis } },
    axisTick: { lineStyle: { color: axis } },
    axisLabel: { color: text },
    splitLine: { lineStyle: { color: grid } },
    nameTextStyle: { color: text },
  };
  return {
    ...(color.length ? { color } : {}),
    backgroundColor: 'transparent',
    // Single quotes: ECharts writes the family into a double-quoted SVG
    // `style` attribute unescaped, which breaks the exported SVG's XML.
    textStyle: { color: text, fontFamily: getComputedStyle(document.body).fontFamily.replaceAll('"', "'") },
    title: { textStyle: { color: strong }, subtextStyle: { color: text } },
    legend: { textStyle: { color: text } },
    categoryAxis: axisTheme,
    valueAxis: axisTheme,
    logAxis: axisTheme,
    timeAxis: axisTheme,
    line: { lineStyle: { width: 2 }, symbolSize: 6 },
    // Outside labels wear the text ink; ECharts' auto contrast outline would
    // otherwise halo them on the dark surface.
    pie: { ...gap, label: { color: text, textBorderWidth: 0 } },
    funnel: gap,
    sunburst: gap,
    treemap: gap,
  };
}

// ---- Renderer -----------------------------------------------------------------

const LABELS = {
  en: {
    failed: "Couldn't draw this chart",
    png: 'Download PNG',
    svg: 'Download SVG',
    csv: 'Download data (CSV)',
    json: 'Download data (JSON)',
  },
  zh: {
    failed: '无法绘制该图表',
    png: '下载 PNG 图片',
    svg: '下载 SVG 图片',
    csv: '下载数据（CSV）',
    json: '下载数据（JSON）',
  },
};

const XML_ESC = /** @type {Record<string, string>} */ ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
});
const xml = (/** @type {string} */ s) => s.replace(/[&<>"]/g, (c) => XML_ESC[c]);

/**
 * The drawn chart as a standalone SVG on the card surface, with the heading
 * (lifted out of the option) drawn back in above it.
 * @param {any} chart
 * @param {{ text: string, subtext: string } | null} heading
 */
function chartSvg(chart, heading) {
  const cs = getComputedStyle(document.documentElement);
  const v = (/** @type {string} */ name) => xml(cs.getPropertyValue(name).trim());
  const font = xml(getComputedStyle(document.body).fontFamily);
  const style = (/** @type {string} */ text, size = 12, weight = 400, fill = v('--chart-text')) =>
    text.split('\n').filter(Boolean).map((s) => ({ s, size, weight, fill }));
  const lines = [
    ...style(heading?.text ?? '', 14, 600, v('--chart-title')),
    ...style(heading?.subtext ?? ''),
  ];
  let y = 0;
  const text = lines
    .map(({ s, size, weight, fill }) => {
      y += Math.round(size * 1.5);
      return `<text x="16" y="${y}" font-family="${font}" font-size="${size}" font-weight="${weight}" fill="${fill}">${xml(s)}</text>`;
    })
    .join('');
  const top = lines.length ? y + 12 : 0;
  chart.dispatchAction({ type: 'hideTip' }); // an open tooltip is part of the SVG
  const width = chart.getWidth();
  const height = chart.getHeight() + top;
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">` +
    `<rect width="100%" height="100%" fill="${v('--surface')}"/>${text}` +
    chart.renderToSVGString().replace(/<svg\b/, `<svg y="${top}"`) +
    '</svg>';
  return { svg, width, height };
}

/**
 * Rasterize an SVG at 2× for a crisp PNG.
 * @param {{ svg: string, width: number, height: number }} img
 * @returns {Promise<Blob>}
 */
async function svgToPng({ svg, width, height }) {
  const image = new Image();
  image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  await image.decode();
  const canvas = document.createElement('canvas');
  canvas.width = width * 2;
  canvas.height = height * 2;
  canvas.getContext('2d')?.drawImage(image, 0, 0, canvas.width, canvas.height);
  return new Promise((resolve, reject) =>
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('PNG encoding failed'))), 'image/png'),
  );
}

/** @type {import('./components.js').Renderer} */
function renderChart(el, props, ctx) {
  const clean = sanitizeOption(props); // throws → the registry's failure notice
  const { heading, option } = liftTitle(clean);
  const L = LABELS[ctx.lang] || LABELS.en;
  const { fig, body: box, tool, filename } = figure(el, { kind: 'chart', heading, lang: ctx.lang });

  let disposed = false;
  /** @type {any} */
  let chart = null;
  /** @type {ResizeObserver | null} */
  let ro = null;
  /** @type {IntersectionObserver | null} */
  let io = null;

  const fail = (/** @type {unknown} */ err) => {
    if (disposed) return;
    console.error('lovia chart failed:', err);
    chart?.dispose();
    chart = null;
    const div = document.createElement('div');
    div.className = 'tool-ui-notice';
    div.textContent = L.failed;
    fig.replaceWith(div);
  };

  // Images need the drawn chart: disabled until ECharts has loaded and drawn it.
  const images = [
    tool(L.png, 'PNG', async () => save(await svgToPng(chartSvg(chart, heading)), filename('png'))),
    tool(L.svg, 'SVG', () =>
      save(new Blob([chartSvg(chart, heading).svg], { type: 'image/svg+xml' }), filename('svg')),
    ),
  ];
  for (const b of images) b.disabled = true;
  const rows = optionRows(option);
  tool(rows ? L.csv : L.json, rows ? 'CSV' : 'JSON', () => {
    if (rows) save(csvBlob(rows), filename('csv'));
    else save(new Blob([JSON.stringify(clean, null, 2)], { type: 'application/json' }), filename('json'));
  });

  const start = async () => {
    const ec = await loadEcharts();
    if (disposed) return;
    chart = ec.init(box, themeFromCss(), {
      renderer: 'svg',
      locale: ctx.lang === 'zh' ? 'ZH' : 'EN',
    });
    chart.setOption(option);
    for (const b of images) b.disabled = false;
    ro = new ResizeObserver(() => chart?.resize());
    ro.observe(box);
  };

  // Draw when (nearly) on screen: replaying a long chat stays cheap.
  if (typeof IntersectionObserver === 'undefined') {
    start().catch(fail);
  } else {
    io = new IntersectionObserver(
      (entries) => {
        if (!entries.some((e) => e.isIntersecting)) return;
        io?.disconnect();
        start().catch(fail);
      },
      { rootMargin: '200px' },
    );
    io.observe(box);
  }

  return () => {
    disposed = true;
    io?.disconnect();
    ro?.disconnect();
    chart?.dispose();
  };
}

registerRenderer('chart', renderChart);
