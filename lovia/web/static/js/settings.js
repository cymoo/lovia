// settings.js — the gear dialog: language, theme, notifications.
//
// Small on purpose: three preferences, all client-side. Language applies on
// reload (the app reads every string at boot); theme applies live; the
// notification toggle asks the browser for permission the moment it's turned
// on, so denial surfaces immediately instead of silently at first use.
import { showDialog, setThemePref, themePref } from './ui.js';
import { t, langPref, setLangPref } from './i18n.js';
import { toast } from './toast.js';

const NOTIF_KEY = 'lovia-notify';

// True when the user opted in AND the browser granted permission.
export function notificationsEnabled() {
  return (
    localStorage.getItem(NOTIF_KEY) === '1' &&
    typeof Notification !== 'undefined' &&
    Notification.permission === 'granted'
  );
}

function field(labelText, control) {
  const row = document.createElement('label');
  row.className = 'settings-row';
  const span = document.createElement('span');
  span.className = 'settings-label';
  span.textContent = labelText;
  row.append(span, control);
  return row;
}

function select(options, value) {
  const el = document.createElement('select');
  el.className = 'dialog-input settings-select';
  for (const [val, label] of options) {
    const opt = document.createElement('option');
    opt.value = val;
    opt.textContent = label;
    el.appendChild(opt);
  }
  el.value = value;
  return el;
}

function openSettingsDialog() {
  const panel = document.createElement('div');
  panel.className = 'settings-panel';
  const h = document.createElement('h3');
  h.textContent = t('settings.title');
  panel.appendChild(h);

  // Language — applies on reload.
  const lang = select(
    [
      ['auto', t('settings.langAuto')],
      ['en', 'English'],
      ['zh', '中文'],
    ],
    langPref(),
  );
  lang.addEventListener('change', () => {
    setLangPref(lang.value);
    location.reload();
  });
  panel.appendChild(field(t('settings.language'), lang));

  // Theme — applies live.
  const theme = select(
    [
      ['system', t('settings.themeSystem')],
      ['light', t('settings.themeLight')],
      ['dark', t('settings.themeDark')],
    ],
    themePref(),
  );
  theme.addEventListener('change', () => setThemePref(theme.value));
  panel.appendChild(field(t('settings.theme'), theme));

  // Desktop notifications for finished background runs.
  if (typeof Notification !== 'undefined') {
    const notif = document.createElement('input');
    notif.type = 'checkbox';
    notif.checked = notificationsEnabled();
    notif.addEventListener('change', async () => {
      if (!notif.checked) {
        localStorage.setItem(NOTIF_KEY, '0');
        return;
      }
      const perm = await Notification.requestPermission();
      if (perm === 'granted') {
        localStorage.setItem(NOTIF_KEY, '1');
      } else {
        notif.checked = false;
        toast(t('settings.notifDenied'), { type: 'error' });
      }
    });
    const row = field(t('settings.notifications'), notif);
    row.classList.add('settings-check');
    panel.appendChild(row);
  }

  const note = document.createElement('p');
  note.className = 'settings-note';
  note.textContent = t('settings.reloadNote');
  panel.appendChild(note);

  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'btn btn-ghost';
  close.textContent = t('dialog.close');
  const dialog = showDialog({ body: panel, actions: close });
  close.addEventListener('click', () => dialog.close());
}

export function initSettings() {
  document
    .getElementById('settings-btn')
    ?.addEventListener('click', openSettingsDialog);
}
