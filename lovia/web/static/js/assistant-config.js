// assistant-config.js — the Settings "Assistant" pane: how the assistant
// behaves in every chat — its custom instructions, the permission mode a new
// chat starts in, and its switches. Like the Models pane it edits the
// server's config.json and exists only with features.model_config.
//
// These are where a new chat starts; a chat picks its own mode in the
// composer. A save applies from the next run: a reply already streaming
// keeps the settings it started with. The way lovia web was started may fix
// the permissions (--readonly, --no-workspace); the server refuses changing
// them then, and the form leaves them out.
import { api } from './api.js';
import { t } from './i18n.js';
import { toast } from './toast.js';
import { segmented } from './ui.js';
import {
  cfg,
  el,
  field,
  loadConfig,
  notice,
  readOnlyNotice,
  refreshAgents,
  scopeFootnote,
} from './model-config.js';

/**
 * An on/off switch for a form row; `value()` reads it back.
 * @param {boolean} checked
 */
export function switchControl(checked) {
  const sw = el('button', 'lv-switch');
  sw.setAttribute('type', 'button');
  sw.setAttribute('role', 'switch');
  sw.setAttribute('aria-checked', String(checked));
  sw.appendChild(el('span', 'lv-switch-knob'));
  sw.addEventListener('click', () => {
    if (sw.hasAttribute('disabled')) return;
    sw.setAttribute('aria-checked', String(sw.getAttribute('aria-checked') !== 'true'));
    sw.dispatchEvent(new Event('change'));
  });
  return { node: sw, value: () => sw.getAttribute('aria-checked') === 'true' };
}

/** A labelled switch row (label and hint left, switch right). */
function switchRow(labelText, control, hintText) {
  const row = el('div', 'lv-cfg-switch-row');
  const text = el('div', 'lv-cfg-switch-text');
  text.appendChild(el('span', 'lv-cfg-label', labelText));
  if (hintText) text.appendChild(el('div', 'lv-cfg-hint', hintText));
  control.setAttribute('aria-label', labelText);
  row.append(text, control);
  // The whole row toggles, as on the General page.
  row.addEventListener('click', (e) => {
    if (!control.contains(/** @type {Node} */ (e.target))) control.click();
  });
  return row;
}

/** @param {string} mode */
export function modeLabel(mode) {
  return t(`cfg.mode${mode[0].toUpperCase()}${mode.slice(1)}`);
}

