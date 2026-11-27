// assistant-config.js — the Settings "Assistant" pane: how the assistant
// behaves in every chat — its custom instructions, the permission mode a new
// chat starts in, and its switches. Like the Models pane it edits the
// server's config.json and exists only with features.settings.
//
// These are where a new chat starts; a chat picks its own mode in the
// composer. A switch or the mode is saved as it is flipped; the instructions,
// typed, have their own Save. Either applies from the next run: a reply
// already streaming keeps the settings it started with. The way lovia web was started may fix
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
  /** Instructions typed and not saved, carried through a redraw. @type {string | null} */
  let draft = null;

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

    // One save at a time, in the order made; a failed one says why and puts
    // the pane back as saved.
    let queue = Promise.resolve();
    /** @param {Record<string, unknown>} body @param {string} [done] a toast on success */
    const apply = (body, done) => {
      queue = queue.then(async () => {
        try {
          await api.updateAssistant(body);
          await loadConfig();
          await refreshAgents();
          if (done) toast(done);
        } catch (err) {
          // Back to what is saved — but what was typed stays, to retry.
          toast(String(err.message || err), { type: 'error' });
          draft = prompt.value;
          await loadConfig().catch(() => {});
          render();
        }
      });
      return queue;
    };

    const prompt = /** @type {HTMLTextAreaElement} */ (el('textarea', 'lv-dialog-input lv-cfg-input'));
    prompt.rows = 5;
    prompt.value = draft ?? (a.instructions || '');
    draft = null;
    prompt.placeholder = t('cfg.instructionsPlaceholder');
    const promptField = field(t('cfg.instructions'), prompt, t('cfg.instructionsHint'));
    // Typed text has a Save, live once it differs from what is saved.
    const savePrompt = el('button', 'lv-btn lv-btn-sm lv-btn-primary', t('cfg.save'));
    savePrompt.setAttribute('type', 'button');
    const syncPrompt = () => {
      savePrompt.toggleAttribute('disabled', !c.writable || prompt.value === (cfg().assistant.instructions || ''));
    };
    prompt.addEventListener('input', syncPrompt);
    savePrompt.addEventListener('click', async () => {
      savePrompt.toggleAttribute('disabled', true);
      await apply({ instructions: prompt.value }, t('cfg.savedNextRun'));
      syncPrompt();
    });
    syncPrompt();
    const promptActions = el('div', 'lv-cfg-inline-actions');
    promptActions.appendChild(savePrompt);
    promptField.appendChild(promptActions);
    section().appendChild(promptField);

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
          void apply({ mode: val });
        },
      );
      const group = el('div', 'lv-assistant-card-group');
      group.setAttribute('role', 'group');
      group.setAttribute('aria-label', t('cfg.autoAllow'));
      group.appendChild(el('div', 'lv-assistant-card-head', t('cfg.autoAllow')));
      for (const name of perms.bundles) {
        const sw = switchControl(a.auto_allow.includes(name));
        bundles.set(name, sw.value);
        sw.node.addEventListener('change', () => {
          void apply({ auto_allow: [...bundles].filter(([, on]) => on()).map(([n]) => n) });
        });
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

    /** A switch that saves ``name`` as it is flipped. @param {string} name */
    const saving = (name) => {
      const sw = switchControl(Boolean(a[name]));
      sw.node.addEventListener('change', () => void apply({ [name]: sw.value() }));
      return sw;
    };
    const search = saving('search');
    const memory = saving('memory');
    const subagents = saving('subagents');
    const followups = saving('followups');
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

    box.append(form);
    if (c.writable) box.appendChild(el('div', 'lv-cfg-footnote', t('cfg.appliesNextRun')));
    return box;
  }

  render();
  return pane;
}
