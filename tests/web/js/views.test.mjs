// Views (views.js): registration, opening and closing, the URL, link clicks,
// the sidebar's current link, and the render lifecycle — against a small
// fake of the few DOM pieces the module touches.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { beforeEach, test } from 'node:test';

const el = (tag = 'div') => ({
  tag,
  hidden: true,
  className: '',
  textContent: '',
  children: [],
  replaceChildren(...kids) {
    this.children = kids;
  },
});

/** A link as views.js reads it; `inTurn` puts it inside a message. */
const link = (href, { target = null, download = false, inTurn = false } = {}) => {
  const attrs = new Map([['href', href]]);
  if (target) attrs.set('target', target);
  if (download) attrs.set('download', '');
  const a = {
    getAttribute: (k) => attrs.get(k) ?? null,
    hasAttribute: (k) => attrs.has(k),
    setAttribute: (k, v) => attrs.set(k, v),
    removeAttribute: (k) => attrs.delete(k),
    closest: (sel) => (sel === 'a[href]' ? a : sel === '[data-part="turn"]' && inTurn ? {} : null),
  };
  return a;
};

const page = {
  view: el('section'),
  title: el('h1'),
  chat: { inert: false },
  composer: { inert: false },
  bodyClasses: new Set(),
  nav: [],
  onClick: null,
};
globalThis.Element = Object;
globalThis.document = /** @type {any} */ ({
  getElementById: (id) =>
    ({ view: page.view, 'view-title': page.title, 'chat-region': page.chat, composer: page.composer })[
      id
    ] ?? null,
  createElement: (tag) => el(tag),
  querySelectorAll: () => page.nav,
  addEventListener: (type, fn) => {
    if (type === 'click') page.onClick = fn;
  },
  body: {
    classList: {
      toggle: (name, on) => (on ? page.bodyClasses.add(name) : page.bodyClasses.delete(name)),
    },
  },
});
globalThis.location = /** @type {any} */ (new URL('http://host/app/?session=s1'));
globalThis.history = /** @type {any} */ ({
  replaceState: (_s, _t, url) => {
    globalThis.location = /** @type {any} */ (new URL(String(url)));
  },
});

const views = await import('../../../lovia/web/static/js/views.js');
const { closeView, configureViews, currentView, linkTarget, openView, parseTarget, registerView } =
  views;

const changes = [];
const notes = [];
let shown = 0;
configureViews({
  changed: (v) => changes.push(v),
  notify: (t) => notes.push(t),
  shown: () => shown++,
});
views.initViews();

const tick = () => new Promise((r) => setTimeout(r, 0));
const click = (a, over = {}) => {
  const e = { target: a, button: 0, defaultPrevented: false, prevented: false, ...over };
  e.preventDefault = () => {
    e.prevented = true;
  };
  page.onClick(e);
  return e;
};

beforeEach(() => {
  closeView();
  changes.length = 0;
  notes.length = 0;
});

test('targets split into an id and a path', () => {
  assert.deepEqual(parseTarget('exec'), { id: 'exec', path: '' });
  assert.deepEqual(parseTarget('exec/perf/p95'), { id: 'exec', path: 'perf/p95' });
});

test('registerView checks the id and the view', () => {
  for (const bad of ['', 'Exec', '-x', 'a b', 'lovia-x', 3]) {
    assert.throws(() => registerView(/** @type {any} */ (bad), { render() {} }), /invalid id/);
  }
  assert.throws(() => registerView('ok', /** @type {any} */ ({})), /render\(el, ctx\)/);
  assert.throws(() => openView('nope'), /no view "nope"/);
});

test('opening a view shows it, titles it, and records it beside the session', () => {
  const seen = [];
  registerView('exec', { title: 'Execution', render: (el, ctx) => seen.push([el.className, ctx.path]) });
  const nav = link('?view=exec');
  const other = link('?view=design');
  const plain = link('/help');
  plain.setAttribute('aria-current', 'page'); // the deployer's own; not ours to clear
  page.nav = [nav, other, plain];

  openView('exec/perf');
  assert.deepEqual(seen, [['view-body', 'perf']]);
  assert.equal(currentView(), 'exec/perf');
  assert.ok(page.bodyClasses.has('view-open'));
  assert.equal(page.view.hidden, false);
  assert.equal(page.title.hidden, false);
  assert.equal(page.title.textContent, 'Execution');
  assert.ok(page.chat.inert && page.composer.inert); // laid out underneath, out of reach
  assert.equal(location.search, '?session=s1&view=exec/perf');
  assert.equal(nav.getAttribute('aria-current'), 'page'); // by id, whatever the path
  assert.equal(other.getAttribute('aria-current'), null);
  assert.equal(plain.getAttribute('aria-current'), 'page');
  assert.deepEqual(changes, ['exec/perf']);

  closeView();
  assert.equal(currentView(), null);
  assert.ok(!page.bodyClasses.has('view-open'));
  assert.ok(!page.chat.inert && !page.composer.inert);
  assert.equal(page.view.hidden, true);
  assert.equal(location.search, '?session=s1');
  assert.equal(nav.getAttribute('aria-current'), null);
  assert.deepEqual(changes, ['exec/perf', null]);
  page.nav = [];
});

test('listeners hear of a view after it drew, and every drawing is reported', () => {
  const order = [];
  registerView('first', { render: () => order.push('render first') });
  const before = shown;
  const off = (v) => order.push(`changed ${v}`);
  configureViews({ changed: off });
  try {
    openView('first');
    openView('first'); // drawn again, not changed
  } finally {
    configureViews({ changed: (v) => changes.push(v) });
  }
  assert.deepEqual(order, ['render first', 'changed first', 'render first']);
  assert.equal(shown - before, 2);
});

