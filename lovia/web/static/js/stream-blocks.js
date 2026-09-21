// stream-blocks.js — block-incremental parsing for the streaming render.
//
// Pure parse layer, no DOM: given the growing markdown of one reply, decides
// which leading blocks are final ("committed"), renders each of those exactly
// once, and re-parses only the unfinished tail per flush. The DOM side (which
// nodes to keep, which to replace) belongs to the caller.
//
// Correctness rests on two invariants, tested on every prefix of a corpus in
// tests/web/js/stream_blocks.test.mjs:
//
//   I1  normalize(text) starts with committed.join('') — re-checked on every
//       flush, so any non-append change to the text (a retry, a snapshot)
//       drops to the full-render path.
//   I2  a committed block ends with a blank line and, when it was committed,
//       marked had already lexed — in the same blockTokens() call — a block
//       starting right after it plus one more after that. Nothing appended
//       later can reopen it: every construct that spans a blank line is either
//       an open fence (always the last block, never committed), an HTML block
//       or a link-reference definition (both bail to full render), or a
//       list/indented-code continuation decided by the indentation of the next
//       non-blank line, which was already present.
//
// The stable count can therefore *drop* below what is committed (a setext
// underline, or the lone "-" of a list item still being typed, merges the two
// blocks after a committed one into one). That is bookkeeping, not
// instability: committed blocks are never un-committed. Un-committing would
// also have to roll back cross-block parser state (heading-id de-duplication),
// which is exactly what went wrong when it was tried.

/** marked's own line-ending normalization; block offsets live in this text. */
export function normalize(text) {
  return text.replace(/\r\n?/g, '\n');
}

/**
 * Block-only lex (no inline pass), with each `space` token folded into the
 * block before it so "ends with a blank line" is one uniform test: heading and
 * table tokens absorb their trailing blank lines, paragraph/list/code don't.
 *
 * `defs` is whether the lexer recorded any link-reference definition. That is
 * marked's own classification, so `[x]: …` inside a fence, or on the line
 * after a paragraph (which absorbs it as text), doesn't count.
 * @param {string} src
 * @returns {{ blocks: { type: string, raw: string }[], defs: boolean }}
 */
export function lexBlocks(src) {
  const lexer = new marked.Lexer(marked.defaults);
  const blocks = [];
  for (const t of lexer.blockTokens(src, [])) {
    if (t.type === 'space' && blocks.length) blocks[blocks.length - 1].raw += t.raw;
    else blocks.push({ type: t.type, raw: t.raw });
  }
  return { blocks, defs: Object.keys(lexer.tokens.links).length > 0 };
}

/**
 * Leading blocks that are final: ends with a blank line and at least two
 * blocks follow. Two, not one — the block right after may still be a partial
 * line whose meaning changes with the next character (`#` vs `#x`, `2` vs
 * `2.`, a lone `-` that becomes a setext underline).
 * @param {{ raw: string }[]} blocks
 */
export function stableCount(blocks) {
  for (let i = blocks.length - 3; i >= 0; i--) {
    if (blocks[i].raw.endsWith('\n\n')) return i + 1;
  }
  return 0;
}

/**
 * One reply's incremental parse state. Feed it the whole text so far on each
 * flush; it answers with HTML for the blocks that just became final and for
 * the tail — or a fallback, in which case the caller renders the text in full.
 *
 * `linkdef` and `html` fallbacks are permanent for the reply (append-only
 * text can't take them back); `mismatch` resets the stream, so the next flush
 * starts incremental again from the front.
 */
export class BlockStream {
  constructor() {
    this._reset();
    /** @type {string | null} */
    this.dead = null;
  }

  _reset() {
    /** @type {string[]} */
    this.committed = [];
    // Heading ids are de-duplicated per Parser instance; a persistent one sees
    // committed headings in document order (so ids match a whole-document
    // parse), and the tail renders through a *copy* of its state below.
    this.parser = new marked.Parser(marked.defaults);
  }

  /**
   * @param {string} rawText The reply so far.
   * @returns {{ committed: string[], tail: string } | { fallback: string }}
   *   `committed`: HTML of blocks that became final in this flush (append to
   *   the committed region); `tail`: HTML of everything after (replace the
   *   tail region). `fallback`: `linkdef` | `html` | `mismatch`.
   */
  flush(rawText) {
    if (this.dead) return { fallback: this.dead };
    const text = normalize(rawText);
    // I1: is the committed prefix still literally there?
    let off = 0;
    for (const raw of this.committed) {
      if (!text.startsWith(raw, off)) {
        this._reset();
        return { fallback: 'mismatch' };
      }
      off += raw.length;
    }
    const { blocks: tailBlocks, defs } = lexBlocks(text.slice(off));
    // marked resolves reference links against definitions collected over the
    // whole document; a tail-only lex can't see (or be seen by) the rest.
    if (defs) return { fallback: (this.dead = 'linkdef') };
    // A raw HTML block can wrap later blocks (`<div>` … paragraphs … `</div>`)
    // and only the whole-document parse nests them correctly.
    if (tailBlocks.some((b) => b.type === 'html')) return { fallback: (this.dead = 'html') };

    const blocks = this.committed.map((raw) => ({ type: '', raw })).concat(tailBlocks);
    const stable = Math.max(stableCount(blocks), this.committed.length);
    const committed = [];
    for (let i = this.committed.length; i < stable; i++) {
      this.committed.push(blocks[i].raw);
      committed.push(this.parser.parse(marked.lexer(blocks[i].raw)));
    }
    const tail = blocks.slice(stable).map((b) => b.raw).join('');
    const tailParser = new marked.Parser(marked.defaults);
    tailParser.slugger.seen = { ...this.parser.slugger.seen };
    return { committed, tail: tailParser.parse(marked.lexer(tail)) };
  }
}
