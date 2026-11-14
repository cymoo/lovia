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
import { icon } from './icons.js';
import {
  backButton,
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
      mcp: true,
      memory: true,
      subagents: true,
      max_turns: DEFAULT_MAX_TURNS,
      max_retries: null,
      followups: true,
    };
    const box = el('div');
    const head = el('div', 'lv-cfg-form-head');
    const back = backButton();
    const toList = () => {
      view = { name: 'list' };
      render();
    };
    back.addEventListener('click', toList);
    head.append(back, el('span', 'lv-cfg-pane-title', id ? t('agent.edit') : t('agent.new')));
    box.appendChild(head);
    // Sections divided by a rule, switch lists on a card: the schedule
    // form's layout.
    const form = el('div', 'lv-cfg-form lv-agent-form');
    const section = () => form.appendChild(el('section', 'lv-agent-section'));
    /** A grey card around a mode note or a list of switch rows. @param {HTMLElement[]} rows */
    const card = (...rows) => {
      const c = el('div', 'lv-agent-card');
      c.append(...rows);
      return c;
    };

    // ---- who it is
    const who = section();
    const nameIn = input(a.name || '', { placeholder: id || t('agent.namePlaceholder') });
    who.appendChild(field(t('agent.name'), nameIn));
    const prompt = /** @type {HTMLTextAreaElement} */ (el('textarea', 'lv-dialog-input lv-cfg-input'));
    prompt.rows = 5;
    prompt.value = a.instructions || '';
    prompt.placeholder = t('cfg.instructionsPlaceholder');
    who.appendChild(field(t('cfg.instructions'), prompt, t('cfg.instructionsHint')));

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
    const runs = section();
    runs.appendChild(field(t('agent.model'), model, t('agent.modelHint')));

    // ---- permissions (left out when the launch fixed them): the mode's
    // tabs over a card that says what it allows
    let mode = a.mode;
    /** @type {Map<string, () => boolean>} */
    const bundles = new Map();
    if (perms.lock) {
      runs.appendChild(notice(t(`cfg.wsLock_${perms.lock}`)));
    } else {
      const modeHint = el('div', 'lv-agent-card-note');
      const modeCtl = segmented(
        perms.modes.map((/** @type {string} */ m) => [m, modeLabel(m)]),
        mode,
        (/** @type {string} */ val) => {
          mode = val;
          sync();
        },
      );
      const group = el('div', 'lv-agent-card-group');
      group.setAttribute('role', 'group');
      group.setAttribute('aria-label', t('cfg.autoAllow'));
      group.appendChild(el('div', 'lv-agent-card-head', t('cfg.autoAllow')));
      for (const name of perms.bundles) {
        const sw = switchControl(a.auto_allow.includes(name));
        bundles.set(name, sw.value);
        group.appendChild(switchRow(t(`cfg.bundle_${name}`), sw.node, t(`cfg.bundleHint_${name}`)));
      }
      const modeField = field(t('agent.mode'), modeCtl);
      modeField.appendChild(card(modeHint, group));
      runs.appendChild(modeField);
      // Auto-allowed commands mean something in coding mode only: the other
      // modes never ask (trusted) or never run one (read-only). Hidden there,
      // not greyed — a dimmed list reads as a setting that still applies.
      // Its choices are kept, and saved, either way.
      const sync = () => {
        modeHint.textContent = t('agent.modeHint', { mode: t(`cfg.modeHint_${mode}`) });
        group.hidden = mode !== 'coding';
      };
      sync();
    }

    // ---- switches
    const search = switchControl(a.search);
    const mcp = switchControl(a.mcp);
    const memory = switchControl(a.memory);
    const subagents = switchControl(a.subagents);
    const followups = switchControl(a.followups);
    const servers = c.mcp?.servers || [];
    const enabled = servers.filter((/** @type {any} */ s) => s.enabled).length;
    const abilities = card(
      switchRow(t('agent.search'), search.node, t('agent.searchHint')),
      switchRow(
        t('agent.mcp'),
        mcp.node,
        servers.length ? t('agent.mcpHint', { n: enabled }) : t('agent.mcpHintNone'),
      ),
      switchRow(t('cfg.memory'), memory.node, t('cfg.memoryHint')),
      switchRow(t('cfg.subagents'), subagents.node, t('cfg.subagentsHint')),
      switchRow(t('cfg.followups'), followups.node, t('cfg.followupsHint')),
    );
    section().appendChild(field(t('agent.abilities'), abilities));

    // ---- Advanced: what few change. Folded, its row says what is set; open
    // when that is a change already, so a setting never hides behind a fold.
    const adv = /** @type {HTMLDetailsElement} */ (el('details', 'lv-agent-advanced'));
    const summary = el('summary');
    summary.innerHTML = icon('chevron-right', { size: 14 });
    const summaryText = el('span', 'lv-agent-advanced-set');
    summary.append(el('span', 'lv-agent-advanced-title', t('cfg.advanced')), summaryText);
    adv.appendChild(summary);
    adv.open = a.skills != null || a.max_turns !== DEFAULT_MAX_TURNS || a.max_retries != null;
    const advBody = el('div', 'lv-agent-advanced-body');
    adv.appendChild(advBody);
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
        drawSummary();
      },
    );
    dirsIn.hidden = !ownSkills;
    const skillsField = field(t('settings.tabSkills'), skillsSeg, t('agent.skillsHint'));
    skillsField.appendChild(dirsIn);
    advBody.appendChild(skillsField);

    // run limits, side by side
    const turns = input(String(a.max_turns));
    turns.inputMode = 'numeric';
    // Empty means the core default: say how many that is.
    const defaultRetries = c.defaults?.max_retries ?? 4;
    const retries = input(a.max_retries == null ? '' : String(a.max_retries), {
      placeholder: t('cfg.maxRetriesAuto', { n: defaultRetries }),
    });
    retries.inputMode = 'numeric';
    const limits = el('div', 'lv-agent-pair');
    limits.append(
      field(t('cfg.maxTurns'), turns, t('cfg.maxTurnsHint')),
      field(t('cfg.maxRetries'), retries, t('cfg.maxRetriesHint')),
    );
    advBody.appendChild(limits);

    function drawSummary() {
      const dirs = dirsIn.value.split('\n').filter((d) => d.trim()).length;
      const parts = [ownSkills ? t('agent.skillsOwnShort', { n: dirs }) : t('agent.skillsGlobal')];
      if (turns.value.trim()) parts.push(t('agent.turnsShort', { n: turns.value.trim() }));
      parts.push(t('agent.retriesShort', { n: retries.value.trim() || defaultRetries }));
      summaryText.textContent = parts.join(' · ');
    }
    for (const n of [dirsIn, turns, retries]) n.addEventListener('input', drawSummary);
    drawSummary();

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
        mcp: mcp.value(),
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
