// settings.js — the Settings dialog: a left-rail shell (General / Models /
// Search / About) in a wide dialog. General holds the client-side preferences
// (localStorage: language, theme, text size, Enter behavior, notifications,
// sound); Models/Search edit the server's config.json through model-config.js
// and appear only when the server reports features.model_config.
import { segmented, showDialog, setThemePref, themePref } from './ui.js';
import { t, languages, langPref, setLangPref } from './i18n.js';
import { toast } from './toast.js';
import { store } from './store.js';
import { buildAboutPane, buildModelsPane, buildSearchPane, buildSkillsPane } from './model-config.js';

const NOTIF_KEY = 'lovia-notify';
const SOUND_KEY = 'lovia-sound';
const ENTER_KEY = 'lovia-enter-send';
const TEXT_SIZE_KEY = 'lovia-text-size';
const FOLLOWUPS_KEY = 'lovia-followups';

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
  if (size === 'md') root.style.removeProperty('--lv-chat-font-scale');
  else root.style.setProperty('--lv-chat-font-scale', TEXT_SCALES[size] || TEXT_SCALES.md);
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

// ---- Follow-up suggestions ------------------------------------------------
/**
 * Whether to ask the server for follow-up chips after a reply (on by default,
 * matching the server's willingness). This is the *preference* half of the
 * feature — `store.canSuggest` is the capability half, and both must hold. Read
 * before the request goes out, so turning it off costs the user nothing.
 * @returns {boolean}
 */
export function followupsEnabled() {
  return localStorage.getItem(FOLLOWUPS_KEY) !== '0';
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

// ---- General pane ---------------------------------------------------------

/**
 * A settings row: label (with an optional hint under it) and its control. A
 * radio group sits in a plain row — a wrapping <label> would forward every
 * click on the row to its first radio.
 * @param {string} labelText
 * @param {HTMLElement} control
 * @param {string} [hint]
 */
function field(labelText, control, hint) {
  const group = control.getAttribute('role') === 'radiogroup';
  const row = document.createElement(group ? 'div' : 'label');
  row.className = 'lv-settings-row';
  const text = document.createElement('span');
  text.className = 'lv-settings-text';
  const span = document.createElement('span');
  span.className = 'lv-settings-label';
  span.textContent = labelText;
  text.append(span);
  if (hint) {
    const sub = document.createElement('span');
    sub.className = 'lv-settings-hint';
    sub.textContent = hint;
    text.append(sub);
  }
  if (group) control.setAttribute('aria-label', labelText);
  row.append(text, control);
  return row;
}

function select(options, value) {
  const el = document.createElement('select');
  el.className = 'lv-dialog-input lv-settings-select';
  for (const [val, label] of options) {
    const opt = document.createElement('option');
    opt.value = val;
    opt.textContent = label;
    el.appendChild(opt);
  }
  el.value = value;
  return el;
}

/**
 * An on/off switch (the schedules list's). `onChange` gets the new state, and
 * `set` to put it back when the change didn't take.
 * @param {boolean} checked
 * @param {(on: boolean, set: (on: boolean) => void) => void} onChange
 */
function toggle(checked, onChange) {
  const sw = document.createElement('button');
  sw.type = 'button';
  sw.className = 'lv-switch';
  sw.setAttribute('role', 'switch');
  const set = (on) => sw.setAttribute('aria-checked', String(on));
  set(checked);
  const knob = document.createElement('span');
  knob.className = 'lv-switch-knob';
  sw.append(knob);
  sw.addEventListener('click', () => {
    const on = sw.getAttribute('aria-checked') !== 'true';
    set(on);
    onChange(on, set);
  });
  return sw;
}

function buildGeneralPane() {
  const panel = document.createElement('div');
  panel.className = 'lv-settings-panel';

  // Language — applies on reload. 'auto' is the deployment's default_lang,
  // else the browser's.
  const lang = select([['auto', t('settings.langAuto')], ...languages()], langPref());
  lang.addEventListener('change', () => {
    setLangPref(lang.value);
    location.reload();
  });
  const langRow = field(t('settings.language'), lang, t('settings.reloadNote'));
  langRow.dataset.part = 'language';
  panel.appendChild(langRow);

  // Theme — applies live.
  const theme = segmented(
    [
      ['light', t('settings.themeLight'), 'sun'],
      ['dark', t('settings.themeDark'), 'moon'],
      ['system', t('settings.themeSystem'), 'monitor'],
    ],
    themePref(),
    setThemePref,
  );
  panel.appendChild(field(t('settings.theme'), theme));

  // Message text size — applies live.
  const textSize = segmented(
    [
      ['sm', t('settings.textSmall')],
      ['md', t('settings.textDefault')],
      ['lg', t('settings.textLarge')],
    ],
    textSizePref(),
    (size) => {
      localStorage.setItem(TEXT_SIZE_KEY, size);
      applyTextSize(size);
    },
  );
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
    const notif = toggle(notificationsEnabled(), async (on, set) => {
      if (!on) {
        localStorage.setItem(NOTIF_KEY, '0');
        return;
      }
      const perm = await Notification.requestPermission();
      if (perm === 'granted') {
        localStorage.setItem(NOTIF_KEY, '1');
      } else {
        set(false);
        toast(t('settings.notifDenied'), { type: 'error' });
      }
    });
    panel.appendChild(field(t('settings.notifications'), notif));
  }

  // Completion sound — play a preview when switched on so it's not a mystery.
  const sound = toggle(soundEnabled(), (on) => {
    localStorage.setItem(SOUND_KEY, on ? '1' : '0');
    if (on) playCompletionSound();
  });
  panel.appendChild(field(t('settings.sound'), sound));

  // Follow-up chips — only offered when the server can actually produce them,
  // so this is never a switch that does nothing (same guard as notifications).
  if (store.canSuggest) {
    const followups = toggle(followupsEnabled(), (on) => {
      localStorage.setItem(FOLLOWUPS_KEY, on ? '1' : '0');
    });
    panel.appendChild(field(t('settings.followups'), followups));
  }
  return panel;
}

