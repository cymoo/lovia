// agent-chip.js — which agent the composer is on (store.agent), and the chip
// that shows it: beside the project and permission-mode chips, only when the
// server serves more than one agent.
//
// A new chat picks its agent here; it starts with the one its project's
// latest chat used (else the default, the first served), and the pick goes
// out with its first message. An existing chat keeps the agent it was created
// with: the chip only names it. A chat whose agent is gone runs on the
// default (the server's rule) — the page says so once when it opens.
import { store } from './store.js';
import { t } from './i18n.js';
import { toast } from './toast.js';
import { icon } from './icons.js';
import { scopeRoot } from './workspaces.js';

/** @type {HTMLButtonElement | null} */
let _chip = null;
/** @type {HTMLElement | null} */
let _menu = null;
/** Whether the person picked the new chat's agent (a project change then leaves it be). */
let _picked = false;

/** @param {string | null | undefined} name */
function info(name) {
  return store.agents.find((/** @type {any} */ a) => a.name === name) || null;
}

/** What the page calls an agent. @param {string | null | undefined} name */
export function agentLabel(name) {
  return info(name)?.label || name || '';
}

const defaultAgent = () => store.agents[0]?.name ?? null;

/** The agent the latest chat in `root` used, while it is still served. */
function lastIn(/** @type {string | null | undefined} */ root) {
  let best = null;
  for (const s of store.sessions) {
    if (root != null && s.workspace_root !== root) continue;
    if (!info(s.agent)) continue;
    if (!best || (s.updated_at ?? 0) > (best.updated_at ?? 0)) best = s;
  }
  return best?.agent ?? null;
}

/** @param {string | null} name */
function setAgent(name) {
  if (!name || name === store.agent) return render();
  store.agent = name;
  store.emit('agent-changed', name);
  render();
}

/** A new chat starts from its project's latest agent, unless one was picked. */
function resetDraft() {
  if (store.sessionId) return;
  _picked = false;
  setAgent(lastIn(scopeRoot()) ?? defaultAgent());
}

function render() {
  if (!_chip) return;
  _chip.hidden = store.agents.length < 2;
  const fixed = !!store.sessionId;
  _chip.classList.toggle('lv-chip-fixed', fixed);
  _chip.setAttribute('aria-disabled', String(fixed));
  const text = _chip.querySelector('.lv-model-chip-label');
  if (text) text.textContent = agentLabel(store.agent);
  _chip.title = t(fixed ? 'agent.chipFixed' : 'agent.chipTitle', { name: agentLabel(store.agent) });
}

function closeMenu() {
  if (!_menu) return;
  _menu.remove();
  _menu = null;
  _chip?.setAttribute('aria-expanded', 'false');
}

function openMenu() {
  if (_menu) return closeMenu();
  // An existing chat keeps its agent; a new one whose first message is out
  // already went with the one shown.
  if (!_chip || store.sessionId || store.streaming) return;
  const menu = document.createElement('div');
  menu.className = 'lv-model-chip-menu lv-mode-menu';
  menu.setAttribute('role', 'menu');
  const title = document.createElement('div');
  title.className = 'lv-model-chip-menu-title';
  title.textContent = t('agent.menuTitle');
  menu.appendChild(title);
  for (const a of store.agents) {
    const on = a.name === store.agent;
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'lv-model-chip-item lv-mode-item';
    item.setAttribute('role', 'menuitemradio');
    item.setAttribute('aria-checked', String(on));
    const text = document.createElement('span');
    text.className = 'lv-mode-item-text';
    const name = document.createElement('span');
    name.className = 'lv-model-chip-item-name';
    name.textContent = agentLabel(a.name);
    text.appendChild(name);
    if (a.description) {
      const hint = document.createElement('span');
      hint.className = 'lv-mode-item-hint';
      hint.textContent = a.description;
      text.appendChild(hint);
    }
    item.appendChild(text);
    if (on) item.insertAdjacentHTML('beforeend', icon('check', { size: 15 }));
    item.addEventListener('click', () => {
      closeMenu();
      _picked = true;
      setAgent(a.name);
      document.getElementById('prompt')?.focus();
    });
    menu.appendChild(item);
  }
  if (store.canConfigureModels) {
    const divider = document.createElement('div');
    divider.className = 'lv-model-chip-menu-divider';
    const manage = document.createElement('button');
    manage.type = 'button';
    manage.className = 'lv-model-chip-item lv-manage';
    manage.textContent = t('agent.manage');
    manage.addEventListener('click', async () => {
      closeMenu();
      const { openSettings } = await import('./settings.js');
      openSettings('agents');
    });
    menu.append(divider, manage);
  }
  _chip.insertAdjacentElement('afterend', menu);
  // Over its chip, pulled back in where the bar is narrower (a phone).
  const room = (menu.offsetParent?.clientWidth ?? Infinity) - menu.offsetWidth;
  menu.style.left = `${Math.max(0, Math.min(_chip.offsetLeft, room))}px`;
  menu.style.right = 'auto';
  _menu = menu;
  _chip.setAttribute('aria-expanded', 'true');
  /** @type {HTMLElement | null} */ (menu.querySelector('[aria-checked="true"]'))?.focus();
}

/**
 * An opened chat runs on the agent it was created with — or, that one gone,
 * on the default, which the page says.
 * @param {string | null | undefined} name
 */
function onChatAgent(name) {
  if (info(name)) return setAgent(/** @type {string} */ (name));
  const fallback = defaultAgent();
  if (name && fallback) toast(t('agent.gone', { name, fallback: agentLabel(fallback) }));
  setAgent(fallback);
}

/**
 * Boot: the composer's agent, and its chip. Called from main.js once the
 * agent list loaded — on every server: an embedder may serve several too.
 */
export function initAgentChip() {
  const tools = document.querySelector('.lv-composer-tools');
  if (tools && !_chip) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.id = 'agent-chip';
    chip.className = 'lv-model-chip lv-mode-chip';
    chip.dataset.part = 'agent-chip';
    chip.setAttribute('aria-haspopup', 'menu');
    chip.setAttribute('aria-expanded', 'false');
    const mark = document.createElement('span');
    mark.className = 'lv-mode-chip-icon';
    mark.setAttribute('aria-hidden', 'true');
    mark.innerHTML = icon('bot', { size: 14 });
    const text = document.createElement('span');
    text.className = 'lv-model-chip-label';
    chip.append(mark, text);
    chip.addEventListener('click', (e) => {
      e.stopPropagation();
      openMenu();
    });
    tools.appendChild(chip);
    _chip = chip;
    document.addEventListener('click', (e) => {
      if (_menu && !_menu.contains(/** @type {Node} */ (e.target))) closeMenu();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && _menu) {
        e.stopPropagation();
        closeMenu();
        _chip?.focus();
      }
    });
  }
  store.on('sync-agent', onChatAgent);
  store.on('session-id', (/** @type {string | null} */ id) => {
    closeMenu();
    if (!id) resetDraft();
    else render();
  });
  store.on('new-chat', () => {
    closeMenu();
    resetDraft();
  });
  // The draft's project changed, or the chats that say what it last used
  // arrived: an agent nobody picked follows.
  const follow = () => {
    if (!store.sessionId && !_picked) resetDraft();
  };
  store.on('workspace-scope-changed', follow);
  store.on('sessions-loaded', follow);
  // Settings added, renamed or deleted one.
  store.on('agents-loaded', () => {
    if (!info(store.agent)) setAgent(defaultAgent());
    else render();
  });
  render();
}
