// How long something took, or has been running: one format, one ticker.

/** `m:ss`, or `h:mm:ss` from an hour on. */
export function fmtElapsed(sec) {
  sec = Math.max(0, Math.floor(sec));
  const pad = (n) => String(n).padStart(2, '0');
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m}:${pad(sec % 60)}`;
  return `${Math.floor(m / 60)}:${pad(m % 60)}:${pad(sec % 60)}`;
}

// One shared 1s ticker keeps every running clock fresh without a re-render;
// it stops itself when none is left on the page.
let _ticker = null;
function syncTicker() {
  if (_ticker != null) return;
  _ticker = setInterval(() => {
    const els = document.querySelectorAll('.lv-elapsed[data-started]');
    if (!els.length) {
      clearInterval(_ticker);
      _ticker = null;
      return;
    }
    const now = Date.now() / 1000;
    for (const el of els) {
      el.textContent = fmtElapsed(now - Number(el.getAttribute('data-started')));
    }
  }, 1000);
}

/** A clock running since `startedAt` (Unix seconds). */
export function elapsedNode(startedAt) {
  const span = document.createElement('span');
  span.className = 'lv-elapsed';
  span.dataset.started = String(startedAt);
  span.textContent = fmtElapsed(Date.now() / 1000 - startedAt);
  syncTicker();
  return span;
}
