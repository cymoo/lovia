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
// No DOM access at import time (sanitizeOption is unit-tested under node).

import { registerRenderer } from './components.js';

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
  // doesn't. `confine` keeps it inside the card.
  if (out.tooltip) {
    const rich = (/** @type {any} */ t) => ({ ...t, renderMode: 'richText', confine: true });
    out.tooltip = Array.isArray(out.tooltip) ? out.tooltip.map(rich) : rich(out.tooltip);
  }
  out.aria = { enabled: true, ...out.aria };
  return out;
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
    textStyle: { color: text, fontFamily: getComputedStyle(document.body).fontFamily },
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

const FAILED = { en: "Couldn't draw this chart", zh: '无法绘制该图表' };

/** @type {import('./components.js').Renderer} */
function renderChart(el, props, ctx) {
  const option = sanitizeOption(props); // throws → the registry's failure notice
  const box = document.createElement('div');
  box.className = 'chart-box';
  el.append(box);

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
    div.textContent = FAILED[ctx.lang] || FAILED.en;
    box.replaceWith(div);
  };

  const start = async () => {
    const ec = await loadEcharts();
    if (disposed) return;
    chart = ec.init(box, themeFromCss(), {
      renderer: 'svg',
      locale: ctx.lang === 'zh' ? 'ZH' : 'EN',
    });
    chart.setOption(option);
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
