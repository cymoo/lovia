// Lightweight transient notifications.
//   toast('Chat exported');
//   toast('Couldn’t rename chat', { type: 'error' });

const DEFAULT_TIMEOUT_MS = 3200;
const MAX_TOASTS = 2;
/** @type {Map<HTMLElement, { key?: string, type: string, dismissed: boolean, timer: ReturnType<typeof setTimeout>, dismiss: () => void, drop: () => void }>} */
const entries = new Map();

function container() {
  let el = document.getElementById('toast-container');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast-container';
    el.className = 'lv-toast-container';
    el.setAttribute('aria-live', 'polite');
    document.body.appendChild(el);
  }
  // Promote to the top layer via the Popover API: a modal <dialog> paints
  // above any z-index, so a toast fired from inside Settings would otherwise
  // be buried under the backdrop. Later top-layer entries stack on top, and
  // re-showing after a dialog opens keeps toasts the newest entry.
  // (any-cast: the bundled TS lib predates the Popover API.)
  const pop = /** @type {any} */ (el);
  if ('popover' in el) {
    pop.popover = 'manual';
    try {
      if (document.querySelector('dialog[open]') && el.matches(':popover-open')) {
        pop.hidePopover(); // re-enter the top layer above the dialog
      }
      if (!el.matches(':popover-open')) pop.showPopover();
    } catch { /* popover unsupported edge — the fixed container still shows */ }
  }
  return el;
}

/**
 * Show at most two notifications. A key updates one in place; otherwise identical
 * text/type repeats coalesce. Ordinary notices never evict errors.
 * Click to dismiss; an update restarts `timeout` from the latest event.
 * @param {string} message
 * @param {{ type?: 'info' | 'success' | 'error', timeout?: number, key?: string }} [opts]
 * @returns {HTMLElement} The toast element.
 */
export function toast(message, { type = 'info', timeout = DEFAULT_TIMEOUT_MS, key } = {}) {
  const host = container();
  for (const [el, entry] of entries) {
    if (entry.dismissed) continue;
    const matches = key !== undefined ? entry.key === key
      : entry.key === undefined && entry.type === type && el.textContent === message;
    if (!matches) continue;
    clearTimeout(entry.timer);
    entry.type = type;
    el.className = `lv-toast lv-toast-${type} lv-show`;
    el.setAttribute('role', type === 'error' ? 'alert' : 'status');
    if (el.textContent !== message) el.textContent = message;
    entry.timer = setTimeout(entry.dismiss, timeout);
    return el;
  }

  const el = document.createElement('div');
  el.className = `lv-toast lv-toast-${type}`;
  el.setAttribute('role', type === 'error' ? 'alert' : 'status');
  el.textContent = message;
  if (entries.size >= MAX_TOASTS) {
    // Include exiting elements in the cap: bursts must not leave a third pill
    // fading out beneath two new ones. Eviction skips the exit animation.
    const victim = [...entries.values()].find((e) => e.dismissed)
      || [...entries.values()].find((e) => e.type !== 'error')
      || (type === 'error' ? entries.values().next().value : null);
    if (!victim) return el; // both slots carry errors; discard this ordinary notice
    victim.drop();
  }
  host.appendChild(el);
  requestAnimationFrame(() => { if (!entry.dismissed) el.classList.add('lv-show'); });

  const drop = () => {
    entry.dismissed = true;
    clearTimeout(entry.timer);
    entries.delete(el);
    el.remove();
  };
  const dismiss = () => {
    if (entry.dismissed) return;
    entry.dismissed = true;
    clearTimeout(entry.timer);
    el.classList.remove('lv-show');
    el.addEventListener('transitionend', drop, { once: true });
    entry.timer = setTimeout(drop, 400); // fallback if the transition never fires
  };

  const entry = { key, type, dismissed: false, timer: setTimeout(dismiss, timeout), dismiss, drop };
  entries.set(el, entry);
  el.addEventListener('click', dismiss);
  return el;
}
