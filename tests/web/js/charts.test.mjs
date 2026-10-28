// The chart component's pure helpers (charts.js): the sanitizer — what an
// untrusted ECharts option may and may not carry into the page — the title
// lift, the data export, and the HTML-export snapshot.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  chartSnapshot,
  liftTitle,
  optionRows,
  sanitizeOption,
} from '../../../lovia/web/static/js/charts.js';
import { toCsv } from '../../../lovia/web/static/js/figure.js';

const bar = { type: 'bar' };

test('keeps a plain option, adding aria', () => {
  const opt = {
    title: { text: 'Revenue' },
    dataset: { source: [['m', 'v'], ['Jan', 1]] },
    xAxis: { type: 'category' },
    yAxis: {},
    series: [bar],
  };
  assert.deepEqual(sanitizeOption(opt), { ...opt, aria: { enabled: true } });
});

test('a single series object becomes a list', () => {
  assert.deepEqual(sanitizeOption({ series: bar }).series, [bar]);
});

test('drops top-level components outside the allowlist', () => {
  const out = sanitizeOption({
    series: [bar],
    graphic: [{ type: 'image', style: { image: 'https://evil.test/p.png' } }],
    toolbox: { feature: { saveAsImage: {} } },
    geo: { map: 'world' },
  });
  assert.deepEqual(Object.keys(out).sort(), ['aria', 'series']);
});

test('drops link/sublink, image keys, and image:// strings anywhere', () => {
  const out = sanitizeOption({
    title: { text: 'T', link: 'javascript:alert(1)', sublink: 'x' },
    series: [
      {
        type: 'scatter',
        symbol: 'image://https://evil.test/p.png',
        itemStyle: { color: { image: 'https://evil.test/p.png', repeat: 'repeat' } },
        data: [[1, 2]],
      },
    ],
  });
  assert.deepEqual(out.title, { text: 'T' });
  assert.deepEqual(out.series[0], {
    type: 'scatter',
    itemStyle: { color: { repeat: 'repeat' } },
    data: [[1, 2]],
  });
});

test('drops CSS/SVG paint URLs, which an SVG fill would fetch', () => {
  const out = sanitizeOption({
    series: [
      {
        type: 'bar',
        itemStyle: { color: 'url(https://tracker.test/p.svg#x)', borderColor: ' URL ("x")' },
        lineStyle: { color: '#2a78d6' },
      },
    ],
  });
  assert.deepEqual(out.series[0], { type: 'bar', itemStyle: {}, lineStyle: { color: '#2a78d6' } });
});

test('a dropped array cell becomes null, keeping dataset rows aligned', () => {
  const out = sanitizeOption({
    dataset: { source: [['m', 'v', 'w'], ['image://x', 1, 2], ['Feb', 3, 4]] },
    series: [bar],
  });
  assert.deepEqual(out.dataset.source, [['m', 'v', 'w'], [null, 1, 2], ['Feb', 3, 4]]);
});

test('tooltips are forced to rich text and confined', () => {
  const out = sanitizeOption({
    tooltip: { trigger: 'axis', formatter: '<img src=x onerror=alert(1)>' },
    series: [bar],
  });
  assert.equal(out.tooltip.renderMode, 'richText');
  assert.equal(out.tooltip.confine, true);
  assert.equal(out.tooltip.hideDelay, 0);
  const many = sanitizeOption({ tooltip: [{}, { renderMode: 'html' }], series: [bar] });
  assert.deepEqual(
    many.tooltip.map((t) => t.renderMode),
    ['richText', 'richText'],
  );
});

test('rejects what cannot be drawn', () => {
  assert.throws(() => sanitizeOption(null), /must be an object/);
  assert.throws(() => sanitizeOption([bar]), /must be an object/);
  assert.throws(() => sanitizeOption({ series: [] }), /no series/);
  assert.throws(() => sanitizeOption({ title: {} }), /no series/);
  assert.throws(() => sanitizeOption({ series: [{ type: 'custom' }] }), /custom/);
  assert.throws(() => sanitizeOption({ series: [{ type: 'map' }] }), /map/);
  assert.throws(() => sanitizeOption({ series: [{}] }), /unsupported/);
});

test('pathologically deep values are cut, not recursed forever', () => {
  let deep = { v: 1 };
  for (let i = 0; i < 100; i++) deep = { n: deep };
  const out = sanitizeOption({ series: [{ ...bar, data: deep }] });
  let depth = 0;
  for (let node = out.series[0].data; node?.n; node = node.n) depth++;
  assert.ok(depth < 40, `depth ${depth}`);
});

