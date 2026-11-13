// The composer's `/` menu: typing `/` at the very start of a message offers
// the chat's skills, filtered as you type; picking one puts `/name ` there.
// The text is sent as typed — the Skills prompt tells the model what a
// leading `/name` means, so the server expands nothing.
import { api } from './api.js';
import { store } from './store.js';
import { t } from './i18n.js';
import { draftWorkspaceRoot } from './workspaces.js';

/** @typedef {{ name: string, description: string }} SkillInfo */

// What the Skills prompt rule recognizes: a skill name (letters, digits,
// `-`/`_` between them) right after a `/` that opens the message.
const SLASH = /^\/([A-Za-z0-9_-]*)$/;

/**
 * The name being typed when the caret sits in a `/name` that opens the
 * message, else null. Only from the very first character — the rule the
 * model follows ignores a `/name` anywhere else.
 * @param {string} text
 * @param {number} caret
 * @returns {string | null}
 */
export function slashQuery(text, caret) {
  const m = SLASH.exec(text.slice(0, caret));
  return m ? m[1] : null;
}

/**
 * The skills matching `query`, case-insensitively: names that start with it
 * first, then those that only contain it — each group in catalog order.
 * @param {SkillInfo[]} skills
 * @param {string} query
 * @returns {SkillInfo[]}
 */
export function matchSkills(skills, query) {
  const q = query.toLowerCase();
  const starts = [];
  const contains = [];
  for (const s of skills) {
    const name = s.name.toLowerCase();
    if (name.startsWith(q)) starts.push(s);
    else if (name.includes(q)) contains.push(s);
  }
  return [...starts, ...contains];
}

/**
 * `text` with the `/name` token that opens it replaced by `/<name> `, and
 * where the caret goes: after that space.
 * @param {string} text
 * @param {string} name
 * @returns {{ text: string, caret: number }}
 */
export function insertSkill(text, name) {
  const end = /^\/[A-Za-z0-9_-]*/.exec(text)?.[0].length ?? 0;
  const rest = text.slice(end).replace(/^[ \t]/, '');
  const head = `/${name} `;
  return { text: head + rest, caret: head.length };
}

/**
 * Wire the menu to the composer's textarea. Focus stays in the textarea
 * (the list is its `aria-activedescendant`); ↑/↓ move, Enter or Tab picks,
 * Esc closes until the caret leaves the `/name`.
 * @param {HTMLTextAreaElement | null} input
 */
export function initSkillMenu(input) {
  if (!input) return;
  // The composer's visible box, which the form pads.
  const anchor = /** @type {HTMLElement} */ (input.closest('.lv-composer-inner') ?? input);
  /** @type {HTMLElement | null} */
  let menu = null;
  /** @type {SkillInfo[] | null} null: not fetched for this opening */
  let skills = null;
  /** @type {SkillInfo[]} */
  let shown = [];
  let active = 0;
  let dismissed = false;
  let loading = false; // one fetch at a time; typing meanwhile waits for it
  let opening = 0; // bumped by close(): an answer fetched before it is stale

  input.setAttribute('aria-autocomplete', 'list');

  function hide() {
    if (!menu) return;
    menu.remove();
    menu = null;
    input.removeAttribute('aria-controls');
    input.removeAttribute('aria-activedescendant');
    input.setAttribute('aria-expanded', 'false');
  }

  // Hidden, and the list forgotten: the next opening fetches it afresh (a
  // skill added on disk, another chat's or agent's list).
  function close() {
    skills = null;
    opening++;
    hide();
  }

  async function update() {
    const query = slashQuery(input.value, input.selectionEnd ?? 0);
    if (query === null || input.selectionStart !== input.selectionEnd) {
      dismissed = false;
      close();
      return;
    }
    if (dismissed) return;
    if (skills === null) {
      if (loading) return;
      loading = true;
      const ticket = opening;
      const where = store.sessionId
        ? { session: store.sessionId }
        : { agent: store.agent || undefined, root: draftWorkspaceRoot() || undefined };
      let got = null;
      try {
        got = await api.listSkills(where);
      } catch {
        // No menu beats a broken one: the text still sends as typed.
      }
      loading = false;
      // Closed meanwhile: whatever is open now fetches for itself.
      if (ticket !== opening) return update();
      if (!got) return;
      skills = got;
      // The caret may have moved on while the list was on its way.
      return update();
    }
    const matches = matchSkills(skills, query);
    if (!matches.length) return hide();
    const same = matches.length === shown.length && matches.every((s, i) => s === shown[i]);
    if (!same) active = 0;
    shown = matches;
    render();
  }

  function render() {
    if (!menu) {
      menu = document.createElement('div');
      menu.id = 'lv-skill-menu';
      menu.className = 'lv-popup-menu lv-skill-menu';
      menu.setAttribute('role', 'listbox');
      menu.setAttribute('aria-label', t('composer.skills'));
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
    head.textContent = t('composer.skills');
    const rows = shown.map((s, i) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.tabIndex = -1;
      b.id = `lv-skill-${i}`;
      b.className = 'lv-popup-menu-item lv-skill-menu-item';
      b.setAttribute('role', 'option');
      b.setAttribute('aria-selected', String(i === active));
      const name = document.createElement('span');
      name.className = 'lv-popup-menu-label';
      name.textContent = `/${s.name}`;
      const desc = document.createElement('span');
      desc.className = 'lv-skill-menu-desc';
      desc.textContent = s.description;
      b.append(name, desc);
      b.addEventListener('click', () => pick(s));
      return b;
    });
    menu.replaceChildren(head, ...rows);
    input.setAttribute('aria-activedescendant', `lv-skill-${active}`);
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

  /** @param {SkillInfo} s */
  function pick(s) {
    const next = insertSkill(input.value, s.name);
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
        dismissed = true;
        close();
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
