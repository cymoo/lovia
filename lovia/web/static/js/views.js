// Views — a deployer's pages, shown in the main area in place of the chat
// (lovia.registerView). Design: docs/design/web-views.md.
//
// A view covers the chat rather than leaving it: the session stays current,
// its run keeps streaming, and closing the view shows the chat as it was.
// The open view lives in the URL (?view=<id>[/<path>]) beside ?session=, and
// a plain link to one (`<a href="?view=dashboard">`) opens it.
//
// No DOM access at import time (the pure helpers are unit-tested under node).

import { t } from './i18n.js';
import { Registry } from './registry.js';

const ID = /^[a-z0-9][a-z0-9-]*$/;

/** @type {Registry<View>} */
const views = new Registry();
/** @type {{ id: string, path: string } | null} */
let open = null;
/** @type {string | null} */
let announced = null;
// Bumped on every render and teardown, so a slow async render that lands
// after its view was left is cleaned up instead of adopted.
let gen = 0;
/** @type {(() => void) | null} */
let cleanup = null;

const host = {
  /** @param {string | null} _view */
  changed: (_view) => {},
  /** @param {string} _text */
  notify: (_text) => {},
  /** A view was drawn (each time, not only on a change). */
  shown: () => {},
};

/**
 * Supply what the page does on a change (main.js).
 * @param {Partial<typeof host>} impl
 */
export function configureViews(impl) {
  Object.assign(host, impl);
}

/**
 * `'exec/perf'` → `{ id: 'exec', path: 'perf' }`.
 * @param {string} target
 */
export function parseTarget(target) {
  const i = target.indexOf('/');
  return i < 0 ? { id: target, path: '' } : { id: target.slice(0, i), path: target.slice(i + 1) };
}

/** @returns {string | null} The open view, as in the URL; null on the chat. */
export function currentView() {
  if (!open) return null;
  return open.path ? `${open.id}/${open.path}` : open.id;
}

/**
 * The view a link opens, or null when it is no link to a view: another page,
 * another target, a download, or an anchor on this one.
 * @param {Element} a
 * @param {{ href: string, origin: string, pathname: string, search: string }} here
 * @returns {string | null} `''` for `?view=` (back to the chat).
 */
export function linkTarget(a, here) {
  const target = a.getAttribute('target');
  if ((target && target !== '_self') || a.hasAttribute('download')) return null;
  let url;
  try {
    url = new URL(a.getAttribute('href') ?? '', here.href);
  } catch {
    return null;
  }
  if (url.origin !== here.origin || url.pathname !== here.pathname) return null;
  // `#bottom` resolves against the current URL, view included: an in-page
  // jump the browser does itself, not a navigation to redraw the view for.
  if (url.hash && url.search === here.search) return null;
  return url.searchParams.get('view');
}

/**
 * @param {string} id
 * @param {View} view
 * @returns {() => void} Removes it, bringing back the view it replaced; an
 *   open view with nothing left under its id closes.
 */
export function registerView(id, view) {
  if (typeof id !== 'string' || !ID.test(id) || id.startsWith('lovia')) {
    throw new TypeError(
      `lovia.registerView: invalid id ${JSON.stringify(id)} (a-z, 0-9 and '-'; "lovia…" is reserved)`,
    );
  }
  if (!view || typeof view.render !== 'function') {
    throw new TypeError('lovia.registerView: expected a view with render(el, ctx)');
  }
  const redraw = () => {
    if (open?.id !== id) return;
    if (views.has(id)) show(open);
    else closeView();
  };
  const dispose = views.add(id, view, redraw);
  redraw();
  return dispose;
}

/** @param {string} target `'exec'`, or `'exec/perf'` with a path. */
export function openView(target) {
  const next = parseTarget(String(target));
  if (!views.has(next.id)) {
    throw new TypeError(`lovia.openView: no view ${JSON.stringify(next.id)} is registered`);
  }
  show(next);
}

export function closeView() {
  if (!open) return;
  open = null;
  teardown();
  apply();
  announce();
}

