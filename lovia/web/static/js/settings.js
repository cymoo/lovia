// settings.js — the Settings dialog: a left-rail shell in a wide dialog, its
// tabs in three groups. Yours: General (this browser's preferences in
// localStorage — language, Enter behavior, notifications, sound),
// Appearance (appearance.js) and Memory (memory.js; when an agent has it).
// The server's, shared by everyone on it, shown only when it reports
// features.settings: Models, Assistant, Skills, MCP, Web search, Voice input
// (config.json through model-config.js, assistant-config.js, mcp-config.js,
// voice-settings.js). Then About.
import { segmented, showDialog } from './ui.js';
import { t, languages, langPref, setLangPref } from './i18n.js';
import { toast } from './toast.js';
import { store } from './store.js';
import { icon } from './icons.js';
import { api } from './api.js';
import { buildAboutPane, buildModelsPane, buildSearchPane, buildSkillsPane, cfg, loadConfig } from './model-config.js';
import { buildAssistantPane } from './assistant-config.js';
import { mcpPane } from './mcp-config.js';
import { buildVoicePane } from './voice-settings.js';
import { hasMemory, memoryPane } from './memory.js';
import { buildAppearancePane, initAppearance } from './appearance.js';

const NOTIF_KEY = 'lovia-notify';
const SOUND_KEY = 'lovia-sound';
const ENTER_KEY = 'lovia-enter-send';

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

// ---- Rows and groups ------------------------------------------------------

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

/** Rows that belong together, on one card. @param {(HTMLElement | null)[]} rows */
export function settingsGroup(...rows) {
  const card = document.createElement('div');
  card.className = 'lv-settings-group';
  card.append(...rows.filter((r) => r !== null));
  return card;
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

// ---- General pane ---------------------------------------------------------

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

  // Enter behavior — read live by the composer, so no reload needed. The
  // hint spells out the other key of the pair.
  const enterHint = (v) => t(v === 'send' ? 'settings.enterSendHint' : 'settings.enterNewlineHint');
  const enterNow = enterToSend() ? 'send' : 'newline';
  const enter = segmented(
    [
      ['send', t('settings.enterSend')],
      ['newline', t('settings.enterNewline')],
    ],
    enterNow,
    (v) => {
      localStorage.setItem(ENTER_KEY, v === 'send' ? '1' : '0');
      enterRow.querySelector('.lv-settings-hint').textContent = enterHint(v);
    },
  );
  const enterRow = field(t('settings.enterBehavior'), enter, enterHint(enterNow));

  // Desktop notifications for finished background runs.
  let notifRow = null;
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
    notifRow = field(t('settings.notifications'), notif, t('settings.notificationsHint'));
  }

  // Completion sound — play a preview when switched on so it's not a mystery.
  const sound = toggle(soundEnabled(), (on) => {
    localStorage.setItem(SOUND_KEY, on ? '1' : '0');
    if (on) playCompletionSound();
  });

  panel.append(
    settingsGroup(langRow, enterRow),
    settingsGroup(notifRow, field(t('settings.sound'), sound, t('settings.soundHint'))),
  );
  // The server's System proxy: config.json, so only where Settings edits it.
  if (store.canConfigure) panel.appendChild(proxyField());
  return panel;
}

/** Settings → General's server rows — System proxy, Keep awake: saved as picked. */
function proxyField() {
  const wrap = settingsGroup();
  const draw = () => {
    const c = cfg();
    if (!c) return;
    const on = c.proxy.trust_env ?? c.proxy.launch;
    const ctl = segmented(
      [
        ['on', t('cfg.proxyOn')],
        ['off', t('cfg.proxyOff')],
      ],
      on ? 'on' : 'off',
      async (val) => {
        try {
          await api.setProxy({ trust_env: val === 'on' });
          await loadConfig();
        } catch (err) {
          toast(String(err.message || err), { type: 'error' });
          await loadConfig().catch(() => {});
          draw(); // back to what is saved
        }
      },
    );
    if (!c.writable) {
      for (const radio of ctl.querySelectorAll('input')) radio.disabled = true;
    }
    wrap.replaceChildren(field(t('cfg.proxy'), ctl, t('settings.proxyHint')));
    // Keep awake, first: only where the server's machine can be kept from
    // sleeping.
    if (c.keep_awake?.supported) {
      const sw = toggle(c.keep_awake.on, async (on, set) => {
        try {
          await api.setKeepAwake({ on });
          await loadConfig();
        } catch (err) {
          toast(String(err.message || err), { type: 'error' });
          set(!on);
        }
      });
      if (!c.writable) sw.disabled = true;
      wrap.prepend(field(t('settings.keepAwake'), sw, t('settings.keepAwakeHint')));
    }
  };
  if (cfg()) draw();
  else loadConfig().then(draw, () => {});
  return wrap;
}

// ---- Dialog shell ---------------------------------------------------------

/**
 * Open the Settings dialog on a given tab.
 * @param {'general' | 'appearance' | 'memory' | 'models' | 'assistant' | 'skills' | 'mcp' | 'search' | 'voice' | 'about'} [initialTab]
 * @param {{ root?: string }} [opts] Open Memory and MCP on this directory's project.
 */
export function openSettings(initialTab = 'general', opts = {}) {
  /** @typedef {{ id: string, label: string, build: () => HTMLElement }} Tab */
  /** @type {Tab[][]} */
  const groups = [
    [
      { id: 'general', label: t('settings.tabGeneral'), build: buildGeneralPane },
      {
        id: 'appearance',
        label: t('settings.tabAppearance'),
        build: () => buildAppearancePane({ row: field, group: settingsGroup, segmented }),
      },
      ...(hasMemory()
        ? [{ id: 'memory', label: t('settings.tabMemory'), build: memoryPane(opts) }]
        : []),
    ],
    // The server's configuration: what the assistant runs on first (nothing
    // works without a model), then how it behaves and what it can use.
    store.canConfigure
      ? [
          { id: 'models', label: t('settings.tabModels'), build: buildModelsPane },
          { id: 'assistant', label: t('settings.tabAssistant'), build: buildAssistantPane },
          { id: 'skills', label: t('settings.tabSkills'), build: buildSkillsPane },
          { id: 'mcp', label: t('settings.tabMcp'), build: mcpPane(opts) },
          { id: 'search', label: t('settings.tabSearch'), build: buildSearchPane },
          { id: 'voice', label: t('settings.tabVoice'), build: buildVoicePane },
        ]
      : [],
    [{ id: 'about', label: t('settings.tabAbout'), build: buildAboutPane }],
  ].filter((g) => g.length);
  const tabs = groups.flat();

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
  close.innerHTML = icon('x', { size: 16 });
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
    pane.scrollTop = 0;
  };
  groups.forEach((group, i) => {
    if (i) {
      const sep = document.createElement('div');
      sep.className = 'lv-settings-rail-sep';
      sep.setAttribute('role', 'presentation');
      rail.appendChild(sep);
    }
    for (const tab of group) {
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
  });
  body.append(rail, pane);
  shell.appendChild(body);

  const dialog = showDialog({ body: shell });
  dialog.classList.add('lv-dialog-xl');
  close.addEventListener('click', () => dialog.close());
  activate(active);
  return dialog;
}

export function initSettings() {
  initAppearance(); // in case the page's inline script couldn't (and to migrate)
  document
    .getElementById('settings-btn')
    ?.addEventListener('click', () => openSettings());
}
