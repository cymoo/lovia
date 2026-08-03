// model-config.js — the Settings "Models"/"Search"/"About" panes, the composer
// model chip, and the first-run setup hero. All of it exists only when
// /api/info reports features.model_config (the CLI's default agent); embedder
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
import { confirmDialog } from './ui.js';

/** Fetch (or refresh) the masked config document into store.modelConfig. */
export async function loadConfig() {
  store.modelConfig = await api.getConfig();
  store.emit('config-loaded', store.modelConfig);
  return store.modelConfig;
}

function cfg() {
  return store.modelConfig;
}

function activeProfile() {
  const c = cfg();
  if (!c) return null;
  return c.models.find((m) => m.id === c.roles.chat) || null;
}

function profileName(p) {
  return p?.display_name || p?.model || '';
}

// The request path lovia actually appends, per API format — the live preview
// under the Base URL field (the "/v1 or not" question answers itself).
function requestPreview(flavor, baseUrl, fallback) {
  const base = (baseUrl || fallback || '').replace(/\/+$/, '');
  if (!base) return '';
  const path = flavor === 'anthropic' ? '/messages' : '/chat/completions';
  return t('cfg.baseUrlPreview', { url: base + path });
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

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function field(labelText, control, hintText) {
  const wrap = el('div', 'cfg-field');
  const label = el('label', 'cfg-label', labelText);
  wrap.append(label, control);
  if (hintText) wrap.appendChild(el('div', 'cfg-hint', hintText));
  return wrap;
}

function input(value, { mono = false, placeholder = '', type = 'text' } = {}) {
  const node = /** @type {HTMLInputElement} */ (el('input', 'dialog-input cfg-input'));
  node.type = type;
  node.value = value ?? '';
  node.placeholder = placeholder;
  if (mono) node.classList.add('mono');
  return node;
}

/**
 * A pill-track segmented control (the files-panel tabs pattern).
 * @param {[string, string][]} options [value, label] pairs.
 * @param {string} value Selected value.
 * @param {(value: string) => void} [onChange]
 */
function segmented(options, value, onChange) {
  const wrap = el('div', 'seg-control');
  wrap.setAttribute('role', 'tablist');
  const sync = () => {
    for (const b of wrap.children) {
      const btn = /** @type {HTMLButtonElement} */ (b);
      const active = btn.dataset.value === wrap.dataset.value;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-selected', active ? 'true' : 'false');
    }
  };
  wrap.dataset.value = value;
  for (const [val, label] of options) {
    const btn = el('button', 'seg-btn', label);
    btn.setAttribute('type', 'button');
    btn.setAttribute('role', 'tab');
    btn.dataset.value = val;
    btn.addEventListener('click', () => {
      if (wrap.dataset.value === val) return;
      wrap.dataset.value = val;
      sync();
      onChange?.(val);
    });
    wrap.appendChild(btn);
  }
  sync();
  return wrap;
}

function ghostBtn(label, onClick) {
  const btn = el('button', 'btn btn-ghost btn-sm', label);
  btn.setAttribute('type', 'button');
  btn.addEventListener('click', onClick);
  return btn;
}

// ---- Models pane ----------------------------------------------------------

/**
 * The Settings → Models pane: profile list ⇄ edit form, plus role assignment.
 * Owns its internal rerenders; safe to rebuild wholesale on tab switches.
 */
export function buildModelsPane() {
  const pane = el('div', 'cfg-pane');
  /** @type {{ name: 'list' } | { name: 'form', id: string | null }} */
  let view = { name: 'list' };

  async function render() {
    if (!cfg()) {
      pane.replaceChildren(el('div', 'cfg-hint', '…'));
      try {
        await loadConfig();
      } catch (err) {
        pane.replaceChildren(el('div', 'cfg-error', String(err.message || err)));
        return;
      }
    }
    pane.replaceChildren(view.name === 'list' ? renderList() : renderForm(view.id));
  }

  function renderList() {
    const c = cfg();
    const box = el('div');

    const head = el('div', 'cfg-pane-head');
    head.appendChild(el('span', 'cfg-pane-title', t('cfg.models')));
    const add = el('button', 'btn btn-sm', `＋ ${t('cfg.addModel')}`);
    add.setAttribute('type', 'button');
    add.addEventListener('click', () => {
      view = { name: 'form', id: null };
      render();
    });
    head.appendChild(add);
    box.appendChild(head);

    const list = el('div', 'model-list');
    if (!c.models.length) list.appendChild(el('div', 'cfg-hint', t('cfg.emptyList')));
    for (const p of c.models) {
      const row = el('div', 'model-row');
      row.setAttribute('role', 'button');
      row.tabIndex = 0;
      const info = el('div', 'model-row-info');
      const name = el('div', 'model-row-name', profileName(p));
      if (p.id === c.roles.chat) {
        name.appendChild(el('span', 'badge badge-default', t('cfg.default')));
      }
      if (p.vision === 'on') {
        name.appendChild(el('span', 'badge badge-plain', t('cfg.visionBadge')));
      }
      const host = hostOf(p.base_url);
      info.append(name, el('div', 'model-row-meta', host ? `${p.model} · ${host}` : p.model));
      row.appendChild(info);

      const actions = el('div', 'model-row-actions');
      if (p.id !== c.roles.chat) {
        actions.appendChild(
          ghostBtn(t('cfg.makeDefault'), async (e) => {
            e.stopPropagation();
            await switchModel(p.id);
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
        del.classList.add('danger');
        actions.appendChild(del);
      }
      row.appendChild(actions);
      row.appendChild(el('span', 'model-row-chev', '›'));
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

    const note = el('div', 'cfg-footnote');
    note.textContent = c.scope.exists
      ? t('cfg.scopeNote', { label: c.scope.label })
      : t('cfg.scopeNote', { label: c.scope.label }) + ' ' + t('cfg.scopeUnsaved');
    box.appendChild(note);
    return box;
  }

  function renderRoles() {
    const c = cfg();
    const box = el('div', 'cfg-roles');
    box.appendChild(el('div', 'cfg-pane-title', t('cfg.roles')));

    const roleRow = (labelText, current, emptyLabel, role, hint) => {
      const select = /** @type {HTMLSelectElement} */ (
        el('select', 'dialog-input cfg-role-select')
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
      const row = el('div', 'cfg-role-row');
      row.appendChild(el('label', 'cfg-role-label', labelText));
      row.appendChild(select);
      row.appendChild(el('span', 'cfg-role-hint', hint));
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

    const head = el('div', 'cfg-form-head');
    const back = el('button', 'btn-icon cfg-back', '‹');
    back.setAttribute('type', 'button');
    back.setAttribute('aria-label', t('cfg.backLabel'));
    back.addEventListener('click', () => {
      view = { name: 'list' };
      render();
    });
    head.append(back, el('span', 'cfg-pane-title', stored ? t('cfg.editModel') : t('cfg.newModel')));
    box.appendChild(head);

    const form = el('div', 'cfg-form');

    const nameIn = input(stored?.name || '', { placeholder: stored?.model || '' });
    form.appendChild(field(t('cfg.name'), nameIn, t('cfg.nameHint')));

    let flavor = stored?.flavor || 'openai';
    const preview = el('div', 'cfg-hint cfg-preview');
    const urlIn = input(stored?.base_url || '', {
      mono: true,
      placeholder: flavorDefault(flavor),
    });
    const syncPreview = () => {
      urlIn.placeholder = flavorDefault(flavor);
      preview.textContent = requestPreview(flavor, urlIn.value.trim(), flavorDefault(flavor));
    };
    const flavorSeg = segmented(
      [
        ['openai', t('cfg.flavorOpenAI')],
        ['anthropic', t('cfg.flavorAnthropic')],
      ],
      flavor,
      (val) => {
        flavor = val;
        syncPreview();
      },
    );
    form.appendChild(field(t('cfg.flavor'), flavorSeg));
    urlIn.addEventListener('input', syncPreview);
    const urlField = field(t('cfg.baseUrl'), urlIn);
    urlField.appendChild(preview);
    form.appendChild(urlField);
    syncPreview();

    // API key: never echoed. keep (leave stored) / edit (replace) / clear.
    /** @type {'keep' | 'edit' | 'clear'} */
    let keyMode = stored?.api_key?.set ? 'keep' : 'edit';
    const keyIn = input('', { type: 'password', placeholder: t('cfg.apiKeyNone') });
    const keyBox = el('div', 'cfg-key');
    const renderKey = () => {
      syncProbeButtons();
      keyBox.replaceChildren();
      if (keyMode === 'keep') {
        const row = el('div', 'cfg-key-set');
        row.appendChild(el('span', 'cfg-key-dot'));
        row.appendChild(
          el('span', 'cfg-key-mask', t('cfg.apiKeySet', { hint: stored.api_key.hint || '' })),
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
        const row = el('div', 'cfg-key-set');
        row.appendChild(el('span', 'cfg-key-mask', t('cfg.apiKeyNone')));
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
    form.appendChild(field(t('cfg.apiKey'), keyBox, t('cfg.apiKeyNote')));

    const modelIn = input(stored?.model || '', { mono: true, placeholder: 'deepseek-v4-pro' });
    const datalist = el('datalist');
    datalist.id = `cfg-model-list-${Math.random().toString(36).slice(2, 8)}`;
    modelIn.setAttribute('list', datalist.id);
    const fetchRow = el('div', 'cfg-fetch-row');
    const fetchBtn = el('button', 'btn btn-sm', t('cfg.fetchModels'));
    fetchBtn.setAttribute('type', 'button');
    fetchBtn.addEventListener('click', async () => {
      if (!hasEndpoint()) return;
      fetchBtn.disabled = true;
      try {
        const res = await probe();
        datalist.replaceChildren(
          ...(res.models || []).map((m) => {
            const opt = /** @type {HTMLOptionElement} */ (document.createElement('option'));
            opt.value = m;
            return opt;
          }),
        );
        if (!res.models?.length) toast(t('cfg.fetchHint'));
      } catch (err) {
        toast(String(err.message || err), { type: 'error' });
      } finally {
        fetchBtn.disabled = false;
      }
    });
    fetchRow.append(modelIn, fetchBtn, datalist);
    form.appendChild(field(t('cfg.modelId'), fetchRow, t('cfg.fetchHint')));

    // Advanced: context window + vision capability.
    const adv = /** @type {HTMLDetailsElement} */ (el('details', 'cfg-advanced'));
    adv.appendChild(el('summary', '', t('cfg.advanced')));
    const windowIn = input(stored?.context_window ? String(stored.context_window) : '', {
      placeholder: t('cfg.contextWindowAuto'),
    });
    windowIn.inputMode = 'numeric';
    adv.appendChild(field(t('cfg.contextWindow'), windowIn));
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
    if (stored?.context_window || (stored && stored.vision !== 'auto')) adv.open = true;
    form.appendChild(adv);

    // Test connection — the wizard's probe over HTTP, same classifications.
    const testRow = el('div', 'cfg-test-row');
    const testBtn = el('button', 'btn cfg-test-btn', t('cfg.test'));
    testBtn.setAttribute('type', 'button');
    const testOut = el('span', 'cfg-test-result');
    testBtn.addEventListener('click', async () => {
      if (!modelIn.value.trim()) return;
      testBtn.disabled = true;
      testOut.className = 'cfg-test-result';
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
        testOut.classList.add(good ? 'ok' : 'bad');
        const bits = [`${good ? '✓' : '✗'} ${labels[res.outcome]} (${res.detail})`];
        if (res.context_window) {
          bits.push(t('cfg.testWindow', { n: res.context_window.toLocaleString() }));
        }
        testOut.textContent = bits.join(' · ');
        if (res.note) testOut.title = res.note;
      } catch (err) {
        testOut.classList.add('bad');
        testOut.textContent = `✗ ${String(err.message || err)}`;
      } finally {
        testBtn.disabled = false;
      }
    });
    testRow.append(testBtn, testOut);
    form.appendChild(testRow);

    // Enough of an endpoint to talk to: an explicit URL, or a key for the
    // flavor's official default host (entered now or stored).
    function hasEndpoint() {
      return !!(
        urlIn.value.trim() ||
        (keyMode === 'edit' && keyIn.value) ||
        (keyMode === 'keep' && stored?.api_key?.set)
      );
    }

    // A probe with nothing filled in can only time out against a default
    // host — keep the buttons dead until they can mean something.
    function syncProbeButtons() {
      testBtn.disabled = !modelIn.value.trim();
      testBtn.title = testBtn.disabled ? t('cfg.testNeedsModel') : '';
      fetchBtn.disabled = !hasEndpoint();
      fetchBtn.title = fetchBtn.disabled ? t('cfg.fetchNeedsEndpoint') : '';
    }

    function probe() {
      const body = {
        model: modelIn.value.trim() || (stored ? stored.model : 'probe'),
        flavor,
        base_url: urlIn.value.trim() || null,
        context_window: parseWindow() ?? null,
      };
      if (keyMode === 'edit' && keyIn.value) {
        body.api_key = keyIn.value;
      } else if (keyMode === 'keep' && stored) {
        body.profile_id = stored.id; // reuse the stored key server-side
      }
      return api.testConnection(body);
    }

    function parseWindow() {
      const raw = windowIn.value.trim();
      if (!raw) return undefined;
      const n = parseInt(raw.replace(/[,\s]/g, ''), 10);
      return Number.isFinite(n) && n > 0 ? n : undefined;
    }

    renderKey();
    modelIn.addEventListener('input', syncProbeButtons);
    urlIn.addEventListener('input', syncProbeButtons);
    keyIn.addEventListener('input', syncProbeButtons);
    syncProbeButtons();

    const errBox = el('div', 'cfg-error');
    errBox.hidden = true;

    const actions = el('div', 'cfg-form-actions');
    const cancel = el('button', 'btn btn-ghost', t('cfg.cancel'));
    cancel.setAttribute('type', 'button');
    cancel.addEventListener('click', () => {
      view = { name: 'list' };
      render();
    });
    const save = el('button', 'btn btn-primary', t('cfg.save'));
    save.setAttribute('type', 'button');
    save.addEventListener('click', async () => {
      const body = {
        name: nameIn.value.trim() || null,
        model: modelIn.value.trim(),
        flavor,
        base_url: urlIn.value.trim() || null,
        context_window: parseWindow() ?? null,
        vision,
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

export function buildSearchPane() {
  const pane = el('div', 'cfg-pane');

  (async () => {
    if (!cfg()) {
      try {
        await loadConfig();
      } catch (err) {
        pane.replaceChildren(el('div', 'cfg-error', String(err.message || err)));
        return;
      }
    }
    const c = cfg();
    pane.replaceChildren();
    pane.appendChild(el('div', 'cfg-pane-title', t('cfg.searchTitle')));
    const form = el('div', 'cfg-form');

    const select = /** @type {HTMLSelectElement} */ (el('select', 'dialog-input'));
    for (const [val, label] of [
      ['auto', t('cfg.searchAuto')],
      ['tavily', t('cfg.searchTavily')],
      ['ddg', t('cfg.searchDdg')],
      ['off', t('cfg.searchOff')],
    ]) {
      const opt = /** @type {HTMLOptionElement} */ (el('option', '', label));
      opt.value = val;
      select.appendChild(opt);
    }
    select.value = c.search.backend;
    form.appendChild(field(t('cfg.searchBackend'), select));

    /** @type {'keep' | 'edit' | 'clear'} */
    let keyMode = c.search.tavily_api_key.set ? 'keep' : 'edit';
    const keyIn = input('', { type: 'password', placeholder: t('cfg.apiKeyNone') });
    const keyBox = el('div', 'cfg-key');
    const renderKey = () => {
      keyBox.replaceChildren();
      if (keyMode === 'keep') {
        const row = el('div', 'cfg-key-set');
        row.appendChild(el('span', 'cfg-key-dot'));
        row.appendChild(
          el('span', 'cfg-key-mask', t('cfg.apiKeySet', { hint: c.search.tavily_api_key.hint || '' })),
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
        const row = el('div', 'cfg-key-set');
        row.appendChild(el('span', 'cfg-key-mask', t('cfg.apiKeyNone')));
        row.appendChild(
          ghostBtn(t('cfg.keep'), () => {
            keyMode = 'keep';
            renderKey();
          }),
        );
        keyBox.appendChild(row);
      } else {
        keyBox.appendChild(keyIn);
        if (c.search.tavily_api_key.set) {
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
    renderKey();
    form.appendChild(field(t('cfg.tavilyKey'), keyBox, t('cfg.apiKeyNote')));

    const errBox = el('div', 'cfg-error');
    errBox.hidden = true;
    const actions = el('div', 'cfg-form-actions');
    const save = el('button', 'btn btn-primary', t('cfg.save'));
    save.setAttribute('type', 'button');
    save.addEventListener('click', async () => {
      const body = { backend: select.value };
      if (keyMode === 'clear') body.tavily_api_key = '';
      else if (keyMode === 'edit' && keyIn.value) body.tavily_api_key = keyIn.value;
      save.disabled = true;
      errBox.hidden = true;
      try {
        await api.setSearch(body);
        await loadConfig();
        toast(t('cfg.saved'));
      } catch (err) {
        errBox.textContent = String(err.message || err);
        errBox.hidden = false;
      } finally {
        save.disabled = false;
      }
    });
    actions.appendChild(save);
    pane.append(form, errBox, actions);
  })();

  return pane;
}

// ---- About pane -----------------------------------------------------------

export function buildAboutPane() {
  const pane = el('div', 'cfg-pane');
  pane.appendChild(el('div', 'cfg-pane-title', t('settings.tabAbout')));
  const rows = el('div', 'cfg-about');
  const row = (label, value) => {
    const r = el('div', 'cfg-about-row');
    r.appendChild(el('span', 'cfg-about-label', label));
    if (value instanceof HTMLElement) r.appendChild(value);
    else r.appendChild(el('span', 'cfg-about-value', value));
    return r;
  };
  rows.appendChild(row(t('cfg.aboutVersion'), store.serverInfo?.version || '—'));
  if (store.canConfigureModels) {
    const scope = cfg()?.scope;
    rows.appendChild(
      row(
        t('cfg.aboutConfig'),
        scope ? `${scope.label}${scope.exists ? '' : ` ${t('cfg.scopeUnsaved')}`}` : '.lovia/config.json',
      ),
    );
  }
  const link = /** @type {HTMLAnchorElement} */ (el('a', 'cfg-about-link', 'github.com/cymoo/lovia'));
  link.href = 'https://github.com/cymoo/lovia';
  link.target = '_blank';
  link.rel = 'noreferrer';
  rows.appendChild(row(t('cfg.aboutDocs'), link));
  pane.appendChild(rows);
  if (store.canConfigureModels) {
    pane.appendChild(el('div', 'cfg-footnote', t('cfg.aboutConfigNote')));
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
  const label = _chip.querySelector('.model-chip-label');
  if (label) label.textContent = chipLabel();
  const p = activeProfile();
  _chip.title = p ? t('cfg.chip', { name: p.model }) : t('cfg.chipSetup');
}

async function switchModel(id) {
  const before = cfg()?.roles?.chat;
  if (id === before) return;
  await api.setRoles({ chat: id });
  await loadConfig();
  const p = activeProfile();
  toast(t('cfg.switched', { name: profileName(p) }));
  updateChip();
  await refreshAgents();
}

// The agent list carries the model id + workspace/vision capabilities the
// composer reads (context popover row, attach button) — refetch after a swap.
async function refreshAgents() {
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

async function openChipMenu() {
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
  const c = cfg();
  const menu = el('div', 'model-chip-menu');
  menu.setAttribute('role', 'menu');
  menu.appendChild(el('div', 'model-chip-menu-title', t('cfg.switchTitle')));
  for (const p of c.models) {
    const item = el('button', 'model-chip-item');
    item.setAttribute('type', 'button');
    item.setAttribute('role', 'menuitem');
    item.appendChild(el('span', 'model-chip-item-name', profileName(p)));
    if (p.vision === 'on') item.appendChild(el('span', 'model-chip-item-sub', t('cfg.visionBadge')));
    if (p.id === c.roles.chat) item.appendChild(el('span', 'model-chip-item-check', '✓'));
    item.addEventListener('click', async () => {
      closeChipMenu();
      try {
        await switchModel(p.id);
      } catch (err) {
        toast(String(err.message || err), { type: 'error' });
      }
    });
    menu.appendChild(item);
  }
  menu.appendChild(el('div', 'model-chip-menu-divider'));
  const manage = el('button', 'model-chip-item manage');
  manage.setAttribute('type', 'button');
  manage.appendChild(el('span', 'model-chip-item-name', t('cfg.manageModels')));
  manage.addEventListener('click', async () => {
    closeChipMenu();
    const { openSettings } = await import('./settings.js');
    openSettings('models');
  });
  menu.appendChild(manage);

  _chip.insertAdjacentElement('afterend', menu);
  _chipMenu = menu;
  _chip.setAttribute('aria-expanded', 'true');
}

function initModelChip() {
  const inner = document.querySelector('.composer-inner');
  const ring = document.getElementById('context-ring');
  if (!inner || _chip) return;
  const chip = el('button', 'model-chip');
  chip.id = 'model-chip';
  chip.setAttribute('type', 'button');
  chip.setAttribute('aria-haspopup', 'menu');
  chip.setAttribute('aria-expanded', 'false');
  chip.appendChild(el('span', 'model-chip-label', chipLabel()));
  chip.addEventListener('click', async (e) => {
    e.stopPropagation();
    if (!store.configured) {
      const { openSettings } = await import('./settings.js');
      openSettings('models');
      return;
    }
    openChipMenu();
  });
  inner.insertBefore(chip, ring);
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
 * features.model_config.
 */
export function initModelConfig() {
  if (!store.canConfigureModels) return;
  initModelChip();
  store.on('config-changed', _onConfigChanged);
  store.on('open-model-settings', async () => {
    const { openSettings } = await import('./settings.js');
    openSettings('models');
  });
}
