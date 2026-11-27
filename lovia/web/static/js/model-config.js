// model-config.js — the Settings "Models"/"Search"/"About" panes, the composer
// model chip, and the first-run setup hero. All of it exists only when
// /api/info reports features.settings (the CLI's default agent); embedder
// servers render none of this.
//
// Data flow: GET /api/config is cached on store.modelConfig (keys arrive
// masked — this file never holds a secret). Every write goes through
// api.* → the server validates, persists and hot-swaps the served agent →
// a config_changed event lands on /api/events → _onConfigChanged refreshes
// the cache, the chip and the agent list in every open tab.
import { api } from './api.js';
import { store } from './store.js';
import { t } from './i18n.js';
import { toast } from './toast.js';
import { confirmDialog, segmented } from './ui.js';
import { icon } from './icons.js';

/** Fetch (or refresh) the masked config document into store.modelConfig. */
export async function loadConfig() {
  store.modelConfig = await api.getConfig();
  store.emit('config-loaded', store.modelConfig);
  return store.modelConfig;
}

export function cfg() {
  return store.modelConfig;
}

// ---- the chat's own model -------------------------------------------------
// A chat may run on a model other than the default: picked on the chip for a
// new chat (it goes out with the first message), switched there for an open
// one (a PATCH; the next run is on it). null: the default, whatever it is
// then — so a chat that never picked one follows a change of default.

/** The new chat's pick. @type {string | null} */
let _draftModel = null;
/**
 * The open chat's pick; undefined until its row or detail says (shown as
 * the default meanwhile, but a pick then is always sent).
 * @type {string | null | undefined}
 */
let _chatModel = null;
/** The chat `_chatModel` is of; null: a new chat. @type {string | null} */
let _modelOf = null;

/** The id the composer's chat picked, if it is still a configured model. */
function pickedModel() {
  const id = store.sessionId ? _chatModel : _draftModel;
  return id && cfg()?.models.some((/** @type {any} */ m) => m.id === id) ? id : null;
}

/** The model the composer's chat runs on: its own pick, else the default. */
function activeProfile() {
  const c = cfg();
  if (!c) return null;
  const id = pickedModel() ?? c.roles.chat;
  return c.models.find((/** @type {any} */ m) => m.id === id) || null;
}

/**
 * The model a new chat's first message asks for; undefined for an open chat
 * (its own applies) or the default. Read by the composer's send.
 */
export function draftChatModel() {
  return store.sessionId ? undefined : pickedModel() ?? undefined;
}

/**
 * The composer's chat's own model, when it picked one that isn't the
 * default — its name and context window, for the context ring; else null
 * (the agent's own apply).
 * @returns {{ model: string, window: number | null } | null}
 */
export function chatModelInfo() {
  const id = pickedModel();
  if (!id || id === cfg()?.roles.chat) return null;
  const p = activeProfile();
  return p ? { model: p.model, window: p.window ?? null } : null;
}

/**
 * Whether the model the chat picked for itself sees images; null when it
 * runs on the default (or the server has no model settings) — the agent's
 * own `vision` answers then. Read by the composer's attach.
 * @returns {boolean | null}
 */
export function chatModelSeesImages() {
  const id = pickedModel();
  if (!id || id === cfg()?.roles.chat) return null;
  const p = activeProfile();
  return p ? !!p.sees_images : null;
}

/** The open chat changed, or what is known of it did. */
function syncChatModel(/** @type {string | null} */ id, /** @type {any} */ row) {
  const wasNew = _modelOf === null;
  const before = pickedModel();
  _modelOf = id;
  if (!id) _draftModel = null;
  else if (row) _chatModel = row.model ?? null;
  // A new chat that just got its id was saved with the draft's pick.
  else if (wasNew && store.streaming) _chatModel = _draftModel;
  else _chatModel = undefined;
  updateChip();
  if (pickedModel() !== before) store.emit('chat-model-changed');
}

function profileName(p) {
  return p?.display_name || p?.model || '';
}

