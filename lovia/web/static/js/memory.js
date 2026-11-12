// Memory editor — what the agent remembers about you, editable.
//
// The Memory plugin's Notes are a small always-in-context fact list, stored as
// "- fact" markdown lines. This dialog shows that body verbatim and saves a
// wholesale replacement; the server re-applies the plugin's own normalization
// (bullets only, dedup), so the meter warns *before* saving when a line would
// be dropped. Opened from the sidebar footer; visible only for agents that
// carry the plugin (AgentInfo.memory).
//
// A Memory split by project (AgentInfo.memory_projects) has two layers, one
// tab each: "About me" (everywhere) and "This project" (the open chat's, or
// the draft's directory). Whether the chat has a project at all is the
// server's call — the layer GET answers 400 for General or no workspace — so
// both layers load before the dialog opens and the tab strip never pops in.
import { t } from './i18n.js';
import { api } from './api.js';
import { store } from './store.js';
import { confirmDialog, showDialog } from './ui.js';
import { toast } from './toast.js';
import { icon } from './icons.js';
import { escapeHtml } from './util.js';
import { draftWorkspaceRoot, workspaceName } from './workspaces.js';

let btn = null;

function updateVisibility() {
  const agent = store.agents.find((a) => a.name === store.agent);
  btn?.classList.toggle('lv-hidden', !agent?.memory);
}

const droppedLines = (content) =>
  content.split('\n').filter((l) => l.trim() && !l.trim().startsWith('- ')).length;

/**
 * @typedef {{ content: string, budget: number, dreamed_at: number | null, scope_key?: string | null }} Notes
 * @typedef {{ where: import('./api.js').MemoryLayer, saved: string, draft: string, budget: number, dreamedAt: number | null }} LayerState
 */

/** @param {import('./api.js').MemoryLayer} where @param {Notes} data @returns {LayerState} */
const layerState = (where, data) => ({
  where,
  saved: data.content,
  draft: data.content,
  budget: data.budget,
  dreamedAt: data.dreamed_at,
});

/**
 * The open chat's project layer, or null when it has none (General, no
 * workspace) or can't be reached (a directory this server no longer allows,
 * moved, …) — never an error: "About me" stays editable either way.
 */
async function loadProject() {
  const agent = store.agents.find((a) => a.name === store.agent);
  if (!agent?.memory_projects) return null;
  /** @type {import('./api.js').MemoryLayer} */
  const where = store.sessionId
    ? { layer: 'project', session: store.sessionId }
    : { agent: store.agent, layer: 'project', root: draftWorkspaceRoot() };
  try {
    const data = await api.getMemory(where);
    return {
      state: layerState(where, data),
      key: data.scope_key,
      name: workspaceName(data.scope_key),
    };
  } catch (err) {
    // 400/404: no project here. Anything else (409 refused directory): the
    // chat itself says why when it runs; the dialog just leaves the tab out.
    if (err.status !== 400 && err.status !== 404) console.warn('memory: project layer', err);
    return null;
  }
}

