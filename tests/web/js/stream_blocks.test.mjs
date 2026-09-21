// Equivalence corpus for the block-incremental parser (stream-blocks.js).
//
// For every prefix of every document — the way a reply streams in — the
// incremental result must equal a whole-document parse of the same prefix:
//
//   join(committed HTML so far) + tail HTML === marked.parse(prefix)
//
// Then again over random flush histories (gaps of 1–40 chars: throttled and
// skipped flushes commit at different points than a per-character stream).
// A green run cannot hide behind fallbacks: documents without raw HTML or
// link definitions must run incrementally on every flush.
//
// The repo's own markdown is part of the corpus on purpose. If an edit to
// one of those files turns this red, a construct the edge cases don't cover
// has appeared — pin it as an edge case, then fix (or explain) the parser.
//
//   node --test tests/web/js/        (also runs in CI's typecheck-web job)
//   EVERY_PREFIX=1 node --test ...   every prefix of the repo docs too
//                                    (default: every prefix of the edge cases,
//                                    every 7th of the long docs)

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const STATIC = join(ROOT, 'lovia', 'web', 'static');

// The UI loads marked as a CDN-style global; the vendored UMD build is the
// same one the browser gets (tests/web/test_vendor_integrity.py pins it).
globalThis.marked = createRequire(import.meta.url)(join(STATIC, 'vendor', 'marked.min.js'));
// chat.js's options, plus mangle off: marked v4 obfuscates mailto autolinks
// with a per-parse *random* mix of decimal and hex entities, so two parses of
// "typescript@7.0.2" (an email, while its `code span` is still unterminated)
// give different HTML strings for the same DOM. Rendering is unaffected; this
// comparison is on strings.
marked.setOptions({ gfm: true, breaks: false, mangle: false });

const { BlockStream, normalize } = await import(join(STATIC, 'js', 'stream-blocks.js'));

// ---- corpus ---------------------------------------------------------------

const edge = {
  setext: 'para one\n\nTitle\n===\n\nafter\n\nmore\n',
  hashContinue: 'text\n#x continues\n\n# real\n\nend\n\nend2\n',
  listNumber: '1. one\n2. two\n\n3. three\n\npara\n\nz\n',
  looseNested: '- a\n\n  - b\n\n    ```py\n    x\n    ```\n\n- c\n\npara\n\nq\n',
  lazy: '> quote\nlazy continuation\n\n> second\n\npara\n\nx\n',
  tableLate: '| a | b |\n|---|---|\n| 1 | 2 |\n\ntext\n\n| c |\n|---|\n\nend\n\ne2\n',
  fences: '```\ncode\n\nstill code\n```\n\n~~~js\nx()\n~~~\n\np\n\nq\n',
  headingsDup: '# Same\n\n# Same\n\n## Same\n\np\n\nq\n',
  jumpLink: '[跳到安装](#安装)\n\n## 安装\n\ntext\n\n## 安装\n\nagain\n\nend\n',
  cjk: '第一句话，没有空行。'.repeat(200) + '\n\n然后是第二段。\n\n第三段。\n',
  indentedAfterList: '- item\n\n      indented code?\n\npara\n\nx\n',
  hr: 'a\n\n---\n\nb\n\n***\n\nc\n\nd\n',
  crlf: 'a\r\n\r\nb\r\n\r\n- c\r\n- d\r\n\r\ne\r\n\r\nf\r\n',
  inlineOpen: 'text with **bold that\n\nnever closes and `code\n\nend\n\nx\n',
  // Mid-stream, `npx -p typescript@7.0.2` is an open code span whose content
  // autolinks as an email until the closing backtick arrives.
  emailInOpenCode: 'run `npx -p typescript@7.0.2 tsc` now\n\nnext\n\nend\n',
  // Looks like a link definition, isn't one to marked: inside a fence, and on
  // the line after a paragraph (absorbed as text). Neither may fall back.
  defInCode: '```\n[x]: http://e.com\n```\n\npara\n\nq\n',
  defAfterParagraph: 'para\n[z]: http://g.com\n\nsee [z]\n\nend\n',
  // A leading blank line is a `space` token with nothing to fold into.
  leadingBlank: '\n\nfirst\n\nsecond\n\nthird\n',
  // Model house style: heading, "intro:" line, list with no blank line before
  // it, fence, closing paragraph. Each next item's lone "-" momentarily reads
  // as a setext underline for the paragraph above — the stable count drops
  // below what is committed, and must not un-commit anything.
  llmStyle: Array.from({ length: 8 }, (_, i) =>
    `## Step ${i}\n\nThere are three things to check here:\n- **First**: look at \`x_${i}\`\n- **Second**: run it\n- **Third**: verify\n\n\`\`\`bash\nrun --step ${i}\n\`\`\`\n\nThat completes step ${i}.\n\n`).join(''),
};
// By design these fall back to the full render for the rest of the reply.
const fallbackOnly = {
  html: '<div>\n\na\n\nb\n\n</div>\n\np\n\nq\n',
  linkdef: 'see [x]\n\n[x]: http://e.com\n\npara\n\nq\n',
};
const longDocs = Object.fromEntries(
  ['README.md', 'README-zh.md', 'AGENTS.md', 'docs/architecture.md'].map((f) => [
    f,
    readFileSync(join(ROOT, f), 'utf8'),
  ]),
);

