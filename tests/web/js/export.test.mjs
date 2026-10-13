// The HTML export (export.js): UI components draw as their renderer's
// snapshot, sanitized; anything else keeps the raw output.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { registerRenderer } from '../../../lovia/web/static/js/components.js';
import { buildExportDoc } from '../../../lovia/web/static/js/export.js';

// buildExportDoc round-trips its body through a detached element (for code
// highlighting); the stand-in sanitizer marks what passed through it.
globalThis.document = /** @type {any} */ ({
  createElement: () => ({ innerHTML: '', querySelectorAll: () => [] }),
});
globalThis.DOMPurify = /** @type {any} */ ({ sanitize: (s) => `[clean]${s}` });

registerRenderer('snap', { render() {}, snapshot: (props) => `<p>${props.n}</p>` });
registerRenderer('plain', { render() {} });

const call = (id, args) => ({
  role: 'assistant',
  content: '',
  tool_calls: [{ id, name: 'show_it', arguments: args }],
});
const result = (id, content) => ({ role: 'tool', tool_call_id: id, content });

test('a component with a snapshot draws as it, sanitized, in place of the raw output', async () => {
  const doc = await buildExportDoc({
    messages: [
      call('c1', '{"n": 1}'),
      result('c1', '{"lovia_ui": {"kind": "snap", "from_args": true}}'),
      call('c2', '{}'),
      result('c2', '{"lovia_ui": {"kind": "snap", "props": {"n": 2}}}'),
    ],
  });
  assert.match(doc, /<section class="msg msg-component">\[clean\]<p>1<\/p><\/section>/);
  assert.match(doc, /<section class="msg msg-component">\[clean\]<p>2<\/p><\/section>/);
  assert.doesNotMatch(doc, /tool-output/);
  assert.match(doc, /Tool: show_it/); // the call's arguments stay on record
});

test('no snapshot, an unknown kind, or plain output keeps the raw <pre>', async () => {
  const doc = await buildExportDoc({
    messages: [
      call('a', '{}'),
      result('a', '{"lovia_ui": {"kind": "plain", "props": 1}}'),
      call('b', '{}'),
      result('b', '{"lovia_ui": {"kind": "nobody", "props": 1}}'),
      call('c', '{}'),
      result('c', 'just text'),
    ],
  });
  assert.equal(doc.match(/class="tool-output"/g)?.length, 3);
  assert.doesNotMatch(doc, /msg-component/);
});

test('without DOMPurify, snapshots are skipped rather than embedded unsanitized', async () => {
  const purify = globalThis.DOMPurify;
  delete globalThis.DOMPurify;
  try {
    const doc = await buildExportDoc({
      messages: [call('c1', '{}'), result('c1', '{"lovia_ui": {"kind": "snap", "props": {"n": 3}}}')],
    });
    assert.doesNotMatch(doc, /msg-component/);
    assert.match(doc, /class="tool-output"/);
  } finally {
    globalThis.DOMPurify = purify;
  }
});
