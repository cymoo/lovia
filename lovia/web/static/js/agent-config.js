// agent-config.js — the Settings "Agents" pane: the agents people pick for a
// chat, each with its own prompt, model, permissions, skills, switches and
// run limits. Like the Models pane it edits the server's config.json
// and exists only with features.model_config.
//
// The list's first agent is the default: a new chat starts with it (unless
// its project last used another), and a chat whose agent is deleted carries
// on with it. Every save applies from the next run: a reply already
// streaming keeps the settings it started with. The way lovia web was
// started may fix the permissions (--readonly, --no-workspace); the server
// refuses changing them then, and the form leaves them out.
import { api } from './api.js';
import { t } from './i18n.js';
import { toast } from './toast.js';
import { confirmDialog, segmented } from './ui.js';
import {
  cfg,
  el,
  field,
  ghostBtn,
  input,
  loadConfig,
  notice,
  positiveInt,
  readOnlyNotice,
  refreshAgents,
  scopeFootnote,
} from './model-config.js';

// AgentConfig.max_turns's default (DEFAULT_MAX_TURNS in web/config/schema.py).
const DEFAULT_MAX_TURNS = 100;

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
  text.appendChild(el('span', 'lv-cfg-label', labelText));
  if (hintText) text.appendChild(el('div', 'lv-cfg-hint', hintText));
  control.setAttribute('aria-label', labelText);
  row.append(text, control);
  return row;
}

/** @param {string} mode */
export function modeLabel(mode) {
  return t(`cfg.mode${mode[0].toUpperCase()}${mode.slice(1)}`);
}

/** The model an agent runs on, by name: its own, else the default chat one. */
export function modelName(/** @type {any} */ c, /** @type {any} */ agent) {
  const id = agent.model || c.roles.chat;
  const p = c.models.find((/** @type {any} */ m) => m.id === id);
  return p ? p.display_name || p.model : '';
}

/** @param {HTMLInputElement | HTMLTextAreaElement} node */
function invalid(node) {
  node.classList.add('lv-invalid');
  const fold = node.closest('details');
  if (fold) fold.open = true; // a closed Advanced can't show what's wrong
  node.focus();
  node.addEventListener('input', () => node.classList.remove('lv-invalid'), { once: true });
  return null;
}

/** A write, then the page's copies of the config and the agent list. */
async function write(/** @type {() => Promise<any>} */ send) {
  const out = await send();
  await loadConfig();
  await refreshAgents();
  return out;
}