/** The display host of a base URL; tolerant of scheme-less values. */
function hostOf(baseUrl) {
  if (!baseUrl) return '';
  try {
    return new URL(baseUrl).host;
  } catch {
    return baseUrl.replace(/^[a-z+.-]+:\/\//i, '').split('/')[0];
  }
}

function flavorDefault(flavor) {
  const d = cfg()?.defaults;
  if (!d) return '';
  return flavor === 'anthropic' ? d.anthropic_base_url : d.openai_base_url;
}

// ---- small DOM helpers ----------------------------------------------------

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** A form's way back to its list: the head's leading chevron. */
export function backButton() {
  const back = el('button', 'lv-btn-icon lv-cfg-back');
  back.setAttribute('type', 'button');
  back.setAttribute('aria-label', t('cfg.backLabel'));
  back.innerHTML = icon('chevron-left', { size: 16 });
  return back;
}

/** A plain note above a settings form. */
export function notice(text) {
  const box = el('div', 'lv-cfg-notice', text);
  box.setAttribute('role', 'note');
  return box;
}

/**
 * The "config.json isn't writable" note, or null when it is. The server
 * refuses every write then (409 config_read_only); this says so up front,
 * before someone fills in a form that can't be saved.
 */
export function readOnlyNotice() {
  const c = cfg();
  return c && c.writable === false ? notice(t('cfg.readOnly', { file: c.scope.label })) : null;
}

let _fieldIds = 0;

export function field(labelText, control, hintText) {
  const wrap = el('div', 'lv-cfg-field');
  const label = /** @type {HTMLLabelElement} */ (el('label', 'lv-cfg-label', labelText));
  // Name a lone form control by its label (screen readers, and a click on
  // the label focuses it); composite controls keep their own naming.
  if (control.matches('input, textarea, select')) {
    if (!control.id) control.id = `cfg-field-${++_fieldIds}`;
    label.htmlFor = control.id;
  }
  // A <label> names one control; a radio group takes its name as aria-label.
  if (control.getAttribute('role') === 'radiogroup') control.setAttribute('aria-label', labelText);
  wrap.append(label, control);
  if (hintText) wrap.appendChild(el('div', 'lv-cfg-hint', hintText));
  return wrap;
}

export function input(value, { mono = false, placeholder = '', type = 'text' } = {}) {
  const node = /** @type {HTMLInputElement} */ (el('input', 'lv-dialog-input lv-cfg-input'));
  node.type = type;
  node.value = value ?? '';
  node.placeholder = placeholder;
  if (mono) node.classList.add('lv-mono');
  return node;
}

/**
 * A positive integer typed by a person ("128,000" and "128 000" included),
 * or undefined for an empty or unusable value.
 * @param {string} raw
 */
export function positiveInt(raw) {
  const text = raw.trim();
  if (!text) return undefined;
  const n = parseInt(text.replace(/[,\s]/g, ''), 10);
  return Number.isFinite(n) && n > 0 ? n : undefined;
}

export function ghostBtn(label, onClick) {
  const btn = el('button', 'lv-btn lv-btn-ghost lv-btn-sm', label);
  btn.setAttribute('type', 'button');
  btn.addEventListener('click', onClick);
  return btn;
}

/**
 * Common request shapes per API flavor, offered as chips. Each chip writes
 * its object into the JSON below — nothing here is a capability claim, and
 * the endpoint's docs decide which apply. Kept to the two things people
 * reach for (effort, thinking on/off) plus lovia's own Anthropic caching knobs.
 * @type {Record<string, [string, object][]>}
 */
const EXTRA_BODY_SNIPPETS = {
  openai: [
    ['reasoning_effort', { reasoning_effort: 'medium' }],
    ['chat_template_kwargs', { chat_template_kwargs: { enable_thinking: false } }],
    ['thinking', { thinking: { type: 'disabled' } }],
  ],
  anthropic: [
    ['output_config.effort', { output_config: { effort: 'medium' } }],
    ['thinking', { thinking: { type: 'adaptive' } }],
    ['cache_system', { cache_system: true }],
    ['cache_ttl', { cache_ttl: '1h' }],
  ],
};

const isPlainObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

/**
 * Set a snippet's leaves into `target`, keeping sibling keys of any nested
 * object it touches (so adding `enable_thinking` under `chat_template_kwargs`
 * doesn't drop a `reasoning_effort` already there).
 */
function setLeaves(target, snippet) {
  for (const [key, value] of Object.entries(snippet)) {
    if (isPlainObject(value) && isPlainObject(target[key])) {
      setLeaves(target[key], value);
    } else {
      target[key] = value;
    }
  }
  return target;
}

/**
 * The "extra request fields" editor: a JSON textarea plus snippet chips.
 * `read()` returns the parsed object, `{}` for blank, or null (after marking
 * the field invalid) when the text isn't a JSON object.
 * @param {object | undefined} initial
 * @param {() => string} flavor Current API flavor, read at click time.
 */
function extraBodyEditor(initial, flavor) {
  const node = el('div', 'lv-cfg-extra');
  const area = /** @type {HTMLTextAreaElement} */ (el('textarea', 'lv-dialog-input lv-cfg-input lv-mono'));
  area.spellcheck = false;
  area.placeholder = '{ }';
  const hasInitial = initial && Object.keys(initial).length > 0;
  area.value = hasInitial ? JSON.stringify(initial, null, 2) : '';
  // Grow with the content (a pretty-printed object is one key per line),
  // within a band that keeps the form scannable.
  const fit = () => {
    area.rows = Math.min(14, Math.max(3, area.value.split('\n').length + 1));
  };
  fit();
  const err = el('div', 'lv-cfg-hint lv-cfg-extra-error');
  err.hidden = true;

  /** @returns {object | null} */
  const parse = () => {
    const raw = area.value.trim();
    if (!raw) return {};
    try {
      const value = JSON.parse(raw);
      if (isPlainObject(value)) return value;
    } catch {
      /* reported below */
    }
    return null;
  };
  const validate = () => {
    const ok = parse() !== null;
    area.classList.toggle('lv-invalid', !ok);
    err.hidden = ok;
    err.textContent = ok ? '' : t('cfg.extraBodyInvalid');
    return ok;
  };
  area.addEventListener('input', () => {
    fit();
    validate();
  });

  const chips = el('div', 'lv-cfg-extra-chips');
  const renderChips = () => {
    chips.replaceChildren();
    for (const [label, snippet] of EXTRA_BODY_SNIPPETS[flavor()] || []) {
      const chip = el('button', 'lv-chip', label);
      chip.setAttribute('type', 'button');
      chip.addEventListener('click', () => {
        // Adding to broken JSON would silently discard it — fix that first.
        if (!validate()) return;
        const merged = setLeaves(parse() || {}, structuredClone(snippet));
        area.value = JSON.stringify(merged, null, 2);
        fit();
        validate();
        area.focus();
      });
      chips.appendChild(chip);
    }
  };
  renderChips();
  node.append(area, err, chips);
  return {
    node,
    read: () => (validate() ? parse() : null),
    focus: () => area.focus(),
    // The chips follow the flavor control; the JSON itself is left alone.
    syncFlavor: renderChips,
  };
}

// ---- Models pane ----------------------------------------------------------

/**
 * The Settings → Models pane: profile list ⇄ edit form, plus role assignment.
 * Owns its internal rerenders; safe to rebuild wholesale on tab switches.
 */
export function buildModelsPane() {
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

  function renderList() {
    const c = cfg();
    const box = el('div');

    const head = el('div', 'lv-cfg-pane-head');
    head.appendChild(el('span', 'lv-cfg-pane-title', t('cfg.models')));
    const add = el('button', 'lv-btn lv-btn-sm', `＋ ${t('cfg.addModel')}`);
    add.setAttribute('type', 'button');
    add.addEventListener('click', () => {
      view = { name: 'form', id: null };
      render();
    });
    head.appendChild(add);
    box.appendChild(head);

    const list = el('div', 'lv-model-list');
    if (!c.models.length) list.appendChild(el('div', 'lv-cfg-hint', t('cfg.emptyList')));
    for (const p of c.models) {
      const row = el('div', 'lv-model-row');
      row.setAttribute('role', 'button');
      row.tabIndex = 0;
      const info = el('div', 'lv-model-row-info');
      const name = el('div', 'lv-model-row-name', profileName(p));
      if (p.id === c.roles.chat) {
        name.appendChild(el('span', 'lv-badge lv-badge-default', t('cfg.default')));
      }
      if (p.vision === 'on') {
        name.appendChild(el('span', 'lv-badge lv-badge-plain', t('cfg.visionBadge')));
      }
      const host = hostOf(p.base_url);
      info.append(name, el('div', 'lv-model-row-meta', host ? `${p.model} · ${host}` : p.model));
      row.appendChild(info);

      const actions = el('div', 'lv-model-row-actions');
      // A server-side copy (the key never reaches the page): the way to keep
      // a second parameter set — say, thinking off for aux — on one endpoint.
      actions.appendChild(
        ghostBtn(t('cfg.duplicate'), async (e) => {
          e.stopPropagation();
          try {
            const out = await api.duplicateModel(p.id);
            await loadConfig();
            view = { name: 'form', id: out.id };
          } catch (err) {
            toast(String(err.message || err), { type: 'error' });
          }
          render();
        }),
      );
      if (p.id !== c.roles.chat) {
        actions.appendChild(
          ghostBtn(t('cfg.makeDefault'), async (e) => {
            e.stopPropagation();
            try {
              await makeDefault(p.id);
            } catch (err) {
              toast(String(err.message || err), { type: 'error' });
            }
            render();
          }),
        );
        const del = ghostBtn(t('cfg.delete'), async (e) => {
          e.stopPropagation();
          if (!(await confirmDialog(t('cfg.deleteConfirm', { name: profileName(p) })))) return;
          try {
            await api.deleteModel(p.id);
            await loadConfig();
          } catch (err) {
            toast(String(err.message || err), { type: 'error' });
          }
          render();
        });
        del.classList.add('lv-danger');
        actions.appendChild(del);
      }
      row.appendChild(actions);
      row.appendChild(el('span', 'lv-model-row-chev', '›'));
      const open = () => {
        view = { name: 'form', id: p.id };
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
    }
    box.appendChild(list);

    if (c.models.length) box.appendChild(renderRoles());
    return box;
  }

  function renderRoles() {
    const c = cfg();
    const box = el('div', 'lv-cfg-roles');
    box.appendChild(el('div', 'lv-cfg-pane-title', t('cfg.roles')));

    const roleRow = (labelText, current, emptyLabel, role, hint) => {
      const select = /** @type {HTMLSelectElement} */ (
        el('select', 'lv-dialog-input lv-cfg-role-select')
      );
      const none = el('option', '', emptyLabel);
      none.setAttribute('value', '');
      select.appendChild(none);
      for (const p of c.models) {
        const opt = /** @type {HTMLOptionElement} */ (el('option', '', profileName(p)));
        opt.value = p.id;
        select.appendChild(opt);
      }
      select.value = current || '';
      select.addEventListener('change', async () => {
        try {
          await api.setRoles({ [role]: select.value || null });
          await loadConfig();
        } catch (err) {
          toast(String(err.message || err), { type: 'error' });
          select.value = cfg()?.roles?.[role] || '';
        }
      });
      const row = el('div', 'lv-cfg-role-row');
      row.appendChild(el('label', 'lv-cfg-role-label', labelText));
      row.appendChild(select);
      row.appendChild(el('span', 'lv-cfg-role-hint', hint));
      return row;
    };

    box.appendChild(
      roleRow(t('cfg.roleVision'), c.roles.vision, t('cfg.roleNone'), 'vision', t('cfg.roleVisionHint')),
    );
    box.appendChild(
      roleRow(t('cfg.roleAux'), c.roles.aux, t('cfg.roleFollow'), 'aux', t('cfg.roleAuxHint')),
    );
    return box;
  }

  function renderForm(id) {
    const c = cfg();
    const stored = id ? c.models.find((m) => m.id === id) : null;
    const box = el('div');

    const head = el('div', 'lv-cfg-form-head');
    const back = backButton();
    back.addEventListener('click', () => {
      view = { name: 'list' };
      render();
    });
    head.append(back, el('span', 'lv-cfg-pane-title', stored ? t('cfg.editModel') : t('cfg.newModel')));
    box.appendChild(head);

    const form = el('div', 'lv-cfg-form');

    const nameIn = input(stored?.name || '', { placeholder: stored?.model || '' });
    form.appendChild(field(t('cfg.name'), nameIn, t('cfg.nameHint')));

    let flavor = stored?.flavor || 'openai';
    const urlIn = input(stored?.base_url || '', {
      mono: true,
      placeholder: flavorDefault(flavor),
    });
    const flavorSeg = segmented(
      [
        ['openai', t('cfg.flavorOpenAI')],
        ['anthropic', t('cfg.flavorAnthropic')],
      ],
      flavor,
      (val) => {
        flavor = val;
        urlIn.placeholder = flavorDefault(flavor);
        extras.syncFlavor();
      },
    );
    form.appendChild(field(t('cfg.flavor'), flavorSeg));
    form.appendChild(field(t('cfg.baseUrl'), urlIn));

    // API key: never echoed. keep (leave stored) / edit (replace) / clear.
    /** @type {'keep' | 'edit' | 'clear'} */
    let keyMode = stored?.api_key?.set ? 'keep' : 'edit';
    const keyIn = input('', { type: 'password', placeholder: 'sk-xxx' });
    const keyBox = el('div', 'lv-cfg-key');
    const renderKey = () => {
      keyBox.replaceChildren();
      if (keyMode === 'keep') {
        const row = el('div', 'lv-cfg-key-set');
        row.appendChild(el('span', 'lv-cfg-key-dot'));
        row.appendChild(
          el('span', 'lv-cfg-key-mask', t('cfg.apiKeySet', { hint: stored.api_key.hint || '' })),
        );
        row.appendChild(
          ghostBtn(t('cfg.replace'), () => {
            keyMode = 'edit';
            renderKey();
            keyIn.focus();
          }),
        );
        row.appendChild(
          ghostBtn(t('cfg.clear'), () => {
            keyMode = 'clear';
            renderKey();
          }),
        );
        keyBox.appendChild(row);
      } else if (keyMode === 'clear') {
        const row = el('div', 'lv-cfg-key-set');
        row.appendChild(el('span', 'lv-cfg-key-mask', t('cfg.apiKeyNone')));
        if (stored?.api_key?.set) {
          row.appendChild(
            ghostBtn(t('cfg.keep'), () => {
              keyMode = 'keep';
              renderKey();
            }),
          );
        }
        row.appendChild(
          ghostBtn(t('cfg.replace'), () => {
            keyMode = 'edit';
            renderKey();
            keyIn.focus();
          }),
        );
        keyBox.appendChild(row);
      } else {
        keyBox.appendChild(keyIn);
        if (stored?.api_key?.set) {
          keyBox.appendChild(
            ghostBtn(t('cfg.keep'), () => {
              keyMode = 'keep';
              keyIn.value = '';
              renderKey();
            }),
          );
        }
      }
    };
    form.appendChild(field(t('cfg.apiKey'), keyBox));

    const modelIn = input(stored?.model || '', { mono: true, placeholder: 'deepseek-v4-pro' });
    form.appendChild(field(t('cfg.modelId'), modelIn));

    // Advanced: context window + vision capability.
    const adv = /** @type {HTMLDetailsElement} */ (el('details', 'lv-cfg-advanced'));
    adv.appendChild(el('summary', '', t('cfg.advanced')));
    const windowIn = input(stored?.context_window ? String(stored.context_window) : '', {
      placeholder: t('cfg.contextWindowAuto'),
    });
    windowIn.inputMode = 'numeric';
    adv.appendChild(field(t('cfg.contextWindow'), windowIn));
    const maxTokensIn = input(stored?.max_tokens ? String(stored.max_tokens) : '', {
      placeholder: t('cfg.maxTokensAuto'),
    });
    maxTokensIn.inputMode = 'numeric';
    adv.appendChild(field(t('cfg.maxTokens'), maxTokensIn, t('cfg.maxTokensHint')));
    let vision = stored?.vision || 'auto';
    adv.appendChild(
      field(
        t('cfg.vision'),
        segmented(
          [
            ['auto', t('cfg.visionAuto')],
            ['on', t('cfg.visionOn')],
            ['off', t('cfg.visionOff')],
          ],
          vision,
          (val) => {
            vision = val;
          },
        ),
        t('cfg.visionHint'),
      ),
    );
    // Through the system proxy or not; null follows Settings → General.
    /** @type {boolean | null} */
    let trustEnv = stored?.trust_env ?? null;
    const proxyKey = () => (trustEnv === null ? 'default' : trustEnv ? 'on' : 'off');
    adv.appendChild(
      field(
        t('cfg.proxy'),
        segmented(
          [
            ['default', t('cfg.proxyDefault')],
            ['off', t('cfg.proxyOff')],
            ['on', t('cfg.proxyOn')],
          ],
          proxyKey(),
          (val) => {
            trustEnv = val === 'default' ? null : val === 'on';
          },
        ),
        t('cfg.proxyHint'),
      ),
    );
    // Extra request fields: raw JSON merged into every request to this
    // endpoint, in the endpoint's own dialect. The textarea is the truth;
    // the chips just write common shapes into it.
    const extras = extraBodyEditor(stored?.extra_body, () => flavor);
    adv.appendChild(field(t('cfg.extraBody'), extras.node, t('cfg.extraBodyHint')));
    const hasExtras = stored && Object.keys(stored.extra_body || {}).length > 0;
    if (
      stored?.context_window ||
      stored?.max_tokens ||
      (stored && stored.vision !== 'auto') ||
      trustEnv !== null ||
      hasExtras
    ) {
      adv.open = true;
    }
    form.appendChild(adv);

    // Test connection — the wizard's probe over HTTP, same classifications.
    const testRow = el('div', 'lv-cfg-test-row');
    const testBtn = el('button', 'lv-btn lv-cfg-test-btn', t('cfg.test'));
    testBtn.setAttribute('type', 'button');
    const testOut = el('span', 'lv-cfg-test-result');
    testBtn.addEventListener('click', async () => {
      if (!modelIn.value.trim()) return;
      testBtn.disabled = true;
      testOut.className = 'lv-cfg-test-result';
      testOut.textContent = t('cfg.testing');
      try {
        const res = await probe();
        const labels = {
          ok: t('cfg.testOk'),
          auth_failed: t('cfg.testAuth'),
          unreachable: t('cfg.testUnreachable'),
          unverifiable: t('cfg.testUnverifiable'),
        };
        const good = res.outcome === 'ok' || res.outcome === 'unverifiable';
        testOut.classList.add(good ? 'lv-ok' : 'lv-bad');
        const bits = [`${good ? '✓' : '✗'} ${labels[res.outcome]} (${res.detail})`];
        if (res.context_window) {
          bits.push(t('cfg.testWindow', { n: res.context_window.toLocaleString() }));
        }
        testOut.textContent = bits.join(' · ');
        if (res.note) testOut.title = res.note;
      } catch (err) {
        testOut.classList.add('lv-bad');
        testOut.textContent = `✗ ${String(err.message || err)}`;
      } finally {
        syncProbeButtons();
      }
    });
    testRow.append(testBtn, testOut);
    form.appendChild(testRow);

    // A probe without a model ID can't mean anything — keep the button dead
    // until it can.
    function syncProbeButtons() {
      testBtn.disabled = !modelIn.value.trim();
      testBtn.title = testBtn.disabled ? t('cfg.testNeedsModel') : '';
    }

    function probe() {
      const body = {
        model: modelIn.value.trim() || (stored ? stored.model : 'probe'),
        flavor,
        base_url: urlIn.value.trim() || null,
        context_window: parseWindow() ?? null,
        trust_env: trustEnv,
      };
      if (keyMode === 'edit' && keyIn.value) {
        body.api_key = keyIn.value;
      } else if (keyMode === 'keep' && stored) {
        body.profile_id = stored.id; // reuse the stored key server-side
      }
      return api.testConnection(body);
    }

    function parseWindow() {
      return positiveInt(windowIn.value);
    }

    renderKey();
    modelIn.addEventListener('input', syncProbeButtons);
    syncProbeButtons();

    const errBox = el('div', 'lv-cfg-error');
    errBox.hidden = true;

    const actions = el('div', 'lv-cfg-form-actions');
    const cancel = el('button', 'lv-btn lv-btn-ghost', t('cfg.cancel'));
    cancel.setAttribute('type', 'button');
    cancel.addEventListener('click', () => {
      view = { name: 'list' };
      render();
    });
    const save = el('button', 'lv-btn lv-btn-primary', t('cfg.save'));
    save.setAttribute('type', 'button');
    save.addEventListener('click', async () => {
      const extraBody = extras.read();
      if (extraBody === null) {
        adv.open = true;
        extras.focus();
        return;
      }
      const body = {
        name: nameIn.value.trim() || null,
        model: modelIn.value.trim(),
        flavor,
        base_url: urlIn.value.trim() || null,
        context_window: parseWindow() ?? null,
        max_tokens: positiveInt(maxTokensIn.value) ?? null,
        vision,
        extra_body: extraBody,
        trust_env: trustEnv,
      };
      if (keyMode === 'clear') body.api_key = '';
      else if (keyMode === 'edit' && keyIn.value) body.api_key = keyIn.value;
      // keep -> api_key omitted (null): the server retains the stored key.
      save.disabled = true;
      errBox.hidden = true;
      const firstSetup = !store.configured;
      try {
        if (stored) await api.updateModel(stored.id, body);
        else await api.createModel(body);
        await loadConfig();
        toast(t('cfg.saved'));
        if (firstSetup) {
          // The server just brought the default agent up — reboot the page
          // into the normal chat surface.
          location.reload();
          return;
        }
        view = { name: 'list' };
        render();
      } catch (err) {
        errBox.textContent = String(err.message || err);
        errBox.hidden = false;
      } finally {
        save.disabled = false;
      }
    });
    actions.append(cancel, save);

    box.append(form, errBox, actions);
    return box;
  }

  render();
  return pane;
}

// ---- Search pane ----------------------------------------------------------

// The hosted search services (lovia/web/search.py), in the order "auto"
// asks them — the strip's order too.
/** @type {[string, string][]} */
const SEARCH_SERVICES = [
  ['parallel', 'Parallel'],
  ['exa', 'Exa'],
  ['tavily', 'Tavily'],
];

/**
 * Settings → Web search: which service `web_search` asks — "auto", one of
 * them, or none — and, folded away, a service's optional key and the proxy
 * route. A choice is saved as it is made and tested at once (a connection,
 * no search spent); a key is saved with its own button.
 */
export function buildSearchPane() {
  const pane = el('div', 'lv-cfg-pane');

  const draw = async () => {
    if (!cfg()) {
      try {
        await loadConfig();
      } catch (err) {
        pane.replaceChildren(el('div', 'lv-cfg-error', String(err.message || err)));
        return;
      }
    }
    const saved = () => cfg().search;
    pane.replaceChildren();
    pane.appendChild(el('div', 'lv-cfg-pane-title', t('cfg.searchTitle')));
    const ro = readOnlyNotice();
    if (ro) pane.appendChild(ro);
    const form = el('div', 'lv-cfg-form');

    /** @type {string} */
    let provider = saved().provider;
    /** The picked service's saved key: { set, hint }, or none. */
    const savedKey = () => saved().keys[provider] || { set: false, hint: null };
    const single = () => SEARCH_SERVICES.some(([id]) => id === provider);

    // One save at a time, in the order made; a failed one says why and puts
    // the pane back as saved.
    let queue = Promise.resolve();
    /** @param {{ provider?: string, trust_env?: boolean | null, api_key?: string }} body */
    const apply = (body) => {
      queue = queue.then(async () => {
        try {
          await api.setSearch(body);
          await loadConfig();
          void test();
        } catch (err) {
          toast(String(err.message || err), { type: 'error' });
          await loadConfig().catch(() => {});
          void draw();
        }
      });
      return queue;
    };

    const desc = el('div', 'lv-cfg-hint lv-search-desc');
    // The status line, and under it (not in its row) why a service failed.
    const status = el('div');
    /** @param {string} state @param {string} label */
    const statusLine = (state, label) => {
      const line = el('div', `lv-mcp-status lv-mcp-status-${state}`);
      line.append(el('span', 'lv-mcp-dot'), el('span', '', label));
      return line;
    };
    const describeService = () => {
      const name = SEARCH_SERVICES.find(([id]) => id === provider)?.[1];
      if (provider === 'auto') desc.textContent = t('search.about.auto');
      else if (name) desc.textContent = t(`search.about.${provider}`) + t('search.sentTo', { name });
      else desc.textContent = t('search.offHint');
      adv.hidden = provider === 'off';
      keyField.hidden = !single();
    };

    let tested = 0; // a slower, older answer is dropped
    async function test() {
      const mine = ++tested;
      status.hidden = provider === 'off';
      if (status.hidden) return;
      status.replaceChildren(statusLine('testing', t('mcp.testing')));
      /** @type {{ ok: boolean, service?: string | null, skipped?: { service: string, error: string }[] }} */
      let res;
      try {
        res = await api.testSearch({});
      } catch (err) {
        res = { ok: false, skipped: [{ service: '', error: String(err.message || err) }] };
      }
      if (mine !== tested) return;
      const label = !res.ok
        ? t('search.fail')
        : provider === 'auto' && res.service
          ? t('search.okVia', { name: res.service })
          : t('search.ok');
      status.replaceChildren(statusLine(res.ok ? 'ok' : 'fail', label));
      for (const s of res.skipped || []) {
        // Under "auto", one before the one that answers: why it was passed over.
        const line = el('div', `lv-mcp-error${res.ok ? ' lv-mcp-error-muted' : ''}`);
        line.textContent = provider === 'auto' && s.service ? `${s.service}: ${s.error}` : s.error;
        status.appendChild(line);
      }
    }

    const strip = segmented(
      [['auto', t('search.auto')], ...SEARCH_SERVICES, ['off', t('search.off')]],
      provider,
      (val) => {
        provider = val;
        // A key half typed was the last service's: never this one's.
        editing = false;
        keyIn.value = '';
        renderKey();
        describeService();
        void apply({ provider: val });
      },
    );
    strip.setAttribute('aria-label', t('cfg.searchTitle'));
    form.append(strip, desc, status);

    // Advanced: only what a failure would send you looking for.
    const adv = /** @type {HTMLDetailsElement} */ (el('details', 'lv-cfg-advanced'));
    adv.appendChild(el('summary', '', t('cfg.advanced')));

    /** Typing a key replaces the saved one; Save (or Enter) saves it. */
    let editing = false;
    const keyIn = input('', { type: 'password', placeholder: t('search.keyNone') });
    const saveKey = async () => {
      const key = keyIn.value.trim();
      if (!key) return;
      await apply({ provider, api_key: key });
      editing = false;
      keyIn.value = '';
      renderKey();
    };
    keyIn.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') void saveKey();
    });
    const keyBox = el('div', 'lv-cfg-key');
    const renderKey = () => {
      keyBox.replaceChildren();
      const key = savedKey();
      if (key.set && !editing) {
        const row = el('div', 'lv-cfg-key-set');
        row.appendChild(el('span', 'lv-cfg-key-dot'));
        row.appendChild(el('span', 'lv-cfg-key-mask', t('cfg.apiKeySet', { hint: key.hint || '' })));
        row.appendChild(
          ghostBtn(t('cfg.replace'), () => {
            editing = true;
            renderKey();
            keyIn.focus();
          }),
        );
        row.appendChild(
          ghostBtn(t('cfg.clear'), async () => {
            await apply({ provider, api_key: '' }); // keyless now: the test says whether it answers
            renderKey();
          }),
        );
        keyBox.appendChild(row);
        return;
      }
      const row = el('div', 'lv-cfg-key-edit');
      row.append(keyIn, ghostBtn(t('cfg.save'), () => void saveKey()));
      keyBox.appendChild(row);
      if (key.set) {
        row.appendChild(
          ghostBtn(t('cfg.keep'), () => {
            editing = false;
            keyIn.value = '';
            renderKey();
          }),
        );
      }
    };
    renderKey();
    const keyField = field(t('search.key'), keyBox, t('search.keyHint'));
    adv.appendChild(keyField);
    // Through the system proxy or not; null follows Settings → General.
    const trustEnv = saved().trust_env ?? null;
    adv.appendChild(
      field(
        t('cfg.proxy'),
        segmented(
          [
            ['default', t('cfg.proxyDefault')],
            ['off', t('cfg.proxyOff')],
            ['on', t('cfg.proxyOn')],
          ],
          trustEnv === null ? 'default' : trustEnv ? 'on' : 'off',
          (val) => void apply({ trust_env: val === 'default' ? null : val === 'on' }),
        ),
        t('cfg.proxyHint'),
      ),
    );
    adv.open = (single() && savedKey().set) || trustEnv !== null;
    form.appendChild(adv);
    describeService();
    void test();

    pane.append(form);
  };
  void draw();

  return pane;
}

