// agent-config.js — the Settings "Workspace" and "Agent" panes: how the
// default agent works (workspace mode, auto-allowed commands, run limits,
// memory, subagents, the system prompt). Like the Models pane they edit the
// server's config.json and exist only with features.model_config.
//
// Every save applies from the next run: a reply already streaming keeps the
// settings it started with. Two things can make a pane read-only, and the
// server enforces both — config.json isn't writable (every config pane says
// so), or (Workspace only) the way lovia web was started fixes the
// workspace settings.
import { api } from './api.js';
import { t } from './i18n.js';
import { toast } from './toast.js';
import { segmented } from './ui.js';
import {
  cfg,
  el,
  field,
  input,
  loadConfig,
  notice,
  positiveInt,
  readOnlyNotice,
} from './model-config.js';

/**
 * An on/off switch for a form row; `value()` reads it back.
 * @param {boolean} checked
 */
function switchControl(checked) {
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
  const label = el('span', 'lv-cfg-label', labelText);
  text.appendChild(label);
  if (hintText) text.appendChild(el('div', 'lv-cfg-hint', hintText));
  control.setAttribute('aria-label', labelText);
  row.append(text, control);
  return row;
}

/**
 * Shared pane scaffolding: load the config, draw the title, then hand the
 * body to `build`. `build` returns the payload reader and the controls a
 * read-only state disables.
 * @param {string} title
 * @param {(c: any, form: HTMLElement) => { read: () => object | null, controls: HTMLElement[], locked?: boolean }} build
 * @param {(body: object) => Promise<unknown>} send
 */
function settingsPane(title, build, send) {
  const pane = el('div', 'lv-cfg-pane');

  async function render() {
    if (!cfg()) {
      try {
        await loadConfig();
      } catch (err) {
        pane.replaceChildren(el('div', 'lv-cfg-error', String(err.message || err)));
        return;
      }
    }
    const c = cfg();
    pane.replaceChildren(el('div', 'lv-cfg-pane-title', title));
    const ro = readOnlyNotice();
    if (ro) pane.appendChild(ro);
    const form = el('div', 'lv-cfg-form');
    const { read, controls, locked } = build(c, form);
    const frozen = !c.writable || !!locked;
    for (const ctl of controls) {
      ctl.toggleAttribute('disabled', frozen);
      for (const inner of ctl.querySelectorAll('input, textarea, select, button')) {
        inner.toggleAttribute('disabled', frozen);
      }
    }

    const errBox = el('div', 'lv-cfg-error');
    errBox.hidden = true;
    const actions = el('div', 'lv-cfg-form-actions');
    const save = el('button', 'lv-btn lv-btn-primary', t('cfg.save'));
    save.setAttribute('type', 'button');
    save.toggleAttribute('disabled', frozen);
    save.addEventListener('click', async () => {
      const body = read();
      if (body === null) return; // the reader flagged an invalid field
      save.disabled = true;
      errBox.hidden = true;
      try {
        await send(body);
        await loadConfig();
        toast(t('cfg.savedNextRun'));
        render();
      } catch (err) {
        errBox.textContent = String(err.message || err);
        errBox.hidden = false;
        save.disabled = false;
      }
    });
    actions.appendChild(save);
    pane.append(form, errBox, actions);
    if (!frozen) pane.appendChild(el('div', 'lv-cfg-footnote', t('cfg.appliesNextRun')));
  }

  render();
  return pane;
}

// ---- Workspace pane -------------------------------------------------------

const MODE_LABELS = () => ({
  readonly: t('cfg.modeReadonly'),
  coding: t('cfg.modeCoding'),
  trusted: t('cfg.modeTrusted'),
});

export function buildWorkspacePane() {
  return settingsPane(
    t('settings.tabWorkspace'),
    (c, form) => {
      const ws = c.workspace;
      const labels = MODE_LABELS();
      if (ws.lock) form.appendChild(notice(t(`cfg.wsLock_${ws.lock}`)));
      if (ws.lock === 'no_workspace') return { read: () => ({}), controls: [], locked: true };

      // Locked by --readonly: show what runs get. Otherwise the file's
      // choice — even one this server can't offer (trusted off loopback)
      // still shows, so the control never misstates the file.
      let mode = ws.lock === 'readonly' ? ws.effective_mode : ws.default_mode;
      const offered = ws.modes.includes(mode) ? ws.modes : [...ws.modes, mode];
      const modeCtl = segmented(
        offered.map((m) => [m, labels[m] || m]),
        mode,
        (val) => {
          mode = val;
          sync();
        },
      );
      const modeHint = el('div', 'lv-cfg-hint');
      const modeField = field(t('cfg.defaultMode'), modeCtl);
      modeField.appendChild(modeHint);
      form.appendChild(modeField);
      if (ws.lock !== 'readonly' && ws.effective_mode && ws.effective_mode !== ws.default_mode) {
        form.appendChild(
          notice(t('cfg.modeCapped', { mode: labels[ws.default_mode], runs: labels[ws.effective_mode] })),
        );
      }

      const bundles = el('div', 'lv-cfg-checks');
      /** @type {Map<string, HTMLInputElement>} */
      const boxes = new Map();
      for (const name of ws.bundles) {
        const row = el('label', 'lv-cfg-check');
        const box = /** @type {HTMLInputElement} */ (el('input'));
        box.type = 'checkbox';
        box.checked = ws.auto_allow.includes(name);
        boxes.set(name, box);
        const text = el('span', '');
        text.appendChild(el('span', 'lv-cfg-check-label', t(`cfg.bundle_${name}`)));
        text.appendChild(el('span', 'lv-cfg-hint', t(`cfg.bundleHint_${name}`)));
        row.append(box, text);
        bundles.appendChild(row);
      }
      const bundleField = field(t('cfg.autoAllow'), bundles, t('cfg.autoAllowHint'));
      form.appendChild(bundleField);

      function sync() {
        modeHint.textContent = t(`cfg.modeHint_${mode}`);
        // Auto-allowed commands mean something in coding mode only: the
        // other modes never ask (trusted) or never run one (read-only).
        // Hidden there, not greyed — a dimmed list reads as a setting that
        // still applies. Its choices are kept, and saved, either way.
        bundleField.hidden = mode !== 'coding';
      }
      sync();

      return {
        read: () => ({
          default_mode: mode,
          auto_allow: [...boxes].filter(([, b]) => b.checked).map(([n]) => n),
        }),
        controls: [modeCtl, bundles],
        locked: !!ws.lock,
      };
    },
    (body) => api.setWorkspaceSettings(body),
  );
}

