// UI utilities: theme, sidebar toggle, dialogs.
import { store } from './store.js';

export const $ = (s, root = document) => root.querySelector(s);

const darkToggle = document.getElementById('dark-toggle');
const themeIcon = darkToggle?.querySelector('.theme-icon');

// ---- Theme -------------------------------------------------------------
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  store.theme = theme;
  localStorage.setItem('lovia-theme', theme);
  if (themeIcon) themeIcon.textContent = theme === 'dark' ? '◑' : '◐';
}

export function initTheme() {
  applyTheme(store.theme);
  darkToggle?.addEventListener('click', () => {
    applyTheme(store.theme === 'dark' ? 'light' : 'dark');
  });
}

// ---- Sidebar toggle (mobile) -------------------------------------------
let sidebarOpen = false;

function openSidebar() {
  sidebarOpen = true;
  document.getElementById('sidebar')?.classList.add('open');
  document.getElementById('sidebar-overlay')?.classList.add('open');
}

function closeSidebar() {
  sidebarOpen = false;
  document.getElementById('sidebar')?.classList.remove('open');
  document.getElementById('sidebar-overlay')?.classList.remove('open');
}

export function initSidebarToggle() {
  document.getElementById('sidebar-toggle')?.addEventListener('click', openSidebar);
  overlay?.addEventListener('click', closeSidebar);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && sidebarOpen) closeSidebar();
  });
}

const overlay = document.getElementById('sidebar-overlay');

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
  if (typeof body === 'string') bodyEl.innerHTML = body;
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
    const body = `<p>${message}</p>`;
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
    body.innerHTML = `<p style="margin:0 0 8px">${message}</p>`;

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
    cancelBtn.addEventListener('click', () => dialog.close(null));

    const okBtn = document.createElement('button');
    okBtn.className = 'btn btn-primary';
    okBtn.textContent = 'Save';
    okBtn.addEventListener('click', () => dialog.close(input.value.trim()));

    actions.appendChild(cancelBtn);
    actions.appendChild(okBtn);
    const dialog = showDialog({ body, actions, onClose: (val) => resolve(val && val !== 'null' ? val : null) });
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