// ---- Skills pane ----------------------------------------------------------

/**
 * The Settings → Skills pane: the precedence-ordered skill-directory list
 * with per-directory discovery (what each root contributes). Add/remove
 * applies immediately — the server rebuilds the agent on every write.
 */
export function buildSkillsPane() {
  const pane = el('div', 'lv-cfg-pane');

  async function render() {
    pane.replaceChildren(el('div', 'lv-cfg-hint', '…'));
    let scan;
    try {
      if (!cfg()) await loadConfig();
      scan = await api.getSkills();
    } catch (err) {
      pane.replaceChildren(el('div', 'lv-cfg-error', String(err.message || err)));
      return;
    }
    const c = cfg();
    /** @type {string[]} */
    const dirs = c.skills.dirs;

    let saving = false;
    /** @param {string[]} next */
    const save = async (next) => {
      if (saving) return;
      saving = true;
      try {
        await api.setSkills({ dirs: next });
        await loadConfig();
        toast(t('cfg.saved'));
      } catch (err) {
        // The re-render below reverts the pane, so the error must outlive
        // it — a toast, matching the model-list actions.
        toast(String(err.message || err), { type: 'error' });
      }
      saving = false;
      render();
    };

    pane.replaceChildren();
    pane.appendChild(el('div', 'lv-cfg-pane-title', t('cfg.skillsTitle')));
    const ro = readOnlyNotice();
    if (ro) pane.appendChild(ro);
    pane.appendChild(el('div', 'lv-cfg-hint lv-skills-intro', t('cfg.skillsIntro')));

    const list = el('div', 'lv-model-list');
    if (!scan.roots.length) {
      list.appendChild(el('div', 'lv-cfg-hint', t('cfg.skillsNone')));
    }
    for (const root of scan.roots) {
      const row = el('div', 'lv-model-row lv-skills-root');
      if (!root.exists) row.classList.add('lv-off');
      const info = el('div', 'lv-model-row-info');

      const name = el('div', 'lv-model-row-name');
      name.appendChild(el('code', 'lv-skills-path', root.path));
      if (root.kind !== 'custom') {
        name.appendChild(el('span', 'lv-badge lv-badge-plain', t('cfg.skillsDefaultBadge')));
      }
      const meta = el('div', 'lv-model-row-meta');
      if (!root.exists) meta.textContent = t('cfg.skillsMissing');
      else if (!root.skills.length && !root.problems.length)
        meta.textContent = t('cfg.skillsEmpty');
      else {
        const parts = [t('cfg.skillsCount', { n: String(root.skills.length) })];
        if (root.problems.length)
          parts.push(t('cfg.skillsProblems', { n: String(root.problems.length) }));
        meta.textContent = parts.join(' · ');
      }
      info.append(name, meta);

      if (root.skills.length || root.problems.length) {
        const details = el('details', 'lv-skills-details');
        details.appendChild(el('summary', '', t('cfg.skillsShow')));
        /** Rows whose description may need the expand toggle. */
        const rows = [];
        for (const s of root.skills) {
          const item = el('div', 'lv-skills-item');
          const head = el('div', 'lv-skills-item-head');
          head.appendChild(el('span', 'lv-skills-item-name', s.name));
          if (s.shadowed) {
            head.appendChild(el('span', 'lv-badge lv-badge-plain', t('cfg.skillsShadowed')));
          }
          // Descriptions are trigger prose and can run long — clamp to two
          // lines so a root stays scannable, and expand in place rather than
          // into a tooltip, which would put the text out of reach of a
          // selection.
          const desc = el('div', 'lv-skills-item-desc', s.description);
          const more = el('button', 'lv-skills-item-more', t('cfg.skillsMore'));
          more.setAttribute('type', 'button');
          more.setAttribute('aria-expanded', 'false');
          more.addEventListener('click', () => {
            const open = item.classList.toggle('lv-open');
            more.textContent = t(open ? 'cfg.skillsLess' : 'cfg.skillsMore');
            more.setAttribute('aria-expanded', String(open));
          });
          head.appendChild(more);
          item.append(head, desc);
          rows.push({ item, desc });
          details.appendChild(item);
        }
        // The toggle is only honest once we know the text overflows, and
        // nothing inside a closed <details> has a layout to measure.
        details.addEventListener('toggle', () => {
          if (!details.open) return;
          for (const { item, desc } of rows) {
            if (item.classList.contains('lv-open')) continue;
            item.classList.toggle('lv-clamped', desc.scrollHeight > desc.clientHeight + 1);
          }
        });
        for (const p of root.problems) {
          details.appendChild(
            el('div', 'lv-skills-item lv-skills-item-problem', `${p.dir}: ${p.error}`),
          );
        }
        info.appendChild(details);
      }
      row.appendChild(info);

      const actions = el('div', 'lv-model-row-actions');
      const del = ghostBtn(t('cfg.skillsRemove'), () =>
        save(dirs.filter((d) => d !== root.path)),
      );
      del.classList.add('lv-danger');
      actions.appendChild(del);
      row.appendChild(actions);
      list.appendChild(row);
    }
    pane.appendChild(list);

    const addRow = el('div', 'lv-skills-add');
    const pathIn = input('', { mono: true, placeholder: t('cfg.skillsPathPlaceholder') });
    const addBtn = el('button', 'lv-btn lv-btn-sm', `＋ ${t('cfg.skillsAdd')}`);
    addBtn.setAttribute('type', 'button');
    const submit = () => {
      const value = pathIn.value.trim();
      if (!value) return;
      save([...dirs, value]);
    };
    addBtn.addEventListener('click', submit);
    pathIn.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        submit();
      }
    });
    addRow.append(pathIn, addBtn);
    pane.appendChild(addRow);

    // Offer to bring back a removed conventional root; restores put the
    // defaults first (their conventional precedence), customs keep order.
    const missingDefaults = scan.defaults.filter((d) => !dirs.includes(d));
    if (missingDefaults.length) {
      pane.appendChild(
        ghostBtn(t('cfg.skillsRestore'), () =>
          save([...scan.defaults, ...dirs.filter((d) => !scan.defaults.includes(d))]),
        ),
      );
    }
  }

  render();
  return pane;
}