async function openMemoryDialog() {
  /** @type {import('./api.js').MemoryLayer} */
  const userWhere = { agent: store.agent };
  /** @type {Record<string, LayerState>} */
  const layers = {};
  let projectName = '';
  let projectKey = '';
  let loadError = null;
  try {
    const [user, project] = await Promise.all([api.getMemory(userWhere), loadProject()]);
    layers.user = layerState(userWhere, user);
    if (project) {
      layers.project = project.state;
      projectName = project.name;
      projectKey = project.key ?? '';
    }
  } catch (err) {
    loadError = err;
  }

  const panel = document.createElement('div');
  panel.className = 'lv-memory-panel';
  // Name the agent only when there's more than one to tell apart.
  const who = store.agents.length > 1 && store.agent ? ` — ${escapeHtml(store.agent)}` : '';
  const name = escapeHtml(projectName);
  const tabs = layers.project
    ? `<div class="lv-memory-tabs">
        <div class="lv-files-tabs" role="tablist">
          <button type="button" class="lv-files-tab" role="tab" data-layer="user">${t('memory.tabUser')}</button>
          <button type="button" class="lv-files-tab lv-memory-tab-project" role="tab" data-layer="project"
            title="${escapeHtml(projectKey)}">${t('memory.tabProject', { name })}</button>
        </div>
        <button type="button" class="lv-btn lv-btn-ghost lv-btn-sm lv-memory-clear">${t('memory.clear')}</button>
      </div>`
    : '';
  panel.innerHTML = `
    <div class="lv-memory-head">
      <h3>${t('memory.title')}${who}</h3>
      <button type="button" class="lv-btn-icon lv-memory-close" aria-label="${t('dialog.close')}">${icon('x', { size: 16 })}</button>
    </div>
    ${tabs}
    <p class="lv-memory-hint"></p>
    <label class="lv-vh" for="memory-editor">${t('memory.editorLabel')}</label>
    <textarea id="memory-editor" class="lv-dialog-input lv-memory-editor" spellcheck="false" disabled></textarea>
    <div class="lv-memory-foot">
      <span class="lv-memory-meter">${t('memory.loading')}</span>
      <div class="lv-memory-actions">
        <button type="button" class="lv-btn lv-btn-ghost lv-memory-dream" disabled
          title="${t('memory.dream')}">${icon('sparkles', { size: 14 })} ${t('memory.dream')}</button>
        <button type="button" class="lv-btn lv-btn-ghost lv-memory-cancel">${t('dialog.cancel')}</button>
        <button type="button" class="lv-btn lv-btn-primary lv-memory-save" disabled>${t('dialog.save')}</button>
      </div>
    </div>`;

  const editor = /** @type {HTMLTextAreaElement} */ (panel.querySelector('.lv-memory-editor'));
  const hint = panel.querySelector('.lv-memory-hint');
  const meter = panel.querySelector('.lv-memory-meter');
  const saveBtn = /** @type {HTMLButtonElement} */ (panel.querySelector('.lv-memory-save'));
  const dreamBtn = /** @type {HTMLButtonElement} */ (panel.querySelector('.lv-memory-dream'));
  const clearBtn = /** @type {HTMLButtonElement | null} */ (panel.querySelector('.lv-memory-clear'));
  const tabBtns = /** @type {HTMLButtonElement[]} */ ([...panel.querySelectorAll('[data-layer]')]);

  const dialog = showDialog({ body: panel });
  dialog.classList.add('lv-dialog-wide');
  panel.querySelector('.lv-memory-close').addEventListener('click', () => dialog.close());
  panel.querySelector('.lv-memory-cancel').addEventListener('click', () => dialog.close());

  if (loadError) {
    hint.textContent = t('memory.hint');
    meter.textContent = loadError.message || t('memory.loadFailed');
    meter.classList.add('lv-warn');
    return;
  }

  let current = '';
  const active = () => layers[current];

  const syncMeter = () => {
    const { budget, dreamedAt } = active();
    const used = editor.value.length;
    const dropped = droppedLines(editor.value);
    const bits = [
      t('memory.chars', {
        used: used.toLocaleString(),
        budget: budget.toLocaleString(),
      }),
    ];
    if (dropped) bits.push(t('memory.dropped', { n: dropped }));
    if (dreamedAt)
      bits.push(t('memory.dreamedAt', { when: new Date(dreamedAt * 1000).toLocaleDateString() }));
    meter.textContent = bits.join(' · ');
    meter.classList.toggle('lv-warn', dropped > 0 || used > budget);
  };

  // Each tab keeps its own unsaved edits; switching only swaps the view.
  function show(layer) {
    if (layers[current]) layers[current].draft = editor.value;
    current = layer;
    for (const b of tabBtns) {
      const on = b.dataset.layer === layer;
      b.classList.toggle('lv-active', on);
      b.setAttribute('aria-selected', String(on));
    }
    const project = layer === 'project';
    hint.textContent = project
      ? t('memory.hintProject', { name: projectName })
      : t('memory.hint');
    editor.placeholder = t(project ? 'memory.placeholderProject' : 'memory.placeholder');
    // Hidden, not removed: the tab row keeps its height either way.
    if (clearBtn) clearBtn.style.visibility = project ? '' : 'hidden';
    editor.value = active().draft;
    syncMeter();
  }

  const setBusy = (busy) => {
    for (const el of [editor, saveBtn, dreamBtn, clearBtn, ...tabBtns]) {
      if (el) el.disabled = busy;
    }
  };

  // Saves every tab with edits, then closes.
  async function save() {
    layers[current].draft = editor.value;
    setBusy(true);
    try {
      for (const state of Object.values(layers)) {
        if (state.draft === state.saved) continue;
        const data = await api.putMemory({ ...state.where, content: state.draft });
        state.saved = state.draft = data.content;
      }
      toast(t('toast.memorySaved'));
      dialog.close();
    } catch (err) {
      toast(err.message || t('memory.saveFailed'), { type: 'error' });
      setBusy(false);
    }
  }

  // The dream pass rewrites the tab's whole list server-side (the previous
  // body lands in MEMORY.md.bak); the editor refreshes to the tidied result.
  async function dreamNow() {
    const state = active();
    setBusy(true);
    meter.textContent = t('memory.dreaming');
    try {
      const data = await api.dreamMemory(state.where);
      Object.assign(state, layerState(state.where, data));
      editor.value = data.content;
      toast(t('toast.memoryDreamed', { before: data.before, after: data.after }));
    } catch (err) {
      toast(err.message || t('memory.dreamFailed'), { type: 'error' });
    } finally {
      setBusy(false);
      syncMeter();
    }
  }

  // Notes and archive both: the archive is what recall searches, and the
  // user has no other way to reach it.
  async function clearProject() {
    const state = layers.project;
    if (!(await confirmDialog(t('memory.clearConfirm', { name: projectName })))) return;
    setBusy(true);
    try {
      const data = await api.clearMemory(state.where);
      Object.assign(state, layerState(state.where, data));
      editor.value = data.content;
      toast(t('toast.memoryCleared', { name: projectName }));
    } catch (err) {
      toast(err.message || t('memory.clearFailed'), { type: 'error' });
    } finally {
      setBusy(false);
      syncMeter();
    }
  }

  setBusy(false);
  show('user');
  editor.focus();
  editor.setSelectionRange(editor.value.length, editor.value.length);

  for (const b of tabBtns) b.addEventListener('click', () => show(b.dataset.layer));
  clearBtn?.addEventListener('click', clearProject);
  dreamBtn.addEventListener('click', dreamNow);
  editor.addEventListener('input', syncMeter);
  editor.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      if (!saveBtn.disabled) save();
    }
  });
  saveBtn.addEventListener('click', save);
}

/** Wire up the Memory editor button and keep its visibility in sync with the active agent. */
export function initMemory() {
  btn = document.getElementById('memory-btn');
  if (!btn) return;
  btn.addEventListener('click', openMemoryDialog);
  store.on('agents-loaded', updateVisibility);
  store.on('agent-changed', updateVisibility);
}
