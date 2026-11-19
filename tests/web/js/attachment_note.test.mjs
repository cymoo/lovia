// splitAttachmentNote: a stored user turn back into its text and attachments —
// uploads (absolute paths, served from the chat's folder by name) apart from
// workspace files (relative paths), in the note's current and legacy forms.

import assert from 'node:assert/strict';
import test from 'node:test';

import { splitAttachmentNote } from '../../../lovia/web/static/js/attachment-note.js';

const TAIL = 'Read with your file tools when relevant.]';

test('one path a line: uploads by name, workspace files by path', () => {
  const text =
    'look at these\n\n[Attached:\n' +
    '/home/me/.lovia/attachments/3fa1/cat.png\n' +
    'C:\\Users\\Li, Wei\\.lovia\\attachments\\3fa1\\report.pdf\n' +
    'docs/plan.md\n' +
    TAIL;
  assert.deepEqual(splitAttachmentNote(text), {
    text: 'look at these',
    attachments: [
      { name: 'cat.png', kind: 'image' },
      { name: 'report.pdf', kind: 'file' },
      { path: 'docs/plan.md', name: 'plan.md', kind: 'file' },
    ],
  });
});

test('a note with no text of its own', () => {
  const out = splitAttachmentNote(`[Attached:\n/a/b/x.webp\n${TAIL}`);
  assert.deepEqual(out, { text: '', attachments: [{ name: 'x.webp', kind: 'image' }] });
});

test('a note without the tools line (no workspace)', () => {
  const out = splitAttachmentNote('see\n\n[Attached:\n/a/b/cat.png\n/a/b/dog.jpg]');
  assert.deepEqual(out, {
    text: 'see',
    attachments: [
      { name: 'cat.png', kind: 'image' },
      { name: 'dog.jpg', kind: 'image' },
    ],
  });
});

test('the legacy note still reads: workspace uploads/ paths', () => {
  const text = `hi\n\n[Attached in the workspace: uploads/a.png, uploads/b c.txt. ${TAIL}`;
  assert.deepEqual(splitAttachmentNote(text).attachments, [
    { path: 'uploads/a.png', name: 'a.png', kind: 'image' },
    { path: 'uploads/b c.txt', name: 'b c.txt', kind: 'file' },
  ]);
});

test('text without a note is left alone', () => {
  for (const text of ['plain', '[Attached: inline mention] not at the end', '']) {
    assert.deepEqual(splitAttachmentNote(text), { text, attachments: [] });
  }
  assert.deepEqual(splitAttachmentNote(null), { text: '', attachments: [] });
});