// ---- About pane -----------------------------------------------------------

export function buildAboutPane() {
  const pane = el('div', 'lv-cfg-pane');
  pane.appendChild(el('div', 'lv-cfg-pane-title', t('settings.tabAbout')));
  const rows = el('div', 'lv-cfg-about');
  const row = (label, value) => {
    const r = el('div', 'lv-cfg-about-row');
    r.appendChild(el('span', 'lv-cfg-about-label', label));
    if (value instanceof HTMLElement) r.appendChild(value);
    else r.appendChild(el('span', 'lv-cfg-about-value', value));
    return r;
  };
  rows.appendChild(row(t('cfg.aboutVersion'), store.serverInfo?.version || '—'));
  if (store.canConfigure) {
    const scope = cfg()?.scope;
    rows.appendChild(
      row(
        t('cfg.aboutConfig'),
        scope ? `${scope.label}${scope.exists ? '' : ` ${t('cfg.scopeUnsaved')}`}` : '.lovia/config.json',
      ),
    );
  }
  const link = /** @type {HTMLAnchorElement} */ (el('a', 'lv-cfg-about-link', 'github.com/cymoo/lovia'));
  link.href = 'https://github.com/cymoo/lovia';
  link.target = '_blank';
  link.rel = 'noreferrer';
  rows.appendChild(row(t('cfg.aboutDocs'), link));
  pane.appendChild(rows);
  if (store.canConfigure) {
    pane.appendChild(el('div', 'lv-cfg-footnote', t('cfg.aboutConfigNote')));
  }
  return pane;
}

