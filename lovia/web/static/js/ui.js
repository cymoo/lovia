// UI utilities: theme, sidebar toggle, dialogs.
import { t } from './i18n.js';
import { store } from './store.js';
import { icon } from './icons.js';

// ---- Theme -------------------------------------------------------------
// Three-way preference: 'system' (default — follows the OS live), 'light',
// 'dark'. The stored value is the PREFERENCE; store.theme always holds the
// RESOLVED light/dark the page is actually showing. Switching lives in the
// settings dialog — an app-level concern, so no topbar button.
const THEME_KEY = 'lovia-theme';
const _dark = window.matchMedia('(prefers-color-scheme: dark)');

/** @returns {'system' | 'light' | 'dark'} The stored theme preference. */
export function themePref() {
  const saved = localStorage.getItem(THEME_KEY);
  return saved === 'light' || saved === 'dark' ? saved : 'system';
}

function resolveTheme(pref) {
  return pref === 'light' || pref === 'dark'
    ? pref
    : _dark.matches
      ? 'dark'
      : 'light';
}

// The static <meta name="theme-color" media=…> pair in the template follows
// the OS. Once JS owns the theme (saved preference may oppose the OS), browser
// chrome must follow the *resolved* theme: swap the pair for one meta fed from
// the live --bg token, so it also stays right if the palette ever changes.
function syncThemeColor() {
  document
    .querySelectorAll('meta[name="theme-color"][media]')
    .forEach((m) => m.remove());
  let meta = /** @type {HTMLMetaElement | null} */ (
    document.querySelector('meta[name="theme-color"]')
  );
  if (!meta) {
    meta = document.createElement('meta');
    meta.name = 'theme-color';
    document.head.appendChild(meta);
  }
  const bg = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim();
  if (bg) meta.content = bg;
}

function applyResolved(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  store.theme = theme;
  syncThemeColor();
}

/**
 * Persist a theme preference and apply the resolved light/dark theme live.
 * @param {'system' | 'light' | 'dark' | string} pref
 */
export function setThemePref(pref) {
  localStorage.setItem(THEME_KEY, pref);
  applyResolved(resolveTheme(pref));
}

export function initTheme() {
  applyResolved(resolveTheme(themePref()));
  // 'system' follows OS changes live; explicit picks ignore them.
  _dark.addEventListener?.('change', () => {
    if (themePref() === 'system') applyResolved(resolveTheme('system'));
  });
}

// ---- Sidebar collapse / mobile drawer ------------------------------------
// Two layers decide desktop visibility: the user's persisted preference, and
// an ephemeral "auto" claim the Files panel makes when the viewport is too
// tight for three columns (see files.js). The auto layer is never persisted —
// a reload re-derives it — and an explicit expand clears it: the user's word
// beats ours.
const overlay = document.getElementById('sidebar-overlay');
let sidebarOpen = false;
let sidebarAutoCollapsed = false;
const sidebarCollapseBtn = document.getElementById('sidebar-collapse');
const sidebarExpandBtn = document.getElementById('sidebar-expand');

const isPhone = () => window.matchMedia('(max-width: 720px)').matches;

function openSidebar() {
  sidebarOpen = true;
  document.getElementById('sidebar')?.classList.add('open');
  overlay?.classList.add('open');
}

function closeSidebar() {
  sidebarOpen = false;
  document.getElementById('sidebar')?.classList.remove('open');
  overlay?.classList.remove('open');
}

function syncSidebar() {
  document.body.classList.toggle(
    'sidebar-collapsed',
    !isPhone() && (store.sidebarCollapsed || sidebarAutoCollapsed),
  );
}

// A user gesture: persisted, and expanding overrides any auto claim.
function setSidebarCollapsed(collapsed) {
  store.sidebarCollapsed = collapsed;
  localStorage.setItem('lovia-sidebar-collapsed', collapsed ? '1' : '0');
  if (!collapsed) sidebarAutoCollapsed = false;
  syncSidebar();
}

/**
 * The Files panel claims the sidebar's space while open on a tight viewport
 * and releases it on close.
 * @param {boolean} claimed
 */
export function setSidebarAutoCollapsed(claimed) {
  sidebarAutoCollapsed = claimed;
  syncSidebar();
}

