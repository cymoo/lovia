// The chart component's sanitizer (charts.js): what an untrusted ECharts
// option may and may not carry into the page.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { sanitizeOption } from '../../../lovia/web/static/js/charts.js';

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

test('tooltips are forced to rich text and confined', () => {
  const out = sanitizeOption({
    tooltip: { trigger: 'axis', formatter: '<img src=x onerror=alert(1)>' },
    series: [bar],
  });
  assert.equal(out.tooltip.renderMode, 'richText');
  assert.equal(out.tooltip.confine, true);
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
