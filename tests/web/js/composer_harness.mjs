// A composer completion menu (composer-complete.js plus one spec module)
// wired to a minimal fake DOM, for the skill_menu and file_menu tests.

import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

/** A module's source with its imports and `export`s stripped, so several
 * can share one vm context. */
export function moduleSource(name) {
  return readFileSync(new URL(`../../../lovia/web/static/js/${name}`, import.meta.url), 'utf8')
    .replace(/^import .*;$/gm, '')
    .replace(/^export /gm, '');
}

/** `names` evaluated in a fresh context; returns the context with `exports` on it. */
export function evaluate(names, exports, context = {}) {
  const source = names.map(moduleSource).join('\n');
  const tail = exports.map((n) => `globalThis.${n} = ${n};`).join('\n');
  runInNewContext(`${source}\n${tail}`, context);
  return context;
}

/** A stand-in element: attributes, listeners (fired by hand), children. */
function fakeElement(onRemove = () => {}) {
  const listeners = {};
  const attrs = {};
  return {
    style: {},
    children: [],
    textContent: '',
    setAttribute(k, v) {
      attrs[k] = String(v);
    },
    getAttribute: (k) => attrs[k] ?? null,
    removeAttribute(k) {
      delete attrs[k];
    },
    addEventListener(type, fn) {
      (listeners[type] ||= []).push(fn);
    },
    fire(type, props = {}) {
      for (const fn of listeners[type] || []) {
        fn({ type, preventDefault() {}, stopImmediatePropagation() {}, ...props });
      }
    },
    append(...c) {
      this.children.push(...c);
    },
    replaceChildren(...c) {
      this.children = c;
    },
    remove() {
      onRemove(this);
    },
    showPopover() {},
    scrollIntoView() {},
    getBoundingClientRect: () => ({ left: 0, top: 500, bottom: 540, width: 300, height: 100 }),
  };
}

/**
 * The menu from `spec` (a module name) wired to a fake composer by its
 * `init` function. `api` stubs the module's fetch: `fetches` collects the
 * pending calls (`{ where, resolve, reject }`), each settled by the test.
 */
export function harness(spec, init, apiName, { store: storeExtra = {} } = {}) {
  const open = new Set();
  const fetches = [];
  const handlers = {};
  const input = Object.assign(fakeElement(), {
    value: '',
    selectionStart: 0,
    selectionEnd: 0,
    form: fakeElement(),
    closest: () => null,
    focus() {},
    setSelectionRange(a, b) {
      this.selectionStart = a;
      this.selectionEnd = b;
    },
    dispatchEvent(ev) {
      this.fire(ev.type);
    },
  });
  const store = {
    sessionId: 's1',
    agent: 'a',
    agents: [{ name: 'a', workspace: true }],
    on(event, fn) {
      (handlers[event] ||= []).push(fn);
    },
    emit(event, payload) {
      for (const fn of handlers[event] || []) fn(payload);
    },
    ...storeExtra,
  };
  const context = evaluate(['composer-complete.js', spec], [init], {
    api: {
      [apiName]: (where) =>
        new Promise((resolve, reject) => {
          fetches.push({ where, resolve, reject });
        }),
    },
    store,
    t: (k, params) => (params ? `${k} ${JSON.stringify(params)}` : k),
    draftWorkspaceRoot: () => null,
    document: {
      createElement: () => fakeElement((m) => open.delete(m)),
      body: { append: (m) => open.add(m) },
    },
    window: { addEventListener() {} },
    innerWidth: 1000,
    Event: class {
      constructor(type) {
        this.type = type;
      }
    },
  });
  context[init](input);
  const type = (text, caret = text.length) => {
    input.value = text;
    input.selectionStart = input.selectionEnd = caret;
    input.fire('input');
  };
  const menu = () => [...open][0] ?? null;
  // The menu's rows (option labels, in order) and its note, if any.
  const rows = () =>
    (menu()?.children ?? [])
      .filter((c) => c.getAttribute('role') === 'option')
      .map((b) => b.children[0].textContent);
  const note = () =>
    (menu()?.children ?? []).find((c) => c.className === 'lv-complete-menu-note')?.textContent ??
    null;
  return { input, type, fetches, store, rows, note, isOpen: () => open.size > 0 };
}

export const settle = () => new Promise((r) => setImmediate(r));