// ---- Dialog shell ---------------------------------------------------------

/**
 * Open the Settings dialog on a given tab.
 * @param {'general' | 'models' | 'search' | 'skills' | 'about'} [initialTab]
 */
export function openSettings(initialTab = 'general') {
  const tabs = [
    { id: 'general', label: t('settings.tabGeneral'), build: buildGeneralPane },
  ];
  if (store.canConfigureModels) {
    tabs.push(
      { id: 'models', label: t('settings.tabModels'), build: buildModelsPane },
      { id: 'search', label: t('settings.tabSearch'), build: buildSearchPane },
      { id: 'skills', label: t('settings.tabSkills'), build: buildSkillsPane },
    );
  }
  tabs.push({ id: 'about', label: t('settings.tabAbout'), build: buildAboutPane });

  const shell = document.createElement('div');
  shell.className = 'lv-settings-shell';

  const head = document.createElement('div');
  head.className = 'lv-settings-head';
  const h = document.createElement('h3');
  h.textContent = t('settings.title');
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'lv-btn-icon lv-settings-close';
  close.setAttribute('aria-label', t('dialog.close'));
  close.textContent = '✕';
  head.append(h, close);
  shell.appendChild(head);

  const body = document.createElement('div');
  body.className = 'lv-settings-body';
  const rail = document.createElement('nav');
  rail.className = 'lv-settings-rail';
  rail.setAttribute('role', 'tablist');
  const pane = document.createElement('div');
  pane.className = 'lv-settings-pane-host';
  pane.id = 'settings-pane';
  pane.setAttribute('role', 'tabpanel');

  let active = tabs.some((tab) => tab.id === initialTab) ? initialTab : 'general';
  const buttons = new Map();
  const activate = (id) => {
    active = id;
    for (const [tabId, btn] of buttons) {
      btn.classList.toggle('lv-active', tabId === id);
      btn.setAttribute('aria-selected', tabId === id ? 'true' : 'false');
    }
    pane.setAttribute('aria-labelledby', `settings-tab-${id}`);
    const tab = tabs.find((item) => item.id === id);
    pane.replaceChildren(tab ? tab.build() : document.createElement('div'));
  };
  for (const tab of tabs) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'lv-settings-rail-item';
    btn.id = `settings-tab-${tab.id}`;
    btn.setAttribute('role', 'tab');
    btn.setAttribute('aria-controls', 'settings-pane');
    btn.textContent = tab.label;
    btn.addEventListener('click', () => activate(tab.id));
    buttons.set(tab.id, btn);
    rail.appendChild(btn);
  }
  body.append(rail, pane);
  shell.appendChild(body);

  const dialog = showDialog({ body: shell });
  dialog.classList.add('lv-dialog-xl');
  close.addEventListener('click', () => dialog.close());
  activate(active);
  return dialog;
}

export function initSettings() {
  applyTextSize(); // restore the saved size before the transcript renders
  document
    .getElementById('settings-btn')
    ?.addEventListener('click', () => openSettings());
}