// ---- Composer chip --------------------------------------------------------

let _chip = null;
let _chipMenu = null;

function chipLabel() {
  if (!store.configured) return t('cfg.chipSetup');
  const p = activeProfile();
  return p ? profileName(p) : '…';
}

function updateChip() {
  if (!_chip) return;
  const label = _chip.querySelector('.lv-model-chip-label');
  if (label) label.textContent = chipLabel();
  const p = activeProfile();
  _chip.title = p ? t('cfg.chip', { name: p.model }) : t('cfg.chipSetup');
}

/**
 * Settings → Models' "Make default": the model a chat runs on unless it
 * picked its own (roles.chat).
 * @param {string} id
 */
async function makeDefault(id) {
  await api.setRoles({ chat: id });
  await loadConfig();
  toast(t('cfg.madeDefault', { name: profileName(cfg()?.models.find((m) => m.id === id)) }));
  updateChip();
  await refreshAgents();
}

/**
 * The chip's pick: this chat's model. The default's own entry means "follow
 * the default" (stored as none), so the chat moves with it later.
 * @param {string} id
 */
async function pickModel(id) {
  const c = cfg();
  const value = id === c.roles.chat ? null : id;
  const name = profileName(c.models.find((/** @type {any} */ m) => m.id === id));
  if (!store.sessionId) {
    _draftModel = value;
  } else {
    if (value === _chatModel) return;
    await api.setSessionModel(store.sessionId, value);
    _chatModel = value;
    // A reply in progress finishes on the model it began on.
    toast(t(store.streaming ? 'cfg.modelNextRun' : 'cfg.switched', { name }));
  }
  updateChip();
  store.emit('chat-model-changed');
}

