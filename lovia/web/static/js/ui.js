// UI utilities: theme, sidebar toggle, dialogs.
import { store } from './store.js';
import { icon } from './icons.js';

const darkToggle = document.getElementById('dark-toggle');
const themeIcon = darkToggle?.querySelector('.theme-icon');

// ---- Theme -------------------------------------------------------------
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  store.theme = theme;
  localStorage.setItem('lovia-theme', theme);
  // Show the destination: a sun in dark mode (click → light), a moon in light.
  if (themeIcon) themeIcon.innerHTML = icon(theme === 'dark' ? 'sun' : 'moon', { size: 17 });
}

export function initTheme() {
  applyTheme(store.theme);
  darkToggle?.addEventListener('click', () => {
    applyTheme(store.theme === 'dark' ? 'light' : 'dark');
  });
}

// ---- Sidebar toggle (mobile) -------------------------------------------
const overlay = document.getElementById('sidebar-overlay');
let sidebarOpen = false;
const sidebarCollapseBtn = document.getElementById('sidebar-collapse');
const sidebarExpandBtn = document.getElementById('sidebar-expand');

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

function applySidebarCollapsed(collapsed) {
  if (window.matchMedia('(max-width: 720px)').matches) {
    document.body.classList.remove('sidebar-collapsed');
    return;
  }
  store.sidebarCollapsed = collapsed;
  document.body.classList.toggle('sidebar-collapsed', collapsed);
  localStorage.setItem('lovia-sidebar-collapsed', collapsed ? '1' : '0');
}

export function initSidebarToggle() {
  applySidebarCollapsed(store.sidebarCollapsed);
  document.getElementById('sidebar-toggle')?.addEventListener('click', openSidebar);
  sidebarCollapseBtn?.addEventListener('click', () => applySidebarCollapsed(true));
  sidebarExpandBtn?.addEventListener('click', () => applySidebarCollapsed(false));
  overlay?.addEventListener('click', closeSidebar);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && sidebarOpen) closeSidebar();
  });
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => applySidebarCollapsed(store.sidebarCollapsed), 120);
  });
}

// ---- Native Dialog -----------------------------------------------------
export function showDialog({ body, actions, onClose } = {}) {
  const existing = document.querySelector('dialog.custom-dialog');
  if (existing) existing.close();

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

  dialog.showModal();
  return dialog;
}

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
    cancelBtn.textContent = 'Cancel';
    cancelBtn.addEventListener('click', () => dialog.close('cancel'));

    const okBtn = document.createElement('button');
    okBtn.className = 'btn btn-primary';
    okBtn.textContent = 'OK';
    okBtn.addEventListener('click', () => dialog.close('ok'));

    actions.appendChild(cancelBtn);
    actions.appendChild(okBtn);
    const dialog = showDialog({ body, actions, onClose: (val) => resolve(val === 'ok') });
  });
}

export function promptDialog(message, defaultValue = '') {
  return new Promise((resolve) => {
    const body = document.createElement('div');
    const label = document.createElement('p');
    label.style.margin = '0 0 8px';
    label.textContent = message;
    body.appendChild(label);

    const input = document.createElement('input');
    input.type = 'text';
    input.value = defaultValue;
    input.className = 'dialog-input';
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') dialog.close(input.value.trim());
    });
    body.appendChild(input);

    const actions = document.createElement('div');
    actions.style.display = 'flex';
    actions.style.gap = '8px';
    actions.style.justifyContent = 'flex-end';

    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn btn-ghost';
    cancelBtn.textContent = 'Cancel';
    // Cancel closes with '' (same as Esc) — dialog.close(null) would coerce
    // the returnValue to the string "null", indistinguishable from typing it.
    cancelBtn.addEventListener('click', () => dialog.close(''));

    const okBtn = document.createElement('button');
    okBtn.className = 'btn btn-primary';
    okBtn.textContent = 'Save';
    okBtn.addEventListener('click', () => dialog.close(input.value.trim()));

    actions.appendChild(cancelBtn);
    actions.appendChild(okBtn);
    const dialog = showDialog({ body, actions, onClose: (val) => resolve(val || null) });
    setTimeout(() => input.focus(), 100);
  });
}

// ---- Clipboard ---------------------------------------------------------
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