// ---- liftTitle --------------------------------------------------------------

test('lifts a top-anchored title into the heading', () => {
  for (const top of [undefined, 'top', 0, 10, '20', '8px', '5%']) {
    const opt = { title: { text: 'Revenue', subtext: 'FY26', left: 'center', top }, series: [bar] };
    const { heading, option } = liftTitle(opt);
    assert.deepEqual(heading, { text: 'Revenue', subtext: 'FY26' }, `top=${top}`);
    assert.deepEqual(option, { series: [bar] });
    assert.ok(opt.title, 'the input is not mutated');
  }
  assert.deepEqual(liftTitle({ title: [{ text: 'T' }], series: [bar] }).heading, {
    text: 'T',
    subtext: '',
  });
});

test('leaves titles placed in the plot, hidden, empty, or several', () => {
  const kept = [
    { text: '75%', left: 'center', top: 'center' }, // a donut's centre label
    { text: 'T', top: 'middle' },
    { text: 'T', top: '40%' },
    { text: 'T', top: 200 },
    { text: 'T', bottom: 0 },
    { text: 'T', show: false },
    { text: '  ' },
    { subtext: 'only a subtext' },
    [{ text: 'A', left: '25%' }, { text: 'B', left: '75%' }], // one per pie
  ];
  for (const title of kept) {
    const opt = { title, series: [bar] };
    const { heading, option } = liftTitle(opt);
    assert.equal(heading, null, JSON.stringify(title));
    assert.equal(option, opt);
  }
});

// ---- optionRows / toCsv -------------------------------------------------------

test('rows from dataset.source: arrays, objects, columns', () => {
  const arrays = [['m', 'v'], ['Jan', 1]];
  assert.deepEqual(optionRows({ dataset: { source: arrays }, series: [bar] }), arrays);
  assert.deepEqual(
    optionRows({ dataset: [{ dimensions: ['m', { name: 'v' }], source: [['Jan', 1]] }], series: [bar] }),
    [['m', 'v'], ['Jan', 1]],
  );
  // An all-text first row is a header, unless sourceHeader says otherwise.
  const text = [['Jan', 'open']];
  assert.deepEqual(optionRows({ dataset: { dimensions: ['m', 's'], source: text }, series: [bar] }), text);
  assert.deepEqual(
    optionRows({ dataset: { dimensions: ['m', 's'], sourceHeader: false, source: text }, series: [bar] }),
    [['m', 's'], ['Jan', 'open']],
  );
  assert.deepEqual(
    optionRows({ dataset: { source: [{ m: 'Jan', v: 1 }, { m: 'Feb', w: 2 }] }, series: [bar] }),
    [['m', 'v', 'w'], ['Jan', 1, null], ['Feb', null, 2]],
  );
  assert.deepEqual(
    optionRows({ dataset: { source: { m: ['Jan', 'Feb'], v: [1] } }, series: [bar] }),
    [['m', 'v'], ['Jan', 1], ['Feb', null]],
  );
});

test('rows from series over a category axis: one column per series', () => {
  const rows = optionRows({
    xAxis: { type: 'category', name: 'month', data: ['Jan', { value: 'Feb' }] },
    yAxis: {},
    series: [
      { type: 'line', name: 'Portfolio', data: [1, { value: 1.02 }] },
      { type: 'line', name: 'S&P 500', data: [1] },
    ],
  });
  assert.deepEqual(rows, [['month', 'Portfolio', 'S&P 500'], ['Jan', 1, 1], ['Feb', 1.02, null]]);
  // Horizontal bars: the categories are on the y axis.
  assert.deepEqual(
    optionRows({ xAxis: {}, yAxis: [{ data: ['a'] }], series: [{ type: 'bar', data: [3] }] }),
    [['', 'value'], ['a', 3]],
  );
});