// The agent list carries the model id + workspace/vision capabilities the
// composer reads (context popover row, attach button) — refetch after a swap.
export async function refreshAgents() {
  try {
    store.agents = await api.listAgents();
    store.emit('agents-loaded', store.agents);
  } catch {
    /* transient — the next boot resyncs */
  }
}

function closeChipMenu() {
  if (_chipMenu) {
    _chipMenu.remove();
    _chipMenu = null;
    _chip?.setAttribute('aria-expanded', 'false');
  }
}

/** The composer's model menu. */
async function openChipMenu() {
  if (!_chip) return;
  if (_chipMenu) {
    closeChipMenu();
    return;
  }
  if (!cfg()) {
    try {
      await loadConfig();
    } catch {
      return;
    }
  }
  // A new chat's first message is out and its chat not created yet: it went
  // with the draft's model, so a pick now would be silently lost.
  if (!store.sessionId && store.streaming) return;
  const c = cfg();
  const menu = el('div', 'lv-model-chip-menu');
  menu.setAttribute('role', 'menu');
  menu.appendChild(
    el(
      'div',
      'lv-model-chip-menu-title',
      t(store.sessionId ? 'cfg.chatModelTitle' : 'cfg.newChatModelTitle'),
    ),
  );
  const current = activeProfile()?.id;
  /**
   * @param {string} label
   * @param {string} id
   * @param {string} [sub]
   */
  const option = (label, id, sub) => {
    const on = current === id;
    const item = el('button', 'lv-model-chip-item');
    item.setAttribute('type', 'button');
    item.setAttribute('role', 'menuitemradio');
    item.setAttribute('aria-checked', String(on));
    item.appendChild(el('span', 'lv-model-chip-item-name', label));
    if (sub) item.appendChild(el('span', 'lv-model-chip-item-sub', sub));
    if (on) item.insertAdjacentHTML('beforeend', icon('check', { size: 15 }));
    item.addEventListener('click', async () => {
      closeChipMenu();
      try {
        await pickModel(id);
      } catch (err) {
        toast(String(err.message || err), { type: 'error' });
      }
    });
    menu.appendChild(item);
  };
  for (const p of c.models) {
    // The default says so: a chat on it moves when the default does.
    const notes = [
      p.id === c.roles.chat ? t('cfg.default') : null,
      p.vision === 'on' ? t('cfg.visionBadge') : null,
    ].filter(Boolean);
    option(profileName(p), p.id, notes.join(' · ') || undefined);
  }
  menu.appendChild(el('div', 'lv-model-chip-menu-divider'));
  const manage = el('button', 'lv-model-chip-item lv-manage');
  manage.setAttribute('type', 'button');
  manage.appendChild(el('span', 'lv-model-chip-item-name', t('cfg.manageModels')));
  manage.addEventListener('click', async () => {
    closeChipMenu();
    const { openSettings } = await import('./settings.js');
    openSettings('models');
  });
  menu.appendChild(manage);

  _chip.insertAdjacentElement('afterend', menu);
  // Over its chip: right edges in line (both measure from the composer bar).
  const bar = menu.offsetParent;
  if (bar) menu.style.right = `${bar.clientWidth - _chip.offsetLeft - _chip.offsetWidth}px`;
  _chipMenu = menu;
  _chip.setAttribute('aria-expanded', 'true');
}