// ---- driver ---------------------------------------------------------------

/** Stream `doc` through a BlockStream at the given cut points. */
function drive(doc, cuts) {
  const stream = new BlockStream();
  let committed = '';
  const stats = { flushes: 0, incremental: 0, fallbacks: {} };
  for (const i of cuts) {
    const text = doc.slice(0, i);
    const r = stream.flush(text);
    stats.flushes++;
    let html;
    if ('fallback' in r) {
      stats.fallbacks[r.fallback] = (stats.fallbacks[r.fallback] || 0) + 1;
      committed = '';
      html = marked.parse(normalize(text));
    } else {
      stats.incremental++;
      committed += r.committed.join('');
      html = committed + r.tail;
    }
    assert.equal(html, marked.parse(normalize(text)), `prefix ${i} of ${doc.length}`);
  }
  return stats;
}

const everyPrefix = (doc) => Array.from({ length: doc.length }, (_, i) => i + 1);
const strided = (doc, n) => {
  const cuts = [];
  for (let i = 1; i < doc.length; i += n) cuts.push(i);
  cuts.push(doc.length);
  return cuts;
};
// Deterministic PRNG so a failure reproduces.
function randomCuts(doc, seed) {
  let s = seed >>> 0;
  const rnd = () => ((s = (s * 1664525 + 1013904223) >>> 0) / 2 ** 32);
  const cuts = [];
  for (let i = 0; i < doc.length; ) {
    i = Math.min(doc.length, i + 1 + Math.floor(rnd() * 40));
    cuts.push(i);
  }
  return cuts;
}

for (const [name, doc] of Object.entries(edge)) {
  test(`edge: ${name} — every prefix, fully incremental`, () => {
    const s = drive(doc, everyPrefix(doc));
    assert.equal(s.incremental, s.flushes, JSON.stringify(s.fallbacks));
  });
}

for (const [name, doc] of Object.entries(fallbackOnly)) {
  test(`fallback: ${name} — equivalent via full render`, () => {
    const s = drive(doc, everyPrefix(doc));
    assert.ok(s.fallbacks[name] > 0, `expected ${name} fallbacks, got ${JSON.stringify(s.fallbacks)}`);
  });
}

for (const [name, doc] of Object.entries(longDocs)) {
  const cuts = process.env.EVERY_PREFIX ? everyPrefix(doc) : strided(doc, 7);
  test(`doc: ${name} — ${cuts.length} prefixes, fully incremental`, () => {
    const s = drive(doc, cuts);
    assert.equal(s.incremental, s.flushes, JSON.stringify(s.fallbacks));
  });
}

test('random flush histories — 10 seeds over every document', () => {
  for (const [name, doc] of Object.entries({ ...edge, ...longDocs })) {
    for (let seed = 1; seed <= 10; seed++) {
      const s = drive(doc, randomCuts(doc, seed));
      assert.equal(s.incremental, s.flushes, `${name} seed ${seed}: ${JSON.stringify(s.fallbacks)}`);
    }
  }
});

test('mismatch resets the stream, then it resumes incrementally', () => {
  const stream = new BlockStream();
  const a = 'one\n\ntwo\n\nthree\n\nfour';
  const r1 = stream.flush(a);
  assert.ok('committed' in r1 && r1.committed.length > 0);
  const r2 = stream.flush('ONE\n\ntwo\n\nthree\n\nfour'); // a retry replaced the prefix
  assert.deepEqual(r2, { fallback: 'mismatch' });
  const r3 = stream.flush('ONE\n\ntwo\n\nthree\n\nfour\n\nfive');
  assert.ok('committed' in r3 && r3.committed[0].includes('ONE'));
});

test('html and linkdef fallbacks are permanent for the reply', () => {
  const stream = new BlockStream();
  assert.deepEqual(stream.flush('<div>\n\nx'), { fallback: 'html' });
  assert.deepEqual(stream.flush('<div>\n\nx\n\ny\n\nz\n\n'), { fallback: 'html' });
});
