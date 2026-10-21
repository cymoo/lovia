// window.lovia — the page's public extension API, and the only surface a
// deployer's scripts (ChatUI.scripts) should touch; every other module is
// internal. Its shape is a documented contract (web-server.md, lovia.d.ts,
// pinned by tests/web/js/lovia_api.test.mjs). It stays DOM-free: main.js
// supplies what the page implements through configureLovia and relays the
// page's events through emitLovia.

import { api, request } from './api.js';
import { getRenderer, registerRenderer } from './components.js';
import { t } from './i18n.js';
import { refreshMessageActions, registerMessageAction } from './message-actions.js';
import { pageConfig } from './page-config.js';
import { registerToolView } from './tool-views.js';
import { closeView, openView, registerView } from './views.js';

/** The events `lovia.on` accepts. */
export const EVENTS = Object.freeze(['session', 'agent', 'run-start', 'run-end', 'theme', 'view']);

/** @type {Map<string, Set<(payload: any) => void>>} */
const listeners = new Map(EVENTS.map((name) => [name, new Set()]));

const host = {
  /** @returns {LoviaState} */
  state: () => ({ sessionId: null, agent: null, theme: 'light', lang: 'en', busy: false, view: null }),
  /** @param {string} _text */
  send: (_text) => {},
  /** @param {string} _text */
  setDraft: (_text) => {},
  newChat: () => {},
  /** @param {string} _id @returns {Promise<void>} */
  openSession: async (_id) => {},
  /** @param {string} _text @param {{ type: 'info' | 'error' }} _opts */
  toast: (_text, _opts) => {},
};

/**
 * Supply the parts of the API the page implements.
 * @param {Partial<typeof host>} impl
 */
export function configureLovia(impl) {
  Object.assign(host, impl);
}

/**
 * Deliver an event to `lovia.on` listeners. One that throws is logged and
 * doesn't stop the rest.
 * @param {string} name One of EVENTS.
 * @param {unknown} payload
 */
export function emitLovia(name, payload) {
  for (const fn of listeners.get(name) ?? []) {
    try {
      fn(payload);
    } catch (err) {
      console.error(`lovia: a "${name}" listener failed:`, err);
    }
  }
}

/** @type {Map<string, () => unknown>} */
const providers = new Map();

/**
 * The client context a run starts with: each `provideContext` key's current
 * value. A provider that throws (or returns undefined) is left out.
 * @returns {Record<string, unknown> | undefined} undefined when there is none.
 */
export function collectContext() {
  /** @type {[string, unknown][]} */
  const entries = [];
  for (const [key, fn] of providers) {
    try {
      const value = fn();
      if (value !== undefined) entries.push([key, value]);
    } catch (err) {
      console.error(`lovia: context provider "${key}" failed:`, err);
    }
  }
  // fromEntries defines own properties: a "__proto__" key stays a key,
  // where assigning it would set the object's prototype instead.
  return entries.length ? Object.fromEntries(entries) : undefined;
}

/** @type {() => void} */
let markReady = () => {};
const ready = new Promise((resolve) => {
  markReady = () => resolve(undefined);
});

/** @type {LoviaApi} */
export const lovia = Object.freeze({
  version: typeof pageConfig.version === 'string' ? pageConfig.version : '',
  // The start waits for extension modules, so one that awaits this at its top
  // level stalls until the load timeout (extensions.js).
  ready,
  get state() {
    return Object.freeze({ ...host.state() });
  },
  on(event, fn) {
    const set = listeners.get(event);
    if (!set) {
      throw new TypeError(
        `lovia.on: unknown event ${JSON.stringify(event)} (one of ${EVENTS.join(', ')})`,
      );
    }
    if (typeof fn !== 'function') throw new TypeError('lovia.on: expected a listener function');
    set.add(fn);
    return () => {
      set.delete(fn);
    };
  },
  registerRenderer,
  getRenderer,
  registerToolView,
  registerMessageAction,
  refreshMessageActions,
  registerView,
  openView,
  closeView,
  provideContext(key, fn) {
    if (typeof key !== 'string' || !key) {
      throw new TypeError('lovia.provideContext: expected a key');
    }
    if (typeof fn !== 'function') {
      throw new TypeError('lovia.provideContext: expected a function returning the value');
    }
    providers.set(key, fn);
  },
  send(text) {
    const message = typeof text === 'string' ? text.trim() : '';
    if (message) host.send(message);
  },
  setDraft(text) {
    host.setDraft(String(text ?? ''));
  },
  newChat() {
    host.newChat();
  },
  openSession(id) {
    return host.openSession(String(id));
  },
  toast(text, opts) {
    host.toast(String(text), { type: opts?.type === 'error' ? 'error' : 'info' });
  },
  api,
  fetch(path, init) {
    if (typeof path !== 'string' || !path.startsWith('/')) {
      throw new TypeError("lovia.fetch: expected an app path starting with '/'");
    }
    return request(path, init);
  },
  t,
});

export { markReady };
