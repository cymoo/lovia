// @ts-check
// Feeds the server's run_deps= from the page. Loaded by
// examples/34_web_run_deps.py through ChatUI(scripts=["extension.js"]).

const { lovia } = window;

// UI state: the knowledge base picked by the message box. Read when a message
// is sent, so each run gets whatever is picked at that moment. The browser
// controls this value — the server only accepts knowledge bases it knows.
const kb = /** @type {HTMLSelectElement | null} */ (document.getElementById('acme-kb'));
if (kb) {
  kb.value = localStorage.getItem('acme-kb') || 'docs';
  kb.addEventListener('change', () => localStorage.setItem('acme-kb', kb.value));
  lovia.provideContext('kb', () => kb.value);
}

// Identity is NOT client context: this demo's auth= dependency reads a cookie
// (a real app's reads its session). Switching user sets it and reloads.
const user = /** @type {HTMLSelectElement | null} */ (document.getElementById('acme-user'));
if (user) {
  const current = document.cookie.match(/(?:^|; )acme_user=([^;]*)/);
  user.value = current ? decodeURIComponent(current[1]) : 'guest';
  user.addEventListener('change', () => {
    document.cookie = `acme_user=${encodeURIComponent(user.value)}; path=/; SameSite=Lax`;
    location.reload();
  });
}
