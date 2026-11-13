// A completion menu on the composer's textarea — the `/` skill menu
// (skill-menu.js) and the `@` file menu (file-menu.js) are two of these.
// Focus stays in the textarea (the list is its `aria-activedescendant`);
// ↑/↓ move, Enter or Tab picks, Esc closes until the caret leaves the token.
// A spec says when the menu opens, what it lists and what picking leaves in
// the text; the opening/closing and the stale-answer rules live here.
import { store } from './store.js';

/**
 * @template T
 * @typedef {object} CompleterSpec
 * @property {string} id the menu's element id; options are `${id}-${i}`
 * @property {string} className added to `lv-popup-menu lv-complete-menu`
 * @property {() => string} label the heading, and the list's accessible name
 * @property {(text: string, caret: number) => string | null} query what is
 *   being typed when the caret sits in a token the menu answers, else null
 * @property {() => Promise<{ items: T[], note?: string | null } | null>} load
 *   the list for this opening (once per opening; null or a throw: no menu),
 *   and a line to show under it
 * @property {(items: T[], query: string) => T[]} match
 * @property {(item: T) => Node[]} row an option's contents
 * @property {(text: string, caret: number, item: T) => { text: string, caret: number }} insert
 */

/**
 * Wire a completion menu to the composer's textarea.
 * @template T
 * @param {HTMLTextAreaElement} input
 * @param {CompleterSpec<T>} spec
 */
export function initCompleter(input, spec) {
  // The composer's visible box, which the form pads.
  const anchor = /** @type {HTMLElement} */ (input.closest('.lv-composer-inner') ?? input);
  /** @type {HTMLElement | null} */
  let menu = null;
  /** @type {{ items: T[], note?: string | null } | null} null: not loaded for this opening */
  let list = null;
  /** @type {T[]} */
  let shown = [];
  let active = 0;
  let dismissed = false; // Esc: shut until this opening ends
  let opening = 0; // bumped by close(): an answer loaded before it is stale
  let loadingFor = -1; // the opening a load is out for: one per opening

  input.setAttribute('aria-autocomplete', 'list');

  function hide() {
    if (!menu) return;
    menu.remove();
    menu = null;
    input.removeAttribute('aria-controls');
    input.removeAttribute('aria-activedescendant');
    input.setAttribute('aria-expanded', 'false');
  }

  // This opening is over — hidden, the list let go of (the next opening
  // loads it again), and an Esc forgotten with it. Every way out ends here:
  // the caret leaving the token, a send (which clears the box without an
  // input event), blur.
  function close() {
    list = null;
    dismissed = false;
    opening++;
    hide();
  }

  async function update() {
    const query = spec.query(input.value, input.selectionEnd ?? 0);
    if (query === null || input.selectionStart !== input.selectionEnd) {
      close();
      return;
    }
    if (dismissed) return;
    if (list === null) {
      if (loadingFor === opening) return; // on its way: it updates when in
      const ticket = (loadingFor = opening);
      let got = null;
      try {
        got = await spec.load();
      } catch {
        // No menu beats a broken one: the text still sends as typed.
      }
      // Closed meanwhile: dropped. A later opening loads for itself.
      if (ticket !== opening) return;
      loadingFor = -1; // a failed load is retried on the next keystroke
      if (!got) return;
      list = got;
      // The caret may have moved on while the list was on its way.
      return update();
    }
    const matches = spec.match(list.items, query);
    if (!matches.length) return hide();
    const same = matches.length === shown.length && matches.every((s, i) => s === shown[i]);
    if (!same) active = 0;
    shown = matches;
    render();
  }

  function render() {
    if (!menu) {
      menu = document.createElement('div');
      menu.id = spec.id;
      menu.className = `lv-popup-menu lv-complete-menu ${spec.className}`;
      menu.setAttribute('role', 'listbox');
      menu.setAttribute('aria-label', spec.label());
      menu.popover = 'manual';
      // A press on the list must not take focus from the textarea.
      menu.addEventListener('pointerdown', (e) => e.preventDefault());
      document.body.append(menu);
      menu.showPopover();
      input.setAttribute('aria-controls', menu.id);
      input.setAttribute('aria-expanded', 'true');
    }
    const head = document.createElement('div');
    head.className = 'lv-popup-menu-heading';
    head.textContent = spec.label();
    const rows = shown.map((item, i) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.tabIndex = -1;
      b.id = `${spec.id}-${i}`;
      b.className = 'lv-popup-menu-item lv-complete-menu-item';
      b.setAttribute('role', 'option');
      b.setAttribute('aria-selected', String(i === active));
      b.append(...spec.row(item));
      b.addEventListener('click', () => pick(item));
      return b;
    });
    const parts = [head, ...rows];
    if (list?.note) {
      const note = document.createElement('div');
      note.className = 'lv-complete-menu-note';
      note.textContent = list.note;
      parts.push(note);
    }
    menu.replaceChildren(...parts);
    input.setAttribute('aria-activedescendant', `${spec.id}-${active}`);
    place();
    rows[active]?.scrollIntoView({ block: 'nearest' });
  }

  // Over the composer, lined up with its left edge; under it when there is
  // no room above.
  function place() {
    if (!menu) return;
    const r = anchor.getBoundingClientRect();
    const { width, height } = menu.getBoundingClientRect();
    let y = r.top - height - 6;
    if (y < 8) y = r.bottom + 6;
    menu.style.left = `${Math.max(8, Math.min(r.left, innerWidth - width - 8))}px`;
    menu.style.top = `${y}px`;
  }

  /** @param {T} item */
  function pick(item) {
    const next = spec.insert(input.value, input.selectionEnd ?? 0, item);
    close();
    input.value = next.text;
    input.setSelectionRange(next.caret, next.caret);
    input.focus();
    // The composer's own listeners: autoresize, Send enabled, the saved draft.
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }

  // Capturing, so it runs before the composer's Enter-to-send handler on
  // the same textarea, which it then keeps from seeing a key it claimed.
  input.addEventListener(
    'keydown',
    (e) => {
      if (!menu || e.isComposing) return;
      const n = shown.length;
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        active = (active + (e.key === 'ArrowDown' ? 1 : n - 1)) % n;
        render();
      } else if (e.key === 'Enter' || e.key === 'Tab') {
        if (e.shiftKey || e.altKey || e.metaKey || e.ctrlKey) return;
        pick(shown[active]);
      } else if (e.key === 'Escape') {
        close();
        dismissed = true; // after close(), which forgets it
      } else return;
      e.preventDefault();
      e.stopImmediatePropagation();
    },
    true,
  );
  input.addEventListener('input', () => void update());
  // Caret moves that type nothing: a click, ←/→, Home/End.
  input.addEventListener('click', () => void update());
  input.addEventListener('keyup', (e) => {
    if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) void update();
  });
  input.addEventListener('blur', close);
  input.form?.addEventListener('submit', close);
  window.addEventListener('resize', place);
  for (const event of ['session-switched', 'new-chat', 'agent-changed', 'workspace-scope-changed']) {
    store.on(event, close);
  }
}