// ---- Agent pane -----------------------------------------------------------

export function buildAgentPane() {
  return settingsPane(
    t('settings.tabAgent'),
    (c, form) => {
      const { run, agent } = c;

      const prompt = /** @type {HTMLTextAreaElement} */ (el('textarea', 'lv-dialog-input lv-cfg-input'));
      prompt.rows = 5;
      prompt.value = agent.instructions || '';
      prompt.placeholder = t('cfg.instructionsPlaceholder');
      form.appendChild(field(t('cfg.instructions'), prompt, t('cfg.instructionsHint')));

      const turns = input(String(run.max_turns));
      turns.inputMode = 'numeric';
      form.appendChild(field(t('cfg.maxTurns'), turns, t('cfg.maxTurnsHint')));

      // Empty means the agent's own retry posture: say how many that is.
      const retries = input(run.max_retries == null ? '' : String(run.max_retries), {
        placeholder: t('cfg.maxRetriesAuto', { n: c.defaults?.max_retries ?? 4 }),
      });
      retries.inputMode = 'numeric';
      form.appendChild(field(t('cfg.maxRetries'), retries, t('cfg.maxRetriesHint')));

      const followups = switchControl(run.followups);
      form.appendChild(switchRow(t('cfg.followups'), followups.node, t('cfg.followupsHint')));

      const subagents = switchControl(agent.subagents);
      form.appendChild(switchRow(t('cfg.subagents'), subagents.node, t('cfg.subagentsHint')));

      const memory = switchControl(agent.memory);
      form.appendChild(switchRow(t('cfg.memory'), memory.node, t('cfg.memoryHint')));
      const memDir = input(agent.memory_dir, { mono: true });
      const memDirField = field(t('cfg.memoryDir'), memDir);
      form.appendChild(memDirField);
      // Disabled with memory off, not hidden: the field sits right under its
      // switch, and taking it out of the layout would shift the pane (and
      // what the pointer is on) at the click.
      const syncMemory = () => {
        const on = memory.value();
        memDir.disabled = !on || memory.node.hasAttribute('disabled');
        memDirField.classList.toggle('lv-cfg-off', !on);
      };
      memory.node.addEventListener('change', syncMemory);
      syncMemory();
      // Once the pane has applied a read-only state to the switch (after
      // this builder returns), which the folder then follows too.
      queueMicrotask(syncMemory);

      /** @param {HTMLInputElement} node */
      const invalid = (node) => {
        node.classList.add('lv-invalid');
        node.focus();
        node.addEventListener('input', () => node.classList.remove('lv-invalid'), { once: true });
        return null;
      };

      return {
        read: () => {
          const maxTurns = positiveInt(turns.value);
          if (maxTurns === undefined) return invalid(turns);
          let maxRetries = null;
          if (retries.value.trim()) {
            const n = Number(retries.value.trim());
            if (!Number.isInteger(n) || n < 0) return invalid(retries);
            maxRetries = n;
          }
          if (memory.value() && !memDir.value.trim()) return invalid(memDir);
          return {
            run: { max_turns: maxTurns, max_retries: maxRetries, followups: followups.value() },
            agent: {
              instructions: prompt.value,
              subagents: subagents.value(),
              memory: memory.value(),
              ...(memDir.value.trim() ? { memory_dir: memDir.value.trim() } : {}),
            },
          };
        },
        // The memory folder follows its switch (syncMemory), not this list.
        controls: [prompt, turns, retries, followups.node, subagents.node, memory.node],
      };
    },
    // Agent first: it is the half the server can still refuse (a memory
    // directory that is a file), and the run limits are checked above —
    // so a refusal leaves nothing half-saved.
    /** @param {any} body */
    async (body) => {
      await api.setAgentSettings(body.agent);
      await api.setRunSettings(body.run);
    },
  );
}
