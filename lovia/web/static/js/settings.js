// settings.js — the gear dialog: language, theme, message text size, Enter
// behavior, desktop notifications, and a completion sound.
//
// All preferences are client-side (localStorage). Language applies on reload
// (the app reads every string at boot); theme and text size apply live; the
// notification toggle asks the browser for permission the moment it's turned
// on, so denial surfaces immediately instead of silently at first use.
import { showDialog, setThemePref, themePref } from './ui.js';
import { t, langPref, setLangPref } from './i18n.js';
import { toast } from './toast.js';

const NOTIF_KEY = 'lovia-notify';
const SOUND_KEY = 'lovia-sound';
const ENTER_KEY = 'lovia-enter-send';
const TEXT_SIZE_KEY = 'lovia-text-size';

// ---- Message text size ----------------------------------------------------
// Scales the transcript's prose via the --chat-font-scale CSS var (styles.css
// multiplies the message font-sizes by it). 'md' is the default → var unset.
const TEXT_SCALES = { sm: '0.92', md: '1', lg: '1.14' };

function textSizePref() {
  const v = localStorage.getItem(TEXT_SIZE_KEY);
  return v === 'sm' || v === 'lg' ? v : 'md';
}

/**
 * Apply the saved (or given) message text size to the document. Exported so boot
 * can set it before the first paint, alongside the theme.
 * @param {'sm' | 'md' | 'lg' | string} [size]
 */
export function applyTextSize(size = textSizePref()) {
  const root = document.documentElement;
  if (size === 'md') root.style.removeProperty('--chat-font-scale');
  else root.style.setProperty('--chat-font-scale', TEXT_SCALES[size] || TEXT_SCALES.md);
}

// ---- Enter-to-send --------------------------------------------------------
/**
 * True (default): Enter sends, Shift+Enter inserts a newline. False: Enter
 * inserts a newline and ⌘/Ctrl+Enter sends — friendlier for multi-line drafts.
 * @returns {boolean}
 */
export function enterToSend() {
  return localStorage.getItem(ENTER_KEY) !== '0';
}

// ---- Desktop notifications ------------------------------------------------
/** @returns {boolean} True when the user opted in AND the browser granted permission. */
export function notificationsEnabled() {
  return (
    localStorage.getItem(NOTIF_KEY) === '1' &&
    typeof Notification !== 'undefined' &&
    Notification.permission === 'granted'
  );
}

// ---- Completion sound -----------------------------------------------------
/** @returns {boolean} Whether the completion sound is enabled (off by default). */
export function soundEnabled() {
  return localStorage.getItem(SOUND_KEY) === '1';
}

let _audioCtx = null;
/**
 * Play the completion chime, if enabled. A short synthesized sound — no bundled
 * asset — that degrades silently where Web Audio is unavailable or blocked.
 */
export function playCompletionSound() {
  if (!soundEnabled()) return;
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    _audioCtx = _audioCtx || new Ctx();
    const ctx = _audioCtx;
    if (ctx.state === 'suspended') ctx.resume(); // unlock after autoplay-gating
    const now = ctx.currentTime;
    // Two soft ascending notes — a gentle "done" without a bundled file.
    for (const [freq, at] of [[880, 0], [1174.66, 0.11]]) {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, now + at);
      gain.gain.exponentialRampToValueAtTime(0.11, now + at + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + at + 0.18);
      osc.connect(gain).connect(ctx.destination);
      osc.start(now + at);
      osc.stop(now + at + 0.2);
    }
  } catch { /* audio blocked/unavailable — stay silent */ }
}

// ---- Dialog ---------------------------------------------------------------
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

  // Message text size — applies live.
  const textSize = select(
    [
      ['sm', t('settings.textSmall')],
      ['md', t('settings.textDefault')],
      ['lg', t('settings.textLarge')],
    ],
    textSizePref(),
  );
  textSize.addEventListener('change', () => {
    localStorage.setItem(TEXT_SIZE_KEY, textSize.value);
    applyTextSize(textSize.value);
  });
  panel.appendChild(field(t('settings.textSize'), textSize));

  // Enter behavior — read live by the composer, so no reload needed.
  const enter = select(
    [
      ['send', t('settings.enterSend')],
      ['newline', t('settings.enterNewline')],
    ],
    enterToSend() ? 'send' : 'newline',
  );
  enter.addEventListener('change', () => {
    localStorage.setItem(ENTER_KEY, enter.value === 'send' ? '1' : '0');
  });
  panel.appendChild(field(t('settings.enterBehavior'), enter));

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

  // Completion sound — play a preview when switched on so it's not a mystery.
  const sound = document.createElement('input');
  sound.type = 'checkbox';
  sound.checked = soundEnabled();
  sound.addEventListener('change', () => {
    localStorage.setItem(SOUND_KEY, sound.checked ? '1' : '0');
    if (sound.checked) playCompletionSound();
  });
  const soundRow = field(t('settings.sound'), sound);
  soundRow.classList.add('settings-check');
  panel.appendChild(soundRow);

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
  applyTextSize(); // restore the saved size before the transcript renders
  document
    .getElementById('settings-btn')
    ?.addEventListener('click', openSettingsDialog);
}
