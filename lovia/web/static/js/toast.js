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
