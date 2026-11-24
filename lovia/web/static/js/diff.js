// Line diffs — the two sides of an edit as one change, and the line
// highlighting the Files panel's git diff shares with it.

// Past this many cells the table costs more than the reading is worth: the
// middle shows as replaced whole, which is still true.
const MAX_CELLS = 1_000_000;

/**
 * Text as diff lines. A closing newline ends the last line rather than
 * starting an empty one.
 * @param {string} text
 * @param {boolean} closed Whether to read it that way.
 */
function toLines(text, closed) {
  if (text === '') return [];
  return (closed && text.endsWith('\n') ? text.slice(0, -1) : text).split('\n');
}

/**
 * Every line of both texts in reading order, removals before the additions
 * that replace them.
 * @param {string} before
 * @param {string} after
 * @returns {{ sign: ' ' | '+' | '-', text: string }[]}
 */
export function lineDiff(before, after) {
  // A newline only one side ends with is part of the change, so it stays visible.
  const closed = [before, after].every((s) => s === '' || s.endsWith('\n'));
  const a = toLines(before, closed);
  const b = toLines(after, closed);
  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head++;
  let tail = 0;
  while (
    tail < a.length - head &&
    tail < b.length - head &&
    a[a.length - 1 - tail] === b[b.length - 1 - tail]
  ) tail++;
  const n = a.length - head - tail;
  const m = b.length - head - tail;

  // lcs[i * w + j]: the longest common run of lines from a[head + i], b[head + j] on.
  const w = m + 1;
  const lcs = n * m <= MAX_CELLS ? new Uint32Array((n + 1) * w) : null;
  if (lcs) {
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        lcs[i * w + j] = a[head + i] === b[head + j]
          ? lcs[(i + 1) * w + j + 1] + 1
          : Math.max(lcs[(i + 1) * w + j], lcs[i * w + j + 1]);
      }
    }
  }

  /** @type {{ sign: ' ' | '+' | '-', text: string }[]} */
  const out = a.slice(0, head).map((text) => ({ sign: ' ', text }));
  let i = 0;
  let j = 0;
  while (i < n || j < m) {
    const text = a[head + i];
    if (lcs && i < n && j < m && text === b[head + j]) {
      out.push({ sign: ' ', text });
      i++;
      j++;
    } else if (i < n && (!lcs || j === m || lcs[(i + 1) * w + j] >= lcs[i * w + j + 1])) {
      out.push({ sign: '-', text });
      i++;
    } else {
      out.push({ sign: '+', text: b[head + j] });
      j++;
    }
  }
  for (const text of a.slice(a.length - tail)) out.push({ sign: ' ', text });
  return out;
}

/**
 * The highlight.js language for a file's diff, or null to leave it plain.
 * @param {string} path
 * @param {number} size Characters to highlight.
 */
export function diffLanguage(path, size) {
  const e = (path.split('.').pop() || '').toLowerCase();
  if (typeof hljs === 'undefined' || size > 200_000 || !/^[a-z0-9]+$/.test(e)) return null;
  return hljs.getLanguage(e) ? e : null;
}

/**
 * One diff line's source, without its sign. Each line is highlighted on its
 * own: a construct spanning lines (a block comment, a template string) loses
 * its colour past the first, but a diff holds no whole file to read one from.
 * @param {string} text
 * @param {string | null} lang From diffLanguage.
 */
export function diffSource(text, lang) {
  const src = document.createElement('span');
  if (lang && hljs) {
    // hljs escapes the source it returns: safe as markup.
    src.innerHTML = hljs.highlight(text, { language: lang, ignoreIllegals: true }).value;
  } else {
    src.textContent = text;
  }
  return src;
}

/**
 * The change from `before` to `after` as rows, without line numbers: an edit's
 * arguments don't say where in the file it lands.
 * @param {string} before
 * @param {string} after
 * @param {string} path The file's path, for its language.
 */
export function renderLineDiff(before, after, path) {
  const box = document.createElement('div');
  const lang = diffLanguage(path, before.length + after.length);
  for (const { sign, text } of lineDiff(before, after)) {
    const row = document.createElement('div');
    row.className = 'lv-diff-line';
    if (sign !== ' ') row.classList.add(sign === '+' ? 'lv-add' : 'lv-del');
    const mark = document.createElement('span');
    mark.className = 'lv-diff-sign';
    mark.textContent = sign === '-' ? '−' : sign;
    const code = diffSource(text, lang);
    code.className = 'lv-diff-code';
    row.append(mark, code);
    box.appendChild(row);
  }
  return box;
}
