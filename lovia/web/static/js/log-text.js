// A command's raw output as lines to read: escape sequences dropped, a line
// redrawn with "\r" (a progress bar) shown as its last state. Output comes in
// chunks that may end anywhere — inside an escape, between "\r" and "\n", in
// the middle of a bar — so the state carries over. Pure: the DOM part is
// log-view.js.

// CSI (colours, cursor moves), OSC (titles, hyperlinks), charset picks, and
// the two-character escapes.
const ESCAPES =
  /\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()*+][0-9A-Za-z]|[ -/]*[0-Z\\^-~])/g;
// One that has not ended yet, at the end of a chunk.
const OPEN_ESCAPE = /\x1b(?:\[[0-?]*[ -/]*|\][^\x07\x1b]*\x1b?|[()*+]|[ -/]*)?$/;
const CONTROLS = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g;

// How long an escape is waited for before it is taken as text.
const MAX_ESCAPE = 1024;

/** `text` without escape sequences and other control characters (keeps \t \n \r). */
export function plainText(text) {
  return text.replace(ESCAPES, '').replace(CONTROLS, '');
}

/**
 * @param {{ maxLine?: number }} [opts] A longer line is cut there, saying by
 *   how much.
 */
export function createLogText({ maxLine = 2000 } = {}) {
  let open = ''; // an escape cut by the end of a chunk
  let seg = ''; // the line so far, or what follows its last "\r"
  let over = 0; // characters of it past maxLine
  let last = ''; // what the line showed before that "\r"
  let returned = false; // a "\r" whose next character starts over

  const shown = () => (over ? `${seg} … (+${over})` : seg);
  const reset = () => {
    seg = '';
    over = 0;
  };
  const add = (text) => {
    if (!text) return;
    if (returned) {
      if (seg) last = shown();
      reset();
      returned = false;
    }
    const room = maxLine - seg.length;
    seg += text.slice(0, room);
    over += Math.max(0, text.length - room);
  };

  return {
    /**
     * @param {string} chunk
     * @returns {{ lines: string[], tail: string }} The lines it completed,
     *   and the unfinished one as it looks now.
     */
    feed(chunk) {
      let text = open + chunk;
      open = '';
      const m = OPEN_ESCAPE.exec(text);
      if (m && m[0].length <= MAX_ESCAPE) {
        open = m[0];
        text = text.slice(0, m.index);
      }
      text = plainText(text);
      const lines = [];
      let from = 0;
      for (let i = 0; i < text.length; i++) {
        const c = text[i];
        if (c !== '\n' && c !== '\r') continue;
        add(text.slice(from, i));
        from = i + 1;
        if (c === '\r') {
          returned = true;
          continue;
        }
        lines.push(shown() || last);
        reset();
        last = '';
        returned = false;
      }
      add(text.slice(from));
      return { lines, tail: shown() || last };
    },
  };
}