test('a listener that opens another view wins, after the first one cleans up', async () => {
  const log = [];
  registerView('from', { render: () => (log.push('render from'), () => log.push('cleanup from')) });
  registerView('to', { render: () => log.push('render to') });
  configureViews({
    changed: (v) => {
      changes.push(v);
      if (v === 'from') openView('to');
    },
  });
  try {
    openView('from');
    await tick();
  } finally {
    configureViews({ changed: (v) => changes.push(v) });
  }
  assert.equal(currentView(), 'to');
  // A synchronous cleanup runs before the next render, not a microtask later.
  assert.deepEqual(log, ['render from', 'cleanup from', 'render to']);
  assert.equal(page.view.children.length, 1);
  assert.deepEqual(changes, ['from', 'to']);
});

test('each render is cleaned up before the next, and on close', async () => {
  const log = [];
  registerView('a', { render: (_el, ctx) => () => log.push(`clean ${ctx.path || 'a'}`) });
  openView('a');
  await tick();
  openView('a/tab');
  await tick();
  closeView();
  assert.deepEqual(log, ['clean a', 'clean tab']);
});

test('a render that throws or rejects shows the failure note', async () => {
  const quiet = console.error;
  console.error = () => {};
  try {
    registerView('boom', {
      render() {
        throw new Error('x');
      },
    });
    openView('boom');
    assert.equal(page.view.children[0].children[0].className, 'view-failed');

    registerView('later', { render: async () => Promise.reject(new Error('y')) });
    openView('later');
    await tick();
    assert.equal(page.view.children[0].children[0].className, 'view-failed');
  } finally {
    console.error = quiet;
  }
});

test('an async render that lands after its view was left is cleaned up at once', async () => {
  let finish;
  const cleaned = [];
  registerView('slow', {
    render: () => new Promise((r) => (finish = () => r(() => cleaned.push('slow')))),
  });
  openView('slow');
  closeView();
  finish();
  await tick();
  assert.deepEqual(cleaned, ['slow']);
});

test('re-registering an open view redraws it; null closes it', () => {
  const drawn = [];
  registerView('re', { render: () => drawn.push('v1') });
  openView('re');
  registerView('re', { render: () => drawn.push('v2') });
  assert.deepEqual(drawn, ['v1', 'v2']);
  registerView('re', null);
  assert.equal(currentView(), null);
  assert.throws(() => openView('re'), /no view/);
});

test('an in-page anchor in a view stays a browser jump', () => {
  const drawn = [];
  registerView('docs', { render: () => drawn.push('docs') });
  openView('docs');
  // Resolved against ?…&view=docs, `#bottom` carries the view along.
  assert.equal(linkTarget(link('#bottom'), location), null);
  assert.equal(linkTarget(link('?session=s1&view=docs#bottom'), location), null);
  assert.equal(click(link('#bottom')).prevented, false);
  assert.deepEqual(drawn, ['docs']); // not redrawn
  // A fragment on a link to another view is still a view link.
  assert.equal(linkTarget(link('?view=exec#top'), location), 'exec');
});

test('linkTarget: only same-page links to a view, opened in place', () => {
  const here = location;
  assert.equal(linkTarget(link('?view=exec/perf'), here), 'exec/perf');
  assert.equal(linkTarget(link('?view='), here), '');
  assert.equal(linkTarget(link('?session=s2'), here), null);
  assert.equal(linkTarget(link('?view=exec', { target: '_blank' }), here), null);
  assert.equal(linkTarget(link('?view=exec', { target: '_self' }), here), 'exec');
  assert.equal(linkTarget(link('?view=exec', { download: true }), here), null);
  assert.equal(linkTarget(link('/other/?view=exec'), here), null);
  assert.equal(linkTarget(link('https://elsewhere.example/app/?view=exec'), here), null);
});

test('plain clicks on view links navigate; others stay browser links', () => {
  registerView('dash', { render() {} });
  assert.equal(click(link('?view=dash'), { metaKey: true }).prevented, false);
  assert.equal(click(link('?view=dash'), { button: 1 }).prevented, false);
  assert.equal(click(link('?view=dash', { inTurn: true })).prevented, false); // model output
  assert.equal(click(link('/help')).prevented, false);
  assert.equal(currentView(), null);

  assert.equal(click(link('?view=dash')).prevented, true);
  assert.equal(currentView(), 'dash');
  click(link('?view='));
  assert.equal(currentView(), null);

  assert.equal(click(link('?view=ghost')).prevented, true);
  assert.equal(currentView(), null);
  assert.deepEqual(notes, ['No view named ghost']);
});

test('the URL view opens at the start; an unknown one is dropped with a note', () => {
  registerView('home', { render() {} });
  history.replaceState({}, '', 'http://host/app/?session=s1&view=home/x');
  views.openViewFromURL();
  assert.equal(currentView(), 'home/x');
  closeView();

  history.replaceState({}, '', 'http://host/app/?view=gone');
  views.openViewFromURL();
  assert.equal(currentView(), null);
  assert.equal(location.search, '');
  assert.deepEqual(notes, ['No view named gone']);
});

test('a run waiting on the user is announced only while a view hides it', () => {
  views.noteAwaiting();
  assert.deepEqual(notes, []);
  registerView('w', { render() {} });
  openView('w');
  views.noteAwaiting();
  assert.deepEqual(notes, ['The chat is waiting for you']);
});
