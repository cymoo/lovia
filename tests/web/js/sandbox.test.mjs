// The proxy document sandbox.js wraps untrusted HTML in: its frame-src guard
// comes before the frame it guards, the untrusted document can't break out
// of its srcdoc attribute, and only the inner frame's messages are relayed.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { proxyDoc } from '../../../lovia/web/static/js/sandbox.js';

const evil = '"></iframe><script>parent.steal()</script><p title=\'x\'>&amp;</p>';

const unescape = (s) =>
  s.replace(/&(amp|lt|gt|quot|#39);/g, (_, e) => ({ amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'" })[e]);

test('the frame-src guard precedes the frame it guards, and is the only policy', () => {
  const doc = proxyDoc('<p>hi</p>', { title: 'T' });
  const policies = [...doc.matchAll(/http-equiv="Content-Security-Policy" content="([^"]*)"/g)];
  assert.deepEqual(policies.map((m) => m[1]), ["frame-src 'none'"]);
  assert.ok(doc.indexOf('Content-Security-Policy') < doc.indexOf('<iframe'));
  assert.match(doc, /<iframe sandbox="allow-scripts" referrerpolicy="no-referrer"/);
});

test('the untrusted document stays inside its srcdoc attribute, verbatim', () => {
  const doc = proxyDoc(evil, { title: '"><b>t' });
  const frames = doc.match(/<iframe[^>]*>/g);
  assert.equal(frames.length, 1); // nothing in `evil` opened a second tag
  const srcdoc = /srcdoc="([^"]*)"/.exec(frames[0])[1];
  assert.equal(unescape(srcdoc), evil);
  assert.doesNotMatch(doc, /<script>parent\.steal/);
  assert.match(frames[0], /title="&quot;&gt;&lt;b&gt;t"/);
});

test('the color scheme is the proxy\'s only when given', () => {
  assert.match(proxyDoc('', { title: '', colorScheme: 'dark' }), /color-scheme:dark;/);
  assert.doesNotMatch(proxyDoc('', { title: '' }), /color-scheme/);
});

test('the relay forwards only what the inner frame posts', () => {
  const doc = proxyDoc('', { title: '' });
  const script = /<script>(.*)<\/script>/.exec(doc)[1];
  const inner = {};
  const sent = [];
  let onMessage = null;
  new Function('document', 'addEventListener', 'parent', script)(
    { querySelector: () => ({ contentWindow: inner }) },
    (type, fn) => {
      if (type === 'message') onMessage = fn;
    },
    { postMessage: (data, origin) => sent.push([data, origin]) },
  );
  onMessage({ source: inner, data: { jsonrpc: '2.0', method: 'ui/message' } });
  onMessage({ source: {}, data: 'from elsewhere' });
  assert.deepEqual(sent, [[{ jsonrpc: '2.0', method: 'ui/message' }, '*']]);
});