/** Open the view in the URL, at the start. One that isn't registered is dropped. */
export function openViewFromURL() {
  const target = new URLSearchParams(location.search).get('view');
  if (!target) return;
  const next = parseTarget(target);
  if (views.has(next.id)) {
    show(next);
  } else {
    host.notify(t('view.unknown', { name: next.id }));
    syncURL(null);
  }
}

/** A run on screen now waits for the user: say so if a view hides it. */
export function noteAwaiting() {
  if (open) host.notify(t('view.needsYou'));
}

/** Route plain clicks on links to views (outside messages) through openView. */
export function initViews() {
  document.addEventListener('click', (e) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) {
      return;
    }
    const a = e.target instanceof Element ? e.target.closest('a[href]') : null;
    // A link in a message stays a link: model output doesn't navigate.
    if (!a || a.closest('[data-part="turn"]')) return;
    const target = linkTarget(a, location);
    if (target === null) return;
    e.preventDefault();
    if (!target) return closeView();
    const next = parseTarget(target);
    if (views.has(next.id)) show(next);
    else host.notify(t('view.unknown', { name: next.id }));
  });
}

/** @param {{ id: string, path: string }} next */
function show(next) {
  teardown();
  open = next;
  apply();
  const container = /** @type {HTMLElement} */ (document.getElementById('view'));
  const el = document.createElement('div');
  el.className = 'lv-view-body';
  container.replaceChildren(el);
  const mine = ++gen;
  const fail = (/** @type {unknown} */ err) => {
    console.error(`lovia view "${next.id}" failed to render:`, err);
    if (mine !== gen) return;
    const note = document.createElement('p');
    note.className = 'lv-view-failed';
    note.textContent = t('view.failed');
    el.replaceChildren(note);
  };
  const adopt = (/** @type {unknown} */ fn) => {
    if (typeof fn !== 'function') return;
    if (mine === gen) cleanup = /** @type {() => void} */ (fn);
    else runCleanup(/** @type {() => void} */ (fn));
  };
  let out;
  try {
    out = /** @type {View} */ (views.get(next.id)).render(el, Object.freeze({ path: next.path }));
  } catch (err) {
    fail(err);
  }
  // A cleanup returned synchronously is held at once, so whatever replaces
  // this view (even from a `view` listener, below) runs it first.
  if (typeof (/** @type {any} */ (out))?.then === 'function') out.then(adopt, fail);
  else adopt(out);
  // Listeners hear of the view once it has drawn, so one that opens another
  // view from its listener replaces this one rather than being drawn over.
  host.shown();
  announce();
}

function teardown() {
  gen++;
  const fn = cleanup;
  cleanup = null;
  if (fn) runCleanup(fn);
  document.getElementById('view')?.replaceChildren();
}

/** @param {() => void} fn */
function runCleanup(fn) {
  try {
    fn();
  } catch (err) {
    console.error('lovia: a view cleanup failed:', err);
  }
}

// Reflect the open view on the page: what shows, the title, the URL, and the
// sidebar's current link.
function apply() {
  const view = currentView();
  document.body.classList.toggle('lv-view-open', !!open);
  const container = document.getElementById('view');
  if (container) container.hidden = !open;
  // The chat underneath stays laid out but out of reach: no focus, no clicks.
  for (const id of ['chat-region', 'composer']) {
    const el = document.getElementById(id);
    if (el) el.inert = !!open;
  }
  const title = document.getElementById('view-title');
  if (title) {
    title.hidden = !open;
    title.textContent = open ? (views.get(open.id)?.title ?? '') : '';
  }
  syncURL(view);
  for (const a of document.querySelectorAll('[data-part="sidebar-nav"] a[href]')) {
    const target = linkTarget(a, location);
    if (target === null) continue;
    if (open && target && parseTarget(target).id === open.id) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
}

function announce() {
  const view = currentView();
  if (view === announced) return;
  announced = view;
  host.changed(view);
}

/** @param {string | null} view */
function syncURL(view) {
  const url = new URL(location.href);
  if (view) url.searchParams.set('view', view);
  else url.searchParams.delete('view');
  // `/` is fine in a query; keep `?view=exec/perf` readable.
  url.search = url.search.replace(/%2F/gi, '/');
  history.replaceState({}, '', url);
}
