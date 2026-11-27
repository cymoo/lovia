// appearance.js — Settings → Appearance: how this browser draws lovia. Theme
// (ui.js keeps it), and a few looks kept in localStorage as one object:
// accent, background tone, message text size, message font, chat width.
//
// Each look is an attribute on <html> (`data-lv-accent="blue"`), set only
// when it isn't the default, and styles.css maps it to tokens — so a page
// that never opened this tab carries no attribute, and a deployer's own
// tokens still decide its defaults. index.html sets them before first paint
// (its inline script mirrors applyAppearance below).
import { t } from './i18n.js';
import { setThemePref, syncThemeColor, themePref } from './ui.js';

const KEY = 'lovia-appearance';
// Until this tab existed, General kept the text size under its own key.
const OLD_SIZE_KEY = 'lovia-text-size';

/** Each look's choices in the order shown; every look's default is 'default'. */
export const LOOKS = {
  accent: ['default', 'blue', 'violet', 'orange', 'pink', 'graphite'],
  tone: ['default', 'warm', 'cool'],
  size: ['sm', 'default', 'lg'],
  font: ['default', 'serif'],
  width: ['default', 'wide'],
};
const DEFAULT = 'default';

/** @returns {Record<string, string>} The saved looks, unknown values dropped. */
export function appearance() {
  /** @type {Record<string, unknown>} */
  let saved = {};
  try {
    saved = JSON.parse(localStorage.getItem(KEY) || '{}') || {};
  } catch {
    /* unreadable: every look at its default */
  }
  /** @type {Record<string, string>} */
  const out = {};
  for (const [look, values] of Object.entries(LOOKS)) {
    const v = saved[look];
    out[look] = typeof v === 'string' && values.includes(v) ? v : DEFAULT;
  }
  return out;
}

/** Put the saved looks on <html>: an attribute per look not at its default. */
export function applyAppearance(looks = appearance()) {
  const root = document.documentElement;
  for (const look of Object.keys(LOOKS)) {
    const v = looks[look];
    if (v && v !== DEFAULT) root.setAttribute(`data-lv-${look}`, v);
    else root.removeAttribute(`data-lv-${look}`);
  }
  syncThemeColor(); // the tone moves the canvas the browser chrome matches
}

/** @param {Record<string, string>} looks */
function save(looks) {
  /** @type {Record<string, string>} */
  const kept = {};
  for (const look of Object.keys(LOOKS)) {
    if (looks[look] && looks[look] !== DEFAULT) kept[look] = looks[look];
  }
  try {
    if (Object.keys(kept).length) localStorage.setItem(KEY, JSON.stringify(kept));
    else localStorage.removeItem(KEY);
  } catch {
    /* storage disabled: the look holds for this page only */
  }
  applyAppearance(looks);
}

/** Carry General's old text size over once, then apply what is saved. */
export function initAppearance() {
  try {
    const old = localStorage.getItem(OLD_SIZE_KEY);
    if (old !== null) {
      localStorage.removeItem(OLD_SIZE_KEY);
      const looks = appearance();
      if ((old === 'sm' || old === 'lg') && looks.size === DEFAULT) save({ ...looks, size: old });
    }
  } catch {
    /* storage disabled: nothing to carry over */
  }
  applyAppearance();
}

// ---- The pane ----------------------------------------------------------------

let _groups = 0;

/**
 * A row of radio choices drawn as the caller likes — swatches, preview
 * cards. Native radios underneath, so arrow keys move between them.
 * @param {string} label The group's accessible name.
 * @param {string[]} values
 * @param {string} value
 * @param {(v: string) => HTMLElement} draw The visible face of one choice.
 * @param {(v: string) => void} onChange
 * @param {string} className
 */
function choices(label, values, value, draw, onChange, className) {
  const name = `lv-look-${++_groups}`;
  const group = document.createElement('div');
  group.className = className;
  group.setAttribute('role', 'radiogroup');
  group.setAttribute('aria-label', label);
  for (const v of values) {
    const opt = document.createElement('label');
    opt.className = 'lv-look-choice';
    opt.dataset.value = v;
    const radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = name;
    radio.value = v;
    radio.checked = v === value;
    radio.addEventListener('change', () => {
      if (radio.checked) onChange(v);
    });
    opt.append(radio, draw(v));
    group.appendChild(opt);
  }
  return group;
}

