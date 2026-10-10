// UI components (components.js): envelope parsing, and the renderer registry's
// lifecycle — late registration, replacement, failures, async cleanup.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  configureComponents,
  mountComponent,
  parseEnvelope,
  registerRenderer,
  rerenderComponents,
  sweepComponents,
  unmountComponent,
} from '../../../lovia/web/static/js/components.js';

// Just enough DOM for the registry: notices are created elements, and a mount
// point only needs replaceChildren + isConnected.
globalThis.document = /** @type {any} */ ({
  createElement: () => ({ className: '', textContent: '' }),
});
const el = () => ({
  isConnected: true,
  children: /** @type {any[]} */ ([]),
  replaceChildren(...c) {
    this.children = c;
  },
});
const noticeOf = (e) => e.children[0]?.textContent;
const tick = () => new Promise((r) => setTimeout(r, 0));

configureComponents({ label: (key, kind) => `${key}:${kind}` });

// ---- parseEnvelope ---------------------------------------------------------

test('inline props', () => {
  const text = '{"lovia_ui": {"kind": "table", "props": {"rows": [[1]]}}}';
  assert.deepEqual(parseEnvelope(text), { kind: 'table', props: { rows: [[1]] } });
});

test('from_args: the whole arguments object, or one field', () => {
  const args = '{"option": {"series": []}, "n": 2}';
  assert.deepEqual(parseEnvelope('{"lovia_ui": {"kind": "chart", "from_args": true}}', args), {
    kind: 'chart',
    props: { option: { series: [] }, n: 2 },
  });
  assert.deepEqual(
    parseEnvelope('{"lovia_ui": {"kind": "chart", "from_args": "option"}}', args),
    { kind: 'chart', props: { series: [] } },
  );
});

test('ordinary or malformed output is not a component', () => {
  for (const text of [
    'plain text',
    '{"other": 1}',
    ' {"lovia_ui" broken',
    '{"lovia_ui": null}',
    '{"lovia_ui": {"kind": "Bad Kind", "props": 1}}',
    '{"lovia_ui": {"props": 1}}', // RegExp.test(undefined) would match "undefined"
    '{"lovia_ui": {"kind": null, "props": 1}}',
    '{"lovia_ui": {"kind": true, "props": 1}}',
    '{"lovia_ui": {"kind": "x"}}',
    '{"lovia_ui": {"kind": "x", "from_args": ""}}',
  ]) {
    assert.equal(parseEnvelope(text, '{}'), null, text);
  }
});

test('from_args with missing, unparsable, or non-object arguments', () => {
  const env = '{"lovia_ui": {"kind": "chart", "from_args": "option"}}';
  assert.equal(parseEnvelope(env, undefined), null);
  assert.equal(parseEnvelope(env, 'not json'), null);
  assert.equal(parseEnvelope(env, '[1]'), null);
  assert.equal(parseEnvelope(env, '{"other": 1}'), null);
});

// ---- registry --------------------------------------------------------------

test('an unknown kind shows a notice, then upgrades once registered', () => {
  const e = el();
  mountComponent(/** @type {any} */ (e), { kind: 'late', props: { n: 1 } });
  assert.equal(noticeOf(e), 'missing:late');
  /** @type {any[]} */
  const seen = [];
  registerRenderer('late', (target, props, ctx) => {
    seen.push([target, props, ctx.kind]);
  });
  assert.deepEqual(seen, [[e, { n: 1 }, 'late']]);
  assert.deepEqual(e.children, []); // the notice was cleared for the renderer
  unmountComponent(/** @type {any} */ (e));
});

test('re-registering a kind re-renders it, after the old cleanup', () => {
  const log = [];
  registerRenderer('swap', () => () => log.push('old cleanup'));
  const e = el();
  mountComponent(/** @type {any} */ (e), { kind: 'swap', props: null });
  return tick().then(() => {
    registerRenderer('swap', () => {
      log.push('new render');
    });
    assert.deepEqual(log, ['old cleanup', 'new render']);
    unmountComponent(/** @type {any} */ (e));
  });
});

test('a throwing or rejecting renderer shows the failure notice', async () => {
  registerRenderer('boom', () => {
    throw new Error('x');
  });
  registerRenderer('reject', async () => {
    throw new Error('y');
  });
  const a = el();
  const b = el();
  const quiet = console.error;
  console.error = () => {};
  try {
    mountComponent(/** @type {any} */ (a), { kind: 'boom', props: 1 });
    mountComponent(/** @type {any} */ (b), { kind: 'reject', props: 1 });
    await tick();
  } finally {
    console.error = quiet;
  }
  assert.equal(noticeOf(a), 'failed:boom');
  assert.equal(noticeOf(b), 'failed:reject');
  unmountComponent(/** @type {any} */ (a));
  unmountComponent(/** @type {any} */ (b));
});

test('an async cleanup arriving after unmount runs at once', async () => {
  let release = () => {};
  let cleaned = 0;
  registerRenderer(
    'slow',
    () => new Promise((resolve) => {
      release = () => resolve(() => cleaned++);
    }),
  );
  const e = el();
  mountComponent(/** @type {any} */ (e), { kind: 'slow', props: 1 });
  unmountComponent(/** @type {any} */ (e));
  release();
  await tick();
  assert.equal(cleaned, 1);
});

test('rerender and sweep: cleanup runs once per render; detached mounts go', async () => {
  let renders = 0;
  let cleanups = 0;
  registerRenderer('count', () => {
    renders++;
    return () => cleanups++;
  });
  const e = el();
  mountComponent(/** @type {any} */ (e), { kind: 'count', props: 1 });
  await tick();
  rerenderComponents();
  await tick();
  assert.deepEqual([renders, cleanups], [2, 1]);
  e.isConnected = false;
  sweepComponents();
  assert.equal(cleanups, 2);
  rerenderComponents(); // swept: no longer rendered
  assert.equal(renders, 2);
});

test('ctx.send is refused once the component left the page', () => {
  const sent = [];
  configureComponents({ send: (text) => (sent.push(text), true) });
  /** @type {any} */
  let ctx;
  registerRenderer('sender', (_el, _props, c) => {
    ctx = c;
  });
  const e = el();
  mountComponent(/** @type {any} */ (e), { kind: 'sender', props: 1 });
  assert.equal(ctx.send('hi'), true);
  e.isConnected = false;
  assert.equal(ctx.send('again'), false);
  assert.deepEqual(sent, ['hi']);
  unmountComponent(/** @type {any} */ (e));
});

test('invalid registrations throw', () => {
  assert.throws(() => registerRenderer('Bad', () => {}), TypeError);
  assert.throws(() => registerRenderer('ok', /** @type {any} */ (null)), TypeError);
});