export function initSidebarToggle() {
  syncSidebar();
  document.getElementById('sidebar-toggle')?.addEventListener('click', openSidebar);
  sidebarCollapseBtn?.addEventListener('click', () => setSidebarCollapsed(true));
  sidebarExpandBtn?.addEventListener('click', () => setSidebarCollapsed(false));
  overlay?.addEventListener('click', closeSidebar);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && sidebarOpen) closeSidebar();
  });
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(syncSidebar, 120);
  });
}

// ---- Native Dialog -----------------------------------------------------
/**
 * @typedef {object} DialogOptions
 * @property {string | HTMLElement} [body] Dialog body — a string is inserted as
 *   plain text, an element as-is (use an element when you need markup).
 * @property {string | HTMLElement} [actions] Footer action row, same rule as `body`.
 * @property {(returnValue: string) => void} [onClose] Called with the dialog's
 *   `returnValue` after it closes.
 * @property {boolean} [stack] Layer on top of an already-open dialog (a confirm
 *   inside an editor) instead of replacing it.
 */

/**
 * Open a modal `<dialog>`. Backdrop click and Esc both dismiss it.
 * @param {DialogOptions} [opts]
 * @returns {HTMLDialogElement} The live dialog; call `.close(returnValue)` to dismiss.
 */
export function showDialog({ body, actions, onClose, stack = false } = {}) {
  if (!stack) {
    const existing = /** @type {HTMLDialogElement | null} */ (
      document.querySelector('dialog.custom-dialog')
    );
    if (existing) existing.close();
  }

  const dialog = document.createElement('dialog');
  dialog.className = 'custom-dialog';

  const content = document.createElement('div');
  content.className = 'dialog-content';

  const bodyEl = document.createElement('div');
  bodyEl.className = 'dialog-body';
  // A string is treated as plain text, never HTML — callers pass an element
  // when they need markup.
  if (typeof body === 'string') bodyEl.textContent = body;
  else if (body instanceof HTMLElement) bodyEl.appendChild(body);
  content.appendChild(bodyEl);

  if (actions) {
    const actionsEl = document.createElement('div');
    actionsEl.className = 'dialog-actions';
    if (typeof actions === 'string') actionsEl.innerHTML = actions;
    else if (actions instanceof HTMLElement) actionsEl.appendChild(actions);
    content.appendChild(actionsEl);
  }

  dialog.appendChild(content);
  document.body.appendChild(dialog);

  dialog.addEventListener('close', () => {
    dialog.remove();
    if (onClose) onClose(dialog.returnValue);
  });

  // Click on the backdrop dismisses the dialog (Esc already does). The dialog
  // box wraps its content exactly — padding lives on .dialog-content, the
  // <dialog> has none — so a pointer event whose target is the <dialog> itself
  // landed on the backdrop. Track the press origin so a text selection that
  // begins inside an input and releases on the backdrop isn't read as a
  // dismiss. Closing with no returnValue reads as cancel for prompt/confirm.
  let pressedOnBackdrop = false;
  dialog.addEventListener('pointerdown', (e) => {
    pressedOnBackdrop = e.target === dialog;
  });
  dialog.addEventListener('click', (e) => {
    if (e.target === dialog && pressedOnBackdrop) dialog.close();
  });

  dialog.showModal();
  return dialog;
}

/**
 * A modal OK/Cancel confirm.
 * @param {string} message
 * @returns {Promise<boolean>} Resolves true on OK, false on Cancel/Esc/backdrop.
 */
export function confirmDialog(message) {
  return new Promise((resolve) => {
    const body = document.createElement('p');
    body.style.margin = '0';
    body.textContent = message;
    const actions = document.createElement('div');
    actions.style.display = 'flex';
    actions.style.gap = '8px';
    actions.style.justifyContent = 'flex-end';

    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn btn-ghost';
    cancelBtn.textContent = t('dialog.cancel');
    cancelBtn.addEventListener('click', () => dialog.close('cancel'));

    const okBtn = document.createElement('button');
    okBtn.className = 'btn btn-primary';
    okBtn.textContent = t('dialog.ok');
    okBtn.addEventListener('click', () => dialog.close('ok'));

    actions.appendChild(cancelBtn);
    actions.appendChild(okBtn);
    const dialog = showDialog({
      body,
      actions,
      stack: true, // confirms may layer on an open editor (schedules, memory)
      onClose: (val) => resolve(val === 'ok'),
    });
  });
}