test('rows from series items otherwise: one row per item', () => {
  // A pie: names and values.
  assert.deepEqual(
    optionRows({ series: { type: 'pie', data: [{ name: 'A', value: 3 }, { name: 'B', value: 5 }] } }),
    [['name', 'value'], ['A', 3], ['B', 5]],
  );
  // Scatter, two series: no names, so no name column.
  assert.deepEqual(
    optionRows({
      series: [
        { type: 'scatter', name: 's1', data: [[1, 2]] },
        { type: 'scatter', data: [[3, 4, 5]] },
      ],
    }),
    [['series', 'value1', 'value2', 'value3'], ['s1', 1, 2], ['series 2', 3, 4, 5]],
  );
  // Candlestick: array values, named by the category axis.
  assert.deepEqual(
    optionRows({ xAxis: { data: ['d1'] }, series: [{ type: 'candlestick', data: [[1, 2, 0, 3]] }] }),
    [['name', 'value1', 'value2', 'value3', 'value4'], ['d1', 1, 2, 0, 3]],
  );
});

test('no rows for data without a table shape', () => {
  assert.equal(optionRows({ series: [{ type: 'sankey', data: [], links: [] }] }), null);
  assert.equal(optionRows({ series: [{ type: 'tree', data: [{ name: 'r', children: [] }] }] }), null);
  assert.equal(optionRows({ series: [{ type: 'line' }] }), null); // data lives elsewhere
  assert.equal(optionRows({ series: [{ type: 'scatter', data: [[{ a: 1 }]] }] }), null);
});

test('toCsv quotes per RFC 4180 and defuses spreadsheet formulas', () => {
  assert.equal(
    toCsv([
      ['name', 'note', 'v'],
      ['a,b', 'say "hi"', -1],
      ['=HYPERLINK("x")', '@cmd', null],
      ['line\nbreak', '+1', { k: 1 }],
    ]),
    [
      'name,note,v',
      '"a,b","say ""hi""",-1',
      `"'=HYPERLINK(""x"")",'@cmd,`,
      `"line\nbreak",'+1,"{""k"":1}"`,
    ].join('\r\n'),
  );
});

// ---- Export snapshot -----------------------------------------------------------

// A stand-in for the ECharts global and the computed styles the theme reads.
function stubEcharts({ failOn } = {}) {
  const calls = { init: [], option: null, disposed: 0 };
  globalThis.echarts = {
    init(dom, _theme, opts) {
      calls.init.push([dom, opts]);
      return {
        setOption(o) {
          if (failOn === 'setOption') throw new Error('bad option');
          calls.option = o;
        },
        renderToSVGString: () => '<svg xmlns="http://www.w3.org/2000/svg"><text>Jan & "Feb"</text></svg>',
        dispose: () => calls.disposed++,
      };
    },
  };
  globalThis.document = /** @type {any} */ ({ documentElement: {}, body: {} });
  globalThis.getComputedStyle = /** @type {any} */ (() => ({ getPropertyValue: () => '', fontFamily: 'sans' }));
  return calls;
}

test('chartSnapshot: SSR to an SVG data image, heading as caption, disposed', async () => {
  const calls = stubEcharts();
  const html = await chartSnapshot(
    { title: { text: 'Sales <Q1>', subtext: 'EUR' }, series: [bar] },
    /** @type {any} */ ({ lang: 'zh' }),
  );
  const [dom, opts] = calls.init[0];
  assert.equal(dom, null);
  assert.deepEqual(opts, { renderer: 'svg', ssr: true, locale: 'ZH', width: 720, height: 360 });
  assert.equal(calls.option.animation, false);
  assert.equal(calls.option.title, undefined); // lifted into the caption
  assert.equal(calls.disposed, 1);
  assert.match(html, /^<figure class="lv-chart"><figcaption>Sales &lt;Q1&gt;<span>EUR<\/span><\/figcaption><img /);
  const src = /src="([^"]+)"/.exec(html)?.[1] ?? '';
  assert.ok(src.startsWith('data:image/svg+xml;charset=utf-8,'));
  assert.match(decodeURIComponent(src.split(',')[1]), /<text>Jan & "Feb"<\/text>/);
  assert.match(html, /alt="Sales &lt;Q1&gt;"/);
});

test('chartSnapshot: an untitled chart still has an accessible name', async () => {
  stubEcharts();
  const html = await chartSnapshot({ series: [bar] }, /** @type {any} */ ({ lang: 'en' }));
  assert.doesNotMatch(html, /figcaption/);
  assert.match(html, /alt="Chart"/);
});

test('chartSnapshot: disposes the instance when drawing fails', async () => {
  const calls = stubEcharts({ failOn: 'setOption' });
  await assert.rejects(chartSnapshot({ series: [bar] }, /** @type {any} */ ({ lang: 'en' })), /bad option/);
  assert.equal(calls.disposed, 1);
});
