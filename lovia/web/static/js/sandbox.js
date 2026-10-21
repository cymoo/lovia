// Frames for untrusted documents — the html component's pages and the Files
// panel's HTML preview. Design and the browser facts behind it:
// docs/design/web-sandbox-frames.md.
//
// `sandbox="allow-scripts"` alone gives a document an opaque origin, but it
// may still navigate itself to a URL that carries its data away, and no CSP
// the document holds can stop that. A frame's navigations are governed by
// the frame-src of the document embedding it, so the untrusted document sits
// inside a proxy of ours whose only policy is `frame-src 'none'`. The chat
// page itself sets no CSP and stays free to embed whatever the deployer
// wants.
//
// No DOM access at import time (proxyDoc is unit-tested under node).

import { escapeHtml } from './util.js';

// Relays what the untrusted document posts to its parent, so the page sees
// those messages arrive from the proxy's window (and checks them there).
const RELAY =
  "const f=document.querySelector('iframe');" +
  "addEventListener('message',(e)=>{if(e.source===f.contentWindow)parent.postMessage(e.data,'*')});";

/**
 * The proxy's document. Its policy holds only frame-src: the untrusted
 * document inherits the proxy's policies, and anything broader would tighten
 * it beyond its own.
 * @param {string} doc The untrusted document.
 * @param {{ title: string, colorScheme?: 'light' | 'dark' }} opts `colorScheme`:
 *   the scheme the document paints in. A frame whose scheme differs from its
 *   embedder's gets an opaque backdrop, so the proxy must match it.
 * @returns {string}
 */
export function proxyDoc(doc, { title, colorScheme }) {
  const scheme = colorScheme ? `color-scheme:${colorScheme};` : '';
  return (
    '<!doctype html><html><head><meta charset="utf-8">' +
    `<meta http-equiv="Content-Security-Policy" content="frame-src 'none'">` +
    `<style>html,body{margin:0;height:100%;overflow:hidden;${scheme}}` +
    'iframe{display:block;border:0;width:100%;height:100%}</style></head><body>' +
    `<iframe sandbox="allow-scripts" referrerpolicy="no-referrer" title="${escapeHtml(title)}"` +
    ` srcdoc="${escapeHtml(doc)}"></iframe>` +
    `<script>${RELAY}</script></body></html>`
  );
}

/**
 * An iframe that runs `doc` untrusted (see the header). Messages the
 * document posts to its parent arrive from this frame's window.
 * @param {string} doc
 * @param {{ title: string, className: string, colorScheme?: 'light' | 'dark' }} opts
 * @returns {HTMLIFrameElement}
 */
export function sandboxFrame(doc, { title, className, colorScheme }) {
  const frame = document.createElement('iframe');
  frame.className = className;
  frame.setAttribute('sandbox', 'allow-scripts');
  frame.setAttribute('referrerpolicy', 'no-referrer');
  frame.title = title;
  frame.srcdoc = proxyDoc(doc, { title, colorScheme });
  return frame;
}