/** A tiny window in a theme: sidebar, two lines of text, the composer. */
function thumb(mode) {
  const win = document.createElement('span');
  win.className = 'lv-theme-thumb';
  win.dataset.mode = mode;
  win.innerHTML =
    '<span class="lv-theme-thumb-side"></span>' +
    '<span class="lv-theme-thumb-main"><i></i><i></i><i></i><b></b></span>';
  return win;
}

/**
 * The Appearance pane. `row(label, control, hint)` and `group(...rows)` are
 * settings.js's, so this pane looks like its neighbours.
 * @param {{ row: (label: string, control: HTMLElement, hint?: string) => HTMLElement, group: (...rows: HTMLElement[]) => HTMLElement, segmented: Function }} kit
 */
export function buildAppearancePane({ row, group, segmented }) {
  const panel = document.createElement('div');
  panel.className = 'lv-settings-panel';

  const draw = () => {
    const looks = appearance();
    const set = (look) => (v) => {
      save({ ...appearance(), [look]: v });
      syncReset();
    };

    const theme = choices(
      t('settings.theme'),
      ['light', 'dark', 'system'],
      themePref(),
      (mode) => {
        const face = document.createElement('span');
        face.className = 'lv-theme-card';
        const pic = document.createElement('span');
        pic.className = 'lv-theme-card-pic';
        if (mode === 'system') pic.append(thumb('light'), thumb('dark'));
        else pic.append(thumb(mode));
        const name = document.createElement('span');
        name.className = 'lv-theme-card-name';
        name.textContent = t(`settings.theme${mode[0].toUpperCase()}${mode.slice(1)}`);
        face.append(pic, name);
        return face;
      },
      (mode) => {
        setThemePref(mode);
        syncReset();
      },
      'lv-theme-cards',
    );

    const swatches = (look, title) =>
      choices(
        title,
        LOOKS[look],
        looks[look],
        (v) => {
          const dot = document.createElement('span');
          dot.className = `lv-swatch lv-swatch-${look}`;
          dot.dataset.value = v;
          dot.title = t(`settings.look.${look}.${v}`);
          return dot;
        },
        set(look),
        'lv-swatches',
      );

    const seg = (look, title) => {
      const ctl = segmented(
        LOOKS[look].map((v) => [v, t(`settings.look.${look}.${v}`)]),
        looks[look],
        set(look),
      );
      ctl.setAttribute('aria-label', title);
      return ctl;
    };

    const reset = document.createElement('button');
    reset.type = 'button';
    reset.className = 'lv-btn lv-btn-ghost lv-btn-sm lv-look-reset';
    reset.textContent = t('settings.look.reset');
    reset.addEventListener('click', () => {
      setThemePref('system');
      save({});
      draw();
    });
    const syncReset = () => {
      const now = appearance();
      reset.hidden = themePref() === 'system' && Object.keys(LOOKS).every((k) => now[k] === DEFAULT);
    };
    syncReset();

    // Swatches name their choice in a tooltip; the row says the current one.
    const named = (look, title) => {
      const r = row(title, swatches(look, title), t(`settings.look.${look}.${appearance()[look]}`));
      r.querySelector('.lv-swatches')?.addEventListener('change', () => {
        const hint = r.querySelector('.lv-settings-hint');
        if (hint) hint.textContent = t(`settings.look.${look}.${appearance()[look]}`);
      });
      return r;
    };

    const themeRow = row(t('settings.theme'), theme);
    themeRow.classList.add('lv-settings-row-theme');
    panel.replaceChildren(
      group(themeRow),
      group(named('accent', t('settings.look.accent')), named('tone', t('settings.look.tone'))),
      group(
        row(t('settings.textSize'), seg('size', t('settings.textSize'))),
        row(t('settings.look.font'), seg('font', t('settings.look.font')), t('settings.look.fontHint')),
        row(t('settings.look.width'), seg('width', t('settings.look.width')), t('settings.look.widthHint')),
      ),
      reset,
    );
  };
  draw();
  return panel;
}
