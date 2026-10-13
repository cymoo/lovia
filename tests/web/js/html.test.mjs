// The html component's pure helpers (html.js): the frame's document — our
// CSP and bootstrap ahead of the page — the messages a page may send, and the
// export snapshot.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  FRAME_CSP,
  MAX_SEND,
  buildSrcdoc,
  handleMessage,
  htmlSnapshot,
  listenToFrame,
  normalizePage,
} from '../../../lovia/web/static/js/html.js';

const page = '<script>evil()</script><p>hi</p>';

test('normalizePage needs html; the title is optional', () => {
  assert.deepEqual(normalizePage({ html: page, title: 'T', extra: 1 }), { html: page, title: 'T' });
  assert.equal(normalizePage({ html: page }).title, '');
  for (const bad of [null, {}, { html: '  ' }, { html: 3 }]) {
    assert.throws(() => normalizePage(bad), /no html/);
  }
});

test('buildSrcdoc: CSP, tokens and bootstrap come before the page', () => {
  const doc = buildSrcdoc(page, {
    theme: 'dark',
    tokens: { '--ink': '#eee', '--bad': 'red</style><script>x()</script>', '--empty': '' },
  });
  const csp = doc.indexOf(`content="${FRAME_CSP}"`);
  const boot = doc.indexOf('ui/notifications/size-changed');
  const body = doc.indexOf(page);
  assert.ok(csp > 0 && csp < boot && boot < body, 'policy, then bootstrap, then the page');
  assert.ok(doc.endsWith(page)); // the page itself is passed through verbatim
  assert.match(doc, /<html data-theme="dark">/);
  assert.match(doc, /--ink:#eee/);
  assert.doesNotMatch(doc.slice(0, body), /<\/style><script>x/); // a token can't close <style>
  assert.doesNotMatch(doc, /--empty/);
  assert.match(buildSrcdoc(page, { theme: 'sepia', tokens: {} }), /data-theme="light"/);
});

test('the frame CSP allows inline code and data images, nothing remote', () => {
  assert.match(FRAME_CSP, /default-src 'none'/);
  assert.match(FRAME_CSP, /script-src 'unsafe-inline'/);
  assert.match(FRAME_CSP, /form-action 'none'/); // default-src doesn't cover it
  assert.doesNotMatch(FRAME_CSP, /https?:|\*|connect-src/);
});

test('listenToFrame acts only on messages from its own frame, until stopped', () => {
  globalThis.window = /** @type {any} */ (new EventTarget());
  const post = (source, data) => {
    const e = new Event('message');
    Object.defineProperties(e, { source: { value: source }, data: { value: data } });
    window.dispatchEvent(e);
  };
  const frame = { contentWindow: {} };
  const sent = [];
  const stop = listenToFrame(frame, { resize: () => {}, send: (t) => sent.push(t) });
  post({}, say('from another window')); // same message, a different source
  post(null, say('from nowhere'));
  post(frame.contentWindow, say('from the frame'));
  stop();
  post(frame.contentWindow, say('after stop'));
  assert.deepEqual(sent, ['from the frame']);
});

/** Collect what handleMessage asks the parent to do. */
function act(data) {
  const did = [];
  handleMessage(data, { resize: (h) => did.push(['resize', h]), send: (t) => did.push(['send', t]) });
  return did;
}
const rpc = (method, params) => ({ jsonrpc: '2.0', method, params });
const say = (text, role = 'user') => rpc('ui/message', { role, content: { type: 'text', text } });

test('size-changed resizes to a finite, non-negative height', () => {
  assert.deepEqual(act(rpc('ui/notifications/size-changed', { height: 420 })), [['resize', 420]]);
  for (const height of [-1, 'tall', Infinity, undefined]) {
    assert.deepEqual(act(rpc('ui/notifications/size-changed', { height })), [], String(height));
  }
});

test('ui/message sends trimmed user text; anything else is dropped', () => {
  assert.deepEqual(act(say('  more please  ')), [['send', 'more please']]);
  assert.deepEqual(act(say('x'.repeat(MAX_SEND))), [['send', 'x'.repeat(MAX_SEND)]]);
  assert.deepEqual(act(say('x'.repeat(MAX_SEND + 1))), []); // too long: dropped, not cut
  assert.deepEqual(act(say('   ')), []);
  assert.deepEqual(act(say('hi', 'assistant')), []);
  assert.deepEqual(act(rpc('ui/message', { role: 'user', content: { type: 'image' } })), []);
});

test('non-JSON-RPC or unknown messages are ignored', () => {
  for (const data of [null, 'hi', { method: 'ui/message' }, rpc('tools/call', {}), rpc('ui/open-link', {})]) {
    assert.deepEqual(act(data), []);
  }
});

test('htmlSnapshot: a note and the escaped source, no frame', () => {
  const html = String(htmlSnapshot({ html: page, title: 'Explorer' }, /** @type {any} */ ({ lang: 'en' })));
  assert.match(html, /^<figure class="html"><figcaption>Explorer<\/figcaption><p>Interactive page/);
  assert.match(html, /<pre><code class="language-html">&lt;script&gt;evil\(\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<iframe|<script/);
});
