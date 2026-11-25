// A log on screen: blocks of lines that stay out of layout while off screen
// (content-visibility, styles.css), the newest lines only, and the bottom kept
// in view unless the reader scrolled away. What the text says is log-text.js's.
import { t } from './i18n.js';
import { icon } from './icons.js';
import { createLogText } from './log-text.js';

// Blocks grow to this many lines and are dropped whole from the top.
const BLOCK_LINES = 100;
// Closer to the end than this counts as following it.
const NEAR_END = 24;

/**
 * @param {{ maxLines?: number }} [opts]
 */
export function createLogView({ maxLines = 5000 } = {}) {
  const text = createLogText();
  /** @type {{ el: HTMLElement, lines: number }[]} */
  let blocks = [];
  let total = 0;
  let omitted = 0; // characters dropped before the first one shown
  let trimmed = false; // lines dropped here, to keep to maxLines
  let following = true;

  const el = document.createElement('div');
  el.className = 'lv-log';
  const note = document.createElement('div');
  note.className = 'lv-log-note';
  note.hidden = true;
  const tail = document.createElement('div');
  tail.className = 'lv-log-block';
  const jump = document.createElement('button');
  jump.type = 'button';
  jump.className = 'lv-btn lv-btn-sm lv-log-jump';
  jump.hidden = true;
  jump.innerHTML = icon('arrow-down', { size: 13 });
  jump.append(t('log.jump'));
  el.append(note, tail, jump);

  /** What scrolls: the viewer's body, or the reader's while it shows there. */
  const scroller = () => /** @type {HTMLElement | null} */ (el.parentElement);

  function toEnd() {
    const box = scroller();
    if (box) box.scrollTop = box.scrollHeight;
  }
  function onScroll() {
    const box = scroller();
    if (!box) return;
    following = box.scrollHeight - box.scrollTop - box.clientHeight < NEAR_END;
    jump.hidden = following;
  }
  jump.addEventListener('click', () => {
    following = true;
    jump.hidden = true;
    toEnd();
  });

  function sayOmitted() {
    const parts = [];
    if (omitted) parts.push(t('log.omitted', { n: omitted.toLocaleString() }));
    if (trimmed) parts.push(t('log.trimmed', { n: maxLines.toLocaleString() }));
    note.textContent = parts.join(' · ');
    note.hidden = !parts.length;
  }

  function addLines(lines) {
    for (let i = 0; i < lines.length; ) {
      let block = blocks[blocks.length - 1];
      if (!block || block.lines >= BLOCK_LINES) {
        const div = document.createElement('div');
        div.className = 'lv-log-block';
        div.append('');
        tail.before(div);
        block = { el: div, lines: 0 };
        blocks.push(block);
      }
      const part = lines.slice(i, i + BLOCK_LINES - block.lines);
      // Each line ends with its newline, so an empty one has a height too.
      /** @type {Text} */ (block.el.firstChild).appendData(`${part.join('\n')}\n`);
      block.lines += part.length;
      block.el.style.setProperty('--lv-log-lines', String(block.lines));
      i += part.length;
      total += part.length;
    }
    while (blocks.length > 1 && total - blocks[0].lines >= maxLines) {
      total -= blocks[0].lines;
      blocks[0].el.remove();
      blocks.shift();
      trimmed = true;
    }
  }

  return {
    el,
    /** @param {string} chunk */
    append(chunk) {
      const out = text.feed(chunk);
      addLines(out.lines);
      tail.textContent = out.tail;
      sayOmitted();
      if (following) toEnd();
    },
    /** `n` characters are missing here: at the start, or between two pages. */
    gap(n) {
      if (!total && !tail.textContent) {
        omitted += n;
        sayOmitted();
        return;
      }
      const out = text.feed('\n');
      addLines(out.lines);
      tail.textContent = '';
      const line = document.createElement('div');
      line.className = 'lv-log-note';
      line.textContent = t('log.gap', { n: n.toLocaleString() });
      tail.before(line);
      blocks.push({ el: line, lines: 0 });
    },
    /** Attach to where `el` was put: call again after moving it. */
    watch() {
      scroller()?.addEventListener('scroll', onScroll, { passive: true });
      if (following) toEnd();
    },
    unwatch() {
      scroller()?.removeEventListener('scroll', onScroll);
    },
    /** What is on screen, as text. */
    text() {
      return (
        blocks.map((b) => b.el.textContent ?? '').join('') + (tail.textContent ?? '')
      );
    },
  };
}