/**
 * A modal single-line text prompt.
 *
 * Resolves to the entered string on Save/Enter (possibly ''), or null on
 * Cancel/Esc — an empty submission is a real answer ("clear it"), so the two
 * must stay distinguishable. The sentinel returnValue carries the ok/cancel
 * bit; the value itself rides in `submitted` (returnValue coerces everything
 * to string, which would fold null and "null" together).
 * @param {string} message Prompt shown above the input.
 * @param {string} [defaultValue] Pre-filled input value.
 * @returns {Promise<string | null>} Entered text, or null if cancelled.
 */
export function promptDialog(message, defaultValue = '') {
  return new Promise((resolve) => {
    const body = document.createElement('div');
    const label = document.createElement('p');
    label.style.margin = '0 0 8px';
    label.textContent = message;
    body.appendChild(label);

    let submitted = null;
    const submit = () => {
      submitted = input.value.trim();
      dialog.close('ok');
    };

    const input = document.createElement('input');
    input.type = 'text';
    input.value = defaultValue;
    input.className = 'dialog-input';
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') submit();
    });
    body.appendChild(input);

    const actions = document.createElement('div');
    actions.style.display = 'flex';
    actions.style.gap = '8px';
    actions.style.justifyContent = 'flex-end';

    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn btn-ghost';
    cancelBtn.textContent = t('dialog.cancel');
    cancelBtn.addEventListener('click', () => dialog.close(''));

    const okBtn = document.createElement('button');
    okBtn.className = 'btn btn-primary';
    okBtn.textContent = t('dialog.save');
    okBtn.addEventListener('click', submit);

    actions.appendChild(cancelBtn);
    actions.appendChild(okBtn);
    const dialog = showDialog({
      body,
      actions,
      onClose: (val) => resolve(val === 'ok' ? submitted : null),
    });
    setTimeout(() => input.focus(), 100);
  });
}

// ---- Clipboard ---------------------------------------------------------
/**
 * Copy text to the clipboard, falling back to a hidden textarea + execCommand
 * where the async Clipboard API is unavailable or blocked.
 * @param {string} text
 * @returns {Promise<boolean>} True once copied.
 */
export async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    return true;
  }
}

// ---- Image lightbox ----------------------------------------------------
/**
 * A focused, in-app viewer for chat-attached images: click a thumbnail to see
 * it large instead of leaving for a new tab. Built on `<dialog>` so Esc, the
 * backdrop, and focus handling come for free.
 * @param {string} src Image URL.
 * @param {{ alt?: string, downloadHref?: string | null }} [opts]
 */
export function openImageLightbox(src, { alt = '', downloadHref = null } = {}) {
  const dialog = document.createElement('dialog');
  // The app targets <dialog>-capable browsers (see showDialog above); if
  // showModal is somehow unavailable, degrade to opening the image directly.
  if (typeof dialog.showModal !== 'function') {
    window.open(src, '_blank', 'noopener');
    return;
  }
  dialog.className = 'lightbox';

  // A relative frame sizes to the image and anchors the action bar to its
  // corner, instead of relying on the dialog's UA positioning.
  const frame = document.createElement('div');
  frame.className = 'lightbox-frame';

  const bar = document.createElement('div');
  bar.className = 'lightbox-bar';
  if (downloadHref) {
    const dl = document.createElement('a');
    dl.className = 'lightbox-btn';
    dl.href = downloadHref;
    dl.setAttribute('download', '');
    dl.title = t('lightbox.download');
    dl.setAttribute('aria-label', t('lightbox.download'));
    dl.innerHTML = icon('download', { size: 18 });
    bar.appendChild(dl);
  }
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'lightbox-btn';
  close.title = t('lightbox.close');
  close.setAttribute('aria-label', t('lightbox.close'));
  close.innerHTML = icon('x', { size: 18 });
  close.addEventListener('click', () => dialog.close());
  bar.appendChild(close);

  const img = document.createElement('img');
  img.className = 'lightbox-img';
  img.src = src;
  img.alt = alt;

  frame.appendChild(bar);
  frame.appendChild(img);
  dialog.appendChild(frame);

  // A click on the backdrop (outside the image frame) closes.
  dialog.addEventListener('click', (e) => {
    if (e.target === dialog) dialog.close();
  });
  dialog.addEventListener('close', () => dialog.remove());
  document.body.appendChild(dialog);
  dialog.showModal();
}
