// window.lovia — the page's public extension API, and the only surface a
// deployer's scripts (ChatUI.scripts) should touch. Every other module is
// internal. Members are a documented contract (web-server.md); keep this
// object the one place they are assembled.

import { getRenderer, registerRenderer } from './components.js';
import { pageConfig } from './page-config.js';

/** @type {() => void} */
let markReady = () => {};
const ready = new Promise((resolve) => {
  markReady = () => resolve(undefined);
});

export const lovia = Object.freeze({
  /** The lovia package version serving this page. */
  version: typeof pageConfig.version === 'string' ? pageConfig.version : '',
  /** Settles once the app's start is over: the page is wired, and the first
   * chat-list request and the URL's chat have loaded or failed. Not when the
   * agent list is refused (signed out). The start waits for extension
   * modules, so one that awaits this at its top level stalls until the load
   * timeout. */
  ready,
  registerRenderer,
  getRenderer,
});

export { markReady };
