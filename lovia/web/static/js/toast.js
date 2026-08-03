// Lightweight transient notifications.
//   toast('Chat exported');
//   toast('Couldn’t rename chat', { type: 'error' });

const DEFAULT_TIMEOUT_MS = 3200;

function container() {
  let el = document.getElementById('toast-container');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast-container';
    el.className = 'toast-container';
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
 * Show a transient notification. Click to dismiss; auto-dismisses after `timeout`.
 * @param {string} message
 * @param {{ type?: 'info' | 'success' | 'error', timeout?: number }} [opts]
 * @returns {HTMLElement} The toast element.
 */
export function toast(message, { type = 'info', timeout = DEFAULT_TIMEOUT_MS } = {}) {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.setAttribute('role', type === 'error' ? 'alert' : 'status');
  el.textContent = message;
  container().appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));

  let dismissed = false;
  const dismiss = () => {
    if (dismissed) return;
    dismissed = true;
    el.classList.remove('show');
    const drop = () => el.remove();
    el.addEventListener('transitionend', drop, { once: true });
    setTimeout(drop, 400); // fallback if the transition never fires
  };

  const timer = setTimeout(dismiss, timeout);
  el.addEventListener('click', () => { clearTimeout(timer); dismiss(); });
  return el;
}