export function buildAgentsPane() {
  const pane = el('div', 'lv-cfg-pane');
  /** @type {{ name: 'list' } | { name: 'form', id: string | null }} */
  let view = { name: 'list' };

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
    pane.replaceChildren(view.name === 'list' ? renderList() : renderForm(view.id));
    const ro = readOnlyNotice();
    if (ro) pane.prepend(ro);
  }

  /** @param {() => Promise<any>} send */
  async function act(send) {
    try {
      await write(send);
    } catch (err) {
      toast(String(err.message || err), { type: 'error' });
    }
    render();
  }

  function renderList() {
    const c = cfg();
    const box = el('div');
    const head = el('div', 'lv-cfg-pane-head');
    head.appendChild(el('span', 'lv-cfg-pane-title', t('settings.tabAgents')));
    const add = el('button', 'lv-btn lv-btn-sm', `＋ ${t('agent.add')}`);
    add.setAttribute('type', 'button');
    add.toggleAttribute('disabled', !c.writable);
    add.addEventListener('click', () => {
      view = { name: 'form', id: null };
      render();
    });
    head.appendChild(add);
    box.append(head, el('div', 'lv-cfg-hint lv-skills-intro', t('agent.intro')));

    const list = el('div', 'lv-model-list');
    c.agents.forEach((/** @type {any} */ a, /** @type {number} */ i) => {
      const row = el('div', 'lv-model-row');
      row.setAttribute('role', 'button');
      row.tabIndex = 0;
      const info = el('div', 'lv-model-row-info');
      const name = el('div', 'lv-model-row-name', a.display_name);
      if (i === 0) name.appendChild(el('span', 'lv-badge lv-badge-default', t('cfg.default')));
      const facts = [modelName(c, a), a.effective_mode && modeLabel(a.effective_mode)];
      info.append(name, el('div', 'lv-model-row-meta', facts.filter(Boolean).join(' · ')));
      row.appendChild(info);

      const actions = el('div', 'lv-model-row-actions');
      if (c.writable) {
        actions.appendChild(
          ghostBtn(t('cfg.duplicate'), (e) => {
            e.stopPropagation();
            act(async () => {
              const out = await api.duplicateAgent(a.id);
              view = { name: 'form', id: out.id };
            });
          }),
        );
        if (i > 0) {
          actions.appendChild(
            ghostBtn(t('cfg.makeDefault'), (e) => {
              e.stopPropagation();
              act(() => api.makeDefaultAgent(a.id));
            }),
          );
        }
        if (c.agents.length > 1) {
          const del = ghostBtn(t('cfg.delete'), async (e) => {
            e.stopPropagation();
            if (!(await confirmDialog(t('agent.deleteConfirm', { name: a.display_name })))) return;
            act(() => api.deleteAgent(a.id));
          });
          del.classList.add('lv-danger');
          actions.appendChild(del);
        }
      }
      row.append(actions, el('span', 'lv-model-row-chev', '›'));
      const open = () => {
        view = { name: 'form', id: a.id };
        render();
      };
      row.addEventListener('click', open);
      row.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          open();
        }
      });
      list.appendChild(row);
    });
    box.append(list, scopeFootnote());
    return box;
  }

  /** @param {string | null} id */
  function renderForm(id) {
    const c = cfg();
    const perms = c.permissions;
    // A new agent starts from the defaults the server would give it.
    const a = c.agents.find((/** @type {any} */ x) => x.id === id) || {
      name: null,
      instructions: '',
      model: null,
      mode: perms.modes.includes('coding') ? 'coding' : perms.modes[perms.modes.length - 1],
      auto_allow: [...perms.bundles],
      skills: null,
      search: true,
      memory: true,
      subagents: true,
      max_turns: DEFAULT_MAX_TURNS,
      max_retries: null,
      followups: true,
    };
    const box = el('div');
    const head = el('div', 'lv-cfg-form-head');
    const back = el('button', 'lv-btn-icon lv-cfg-back', '‹');
    back.setAttribute('type', 'button');
    back.setAttribute('aria-label', t('cfg.backLabel'));
    const toList = () => {
      view = { name: 'list' };
      render();
    };
    back.addEventListener('click', toList);
    head.append(back, el('span', 'lv-cfg-pane-title', id ? t('agent.edit') : t('agent.new')));
    box.appendChild(head);
    const form = el('div', 'lv-cfg-form');

    // ---- who it is
    const nameIn = input(a.name || '', { placeholder: id || t('agent.namePlaceholder') });
    form.appendChild(field(t('agent.name'), nameIn));
    const prompt = /** @type {HTMLTextAreaElement} */ (el('textarea', 'lv-dialog-input lv-cfg-input'));
    prompt.rows = 5;
    prompt.value = a.instructions || '';
    prompt.placeholder = t('cfg.instructionsPlaceholder');
    form.appendChild(field(t('cfg.instructions'), prompt, t('cfg.instructionsHint')));

    // ---- what it runs on
    const model = /** @type {HTMLSelectElement} */ (el('select', 'lv-dialog-input'));
    const chat = c.models.find((/** @type {any} */ m) => m.id === c.roles.chat);
    const follow = el(
      'option',
      '',
      chat ? t('agent.modelDefault', { name: chat.display_name || chat.model }) : t('agent.modelNone'),
    );
    follow.setAttribute('value', '');
    model.appendChild(follow);
    for (const p of c.models) {
      const opt = /** @type {HTMLOptionElement} */ (el('option', '', p.display_name || p.model));
      opt.value = p.id;
      model.appendChild(opt);
    }
    model.value = a.model || '';
    form.appendChild(field(t('agent.model'), model, t('agent.modelHint')));

    // ---- permissions (left out when the launch fixed them)
    let mode = a.mode;
    /** @type {Map<string, () => boolean>} */
    const bundles = new Map();
    if (perms.lock) {
      form.appendChild(notice(t(`cfg.wsLock_${perms.lock}`)));
    } else {
      const modeHint = el('div', 'lv-cfg-hint');
      const modeCtl = segmented(
        perms.modes.map((/** @type {string} */ m) => [m, modeLabel(m)]),
        mode,
        (/** @type {string} */ val) => {
          mode = val;
          sync();
        },
      );
      const modeField = field(t('agent.mode'), modeCtl);
      modeField.appendChild(modeHint);
      form.appendChild(modeField);
      const rows = el('div', 'lv-cfg-switches');
      for (const name of perms.bundles) {
        const sw = switchControl(a.auto_allow.includes(name));
        bundles.set(name, sw.value);
        rows.appendChild(switchRow(t(`cfg.bundle_${name}`), sw.node, t(`cfg.bundleHint_${name}`)));
      }
      const bundleField = field(t('cfg.autoAllow'), rows, t('cfg.autoAllowHint'));
      form.appendChild(bundleField);
      // Auto-allowed commands mean something in coding mode only: the other
      // modes never ask (trusted) or never run one (read-only). Hidden there,
      // not greyed — a dimmed list reads as a setting that still applies.
      // Its choices are kept, and saved, either way.
      const sync = () => {
        modeHint.textContent = t('agent.modeHint', { mode: t(`cfg.modeHint_${mode}`) });
        bundleField.hidden = mode !== 'coding';
      };
      sync();
    }

    // ---- switches
    const search = switchControl(a.search);
    const memory = switchControl(a.memory);
    const subagents = switchControl(a.subagents);
    const followups = switchControl(a.followups);
    const switches = el('div', 'lv-cfg-switches');
    switches.append(
      switchRow(t('agent.search'), search.node, t('agent.searchHint')),
      switchRow(t('cfg.memory'), memory.node, t('cfg.memoryHint')),
      switchRow(t('cfg.subagents'), subagents.node, t('cfg.subagentsHint')),
      switchRow(t('cfg.followups'), followups.node, t('cfg.followupsHint')),
    );
    form.appendChild(field(t('agent.abilities'), switches));

    // ---- Advanced: what few change. Open when it holds a change already,
    // so a setting never hides behind a closed fold.
    const adv = /** @type {HTMLDetailsElement} */ (el('details', 'lv-cfg-advanced'));
    adv.appendChild(el('summary', '', t('cfg.advanced')));
    adv.open = a.skills != null || a.max_turns !== DEFAULT_MAX_TURNS || a.max_retries != null;
    form.appendChild(adv);

    // skills: the global directories, or its own list
    let ownSkills = a.skills !== null && a.skills !== undefined;
    const dirsIn = /** @type {HTMLTextAreaElement} */ (el('textarea', 'lv-dialog-input lv-cfg-input lv-mono'));
    dirsIn.rows = 3;
    dirsIn.value = (a.skills || c.skills.dirs).join('\n');
    dirsIn.placeholder = t('cfg.skillsPathPlaceholder');
    const skillsSeg = segmented(
      [
        ['global', t('agent.skillsGlobal')],
        ['own', t('agent.skillsOwn')],
      ],
      ownSkills ? 'own' : 'global',
      (/** @type {string} */ val) => {
        ownSkills = val === 'own';
        dirsIn.hidden = !ownSkills;
      },
    );
    dirsIn.hidden = !ownSkills;
    const skillsField = field(t('settings.tabSkills'), skillsSeg, t('agent.skillsHint'));
    skillsField.appendChild(dirsIn);
    adv.appendChild(skillsField);

    // run limits
    const turns = input(String(a.max_turns));
    turns.inputMode = 'numeric';
    adv.appendChild(field(t('cfg.maxTurns'), turns, t('cfg.maxTurnsHint')));
    // Empty means the core default: say how many that is.
    const retries = input(a.max_retries == null ? '' : String(a.max_retries), {
      placeholder: t('cfg.maxRetriesAuto', { n: c.defaults?.max_retries ?? 4 }),
    });
    retries.inputMode = 'numeric';
    adv.appendChild(field(t('cfg.maxRetries'), retries, t('cfg.maxRetriesHint')));

    if (!c.writable) {
      for (const ctl of form.querySelectorAll('input, textarea, select, button')) {
        ctl.toggleAttribute('disabled', true);
      }
    }

    /** The form as a request body, or null when a field is invalid. */
    function read() {
      const maxTurns = positiveInt(turns.value);
      if (maxTurns === undefined) return invalid(turns);
      let maxRetries = null;
      if (retries.value.trim()) {
        const n = Number(retries.value.trim());
        if (!Number.isInteger(n) || n < 0) return invalid(retries);
        maxRetries = n;
      }
      const dirs = dirsIn.value.split('\n').map((d) => d.trim()).filter(Boolean);
      /** @type {Record<string, unknown>} */
      const body = {
        name: nameIn.value.trim() || null,
        instructions: prompt.value,
        model: model.value || null,
        skills: ownSkills ? dirs : null,
        search: search.value(),
        memory: memory.value(),
        subagents: subagents.value(),
        followups: followups.value(),
        max_turns: maxTurns,
        max_retries: maxRetries,
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
    const cancel = el('button', 'lv-btn lv-btn-ghost', t('cfg.cancel'));
    cancel.setAttribute('type', 'button');
    cancel.addEventListener('click', toList);
    const save = el('button', 'lv-btn lv-btn-primary', t('cfg.save'));
    save.setAttribute('type', 'button');
    save.toggleAttribute('disabled', !c.writable);
    save.addEventListener('click', async () => {
      const body = read();
      if (body === null) return;
      save.disabled = true;
      errBox.hidden = true;
      try {
        await write(() => (id ? api.updateAgent(id, body) : api.createAgent(body)));
        toast(t('cfg.savedNextRun'));
        toList();
      } catch (err) {
        errBox.textContent = String(err.message || err);
        errBox.hidden = false;
        save.disabled = false;
      }
    });
    actions.append(cancel, save);
    if (c.writable) actions.appendChild(el('div', 'lv-cfg-footnote', t('cfg.appliesNextRun')));
    box.append(form, errBox, actions);
    return box;
  }

  render();
  return pane;
}