function initModelChip() {
  // The composer bar's sending end, before the context ring and Send.
  const ring = document.getElementById('context-ring');
  const end = ring?.parentElement;
  if (!ring || !end || _chip) return;
  const chip = el('button', 'lv-model-chip');
  chip.id = 'model-chip';
  chip.setAttribute('type', 'button');
  chip.setAttribute('aria-haspopup', 'menu');
  chip.setAttribute('aria-expanded', 'false');
  chip.appendChild(el('span', 'lv-model-chip-label', chipLabel()));
  chip.addEventListener('click', async (e) => {
    e.stopPropagation();
    if (!store.configured) {
      const { openSettings } = await import('./settings.js');
      openSettings('models');
      return;
    }
    openChipMenu();
  });
  end.insertBefore(chip, ring);
  _chip = chip;
  document.addEventListener('click', (e) => {
    if (_chipMenu && !_chipMenu.contains(/** @type {Node} */ (e.target))) closeChipMenu();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && _chipMenu) {
      e.stopPropagation();
      closeChipMenu();
    }
  });
  // The label needs the config; fetch lazily so embedderless boots stay lean.
  loadConfig().then(updateChip).catch(() => {});
}

// ---- wiring ---------------------------------------------------------------

function _onConfigChanged(data) {
  // First configuration from another tab (or this one, races aside): the
  // server just gained its agent — reboot into the normal chat surface.
  if (!store.configured && data?.configured) {
    location.reload();
    return;
  }
  loadConfig()
    .then(() => {
      updateChip();
      refreshAgents();
    })
    .catch(() => {});
}

/**
 * Boot the model-config surfaces (chip, hero hook, change events). Called
 * from main.js once /api/info resolved; a no-op unless the server reports
 * features.settings.
 */
export function initModelConfig() {
  if (!store.canConfigure) return;
  initModelChip();
  store.on('config-changed', _onConfigChanged);
  // The open chat's own model: from its list row, then from its detail
  // (a task chat, or one the list doesn't carry).
  store.on('session-id', (/** @type {string | null} */ id) =>
    syncChatModel(id, id ? store.sessions.find((s) => s.id === id) : null),
  );
  store.on('session-meta', (/** @type {any} */ detail) => {
    if (detail?.id === store.sessionId) syncChatModel(detail.id, detail);
  });
  store.on('open-model-settings', async () => {
    const { openSettings } = await import('./settings.js');
    openSettings('models');
  });
}