export function buildAssistantPane() {
  const pane = el('div', 'lv-cfg-pane');

  async function render() {
    if (!cfg()) {
      pane.replaceChildren(el('div', 'lv-cfg-hint', '…'));
      try {
        await loadConfig();
      } catch (err) {
        pane.replaceChildren(el('div', 'lv-cfg-error', String(err.message || err)));
        return;
      }
    }
    pane.replaceChildren(renderForm());
    const ro = readOnlyNotice();
    if (ro) pane.prepend(ro);
  }

  function renderForm() {
    const c = cfg();
    const perms = c.permissions;
    const a = c.assistant;
    const box = el('div');
    const head = el('div', 'lv-cfg-pane-head');
    head.appendChild(el('span', 'lv-cfg-pane-title', t('settings.tabAssistant')));
    box.append(head, el('div', 'lv-cfg-hint lv-skills-intro', t('assistant.intro')));
    // Sections divided by a rule, switch lists on a card: the schedule
    // form's layout.
    const form = el('div', 'lv-cfg-form lv-assistant-form');
    const section = () => form.appendChild(el('section', 'lv-assistant-section'));
    /** A grey card around a mode note or a list of switch rows. @param {HTMLElement[]} rows */
    const card = (...rows) => {
      const c = el('div', 'lv-assistant-card');
      c.append(...rows);
      return c;
    };

    const prompt = /** @type {HTMLTextAreaElement} */ (el('textarea', 'lv-dialog-input lv-cfg-input'));
    prompt.rows = 5;
    prompt.value = a.instructions || '';
    prompt.placeholder = t('cfg.instructionsPlaceholder');
    section().appendChild(field(t('cfg.instructions'), prompt, t('cfg.instructionsHint')));

    // ---- permissions (left out when the launch fixed them): the mode's
    // tabs over a card that says what it allows
    let mode = a.mode;
    /** @type {Map<string, () => boolean>} */
    const bundles = new Map();
    const runs = section();
    if (perms.lock) {
      runs.appendChild(notice(t(`cfg.wsLock_${perms.lock}`)));
    } else {
      const modeHint = el('div', 'lv-assistant-card-note');
      const modeCtl = segmented(
        perms.modes.map((/** @type {string} */ m) => [m, modeLabel(m)]),
        mode,
        (/** @type {string} */ val) => {
          mode = val;
          sync();
        },
      );
      const group = el('div', 'lv-assistant-card-group');
      group.setAttribute('role', 'group');
      group.setAttribute('aria-label', t('cfg.autoAllow'));
      group.appendChild(el('div', 'lv-assistant-card-head', t('cfg.autoAllow')));
      for (const name of perms.bundles) {
        const sw = switchControl(a.auto_allow.includes(name));
        bundles.set(name, sw.value);
        group.appendChild(switchRow(t(`cfg.bundle_${name}`), sw.node, t(`cfg.bundleHint_${name}`)));
      }
      const modeField = field(t('assistant.mode'), modeCtl);
      modeField.appendChild(card(modeHint, group));
      runs.appendChild(modeField);
      // Auto-allowed commands mean something in coding mode only: the other
      // modes never ask (trusted) or never run one (read-only). Hidden there,
      // not greyed — a dimmed list reads as a setting that still applies.
      // Its choices are kept, and saved, either way.
      const sync = () => {
        modeHint.textContent = t('assistant.modeHint', { mode: t(`cfg.modeHint_${mode}`) });
        group.hidden = mode !== 'coding';
      };
      sync();
    }

    const search = switchControl(a.search);
    const memory = switchControl(a.memory);
    const subagents = switchControl(a.subagents);
    const followups = switchControl(a.followups);
    const abilities = card(
      switchRow(t('assistant.search'), search.node, t('assistant.searchHint')),
      switchRow(t('cfg.memory'), memory.node, t('cfg.memoryHint')),
      switchRow(t('cfg.subagents'), subagents.node, t('cfg.subagentsHint')),
      switchRow(t('cfg.followups'), followups.node, t('cfg.followupsHint')),
    );
    section().appendChild(field(t('assistant.abilities'), abilities));

    if (!c.writable) {
      for (const ctl of form.querySelectorAll('input, textarea, select, button')) {
        ctl.toggleAttribute('disabled', true);
      }
    }

    function read() {
      /** @type {Record<string, unknown>} */
      const body = {
        instructions: prompt.value,
        search: search.value(),
        memory: memory.value(),
        subagents: subagents.value(),
        followups: followups.value(),
      };
      if (!perms.lock) {
        body.mode = mode;
        body.auto_allow = [...bundles].filter(([, on]) => on()).map(([n]) => n);
      }
      return body;
    }

    const errBox = el('div', 'lv-cfg-error');
    errBox.hidden = true;
    const actions = el('div', 'lv-cfg-form-actions');
    const save = el('button', 'lv-btn lv-btn-primary', t('cfg.save'));
    save.setAttribute('type', 'button');
    save.toggleAttribute('disabled', !c.writable);
    save.addEventListener('click', async () => {
      save.disabled = true;
      errBox.hidden = true;
      try {
        await api.updateAssistant(read());
        await loadConfig();
        await refreshAgents();
        toast(t('cfg.savedNextRun'));
        render();
      } catch (err) {
        errBox.textContent = String(err.message || err);
        errBox.hidden = false;
        save.disabled = false;
      }
    });
    actions.appendChild(save);
    if (c.writable) actions.appendChild(el('div', 'lv-cfg-footnote', t('cfg.appliesNextRun')));
    // The two limits Settings doesn't edit: say what they are and where
    // (the footnote under it names the file).
    const limits = el(
      'div',
      'lv-cfg-footnote',
      t('assistant.limits', { turns: a.max_turns, retries: a.max_retries }),
    );
    box.append(form, errBox, actions, limits, scopeFootnote());
    return box;
  }

  render();
  return pane;
}
