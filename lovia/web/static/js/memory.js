// memory.js — Settings › Memory: what an agent remembers, editable.
//
// The Memory plugin's Notes are a small always-in-context fact list, stored as
// "- fact" markdown lines. The page shows that body verbatim and saves a
// wholesale replacement; the server re-applies the plugin's own normalization
// (bullets only, dedup), so the meter warns *before* saving when a line would
// be dropped. Settings offers the page only when some agent carries the plugin
// (AgentInfo.memory).
//
// Settings is global, so the page follows no chat: an explicit pick names the
// scope it edits, "About me" (everywhere) or one project's (a Memory split by
// project, AgentInfo.memory_projects), starting at the open chat's directory.
// Whether a directory is a project is the server's call (General answers
// 400), so the page doesn't repeat that rule. The Notes are the selected
// agent's — no agent pick: `lovia web` shares one Memory among its agents,
// where a pick would offer the same notes under every name.
import { t } from './i18n.js';
import { api } from './api.js';
import { store } from './store.js';
import { confirmDialog, segmented } from './ui.js';
import { toast } from './toast.js';
import { icon } from './icons.js';
import { scopeRoot, workspaceChoices, workspaceName, workspacesOn } from './workspaces.js';

/**
 * @typedef {{ content: string, budget: number, dreamed_at: number | null, scope_key?: string | null }} Notes
 * @typedef {{ where: import('./api.js').MemoryLayer, saved: string, draft: string, budget: number, dreamedAt: number | null }} LayerState
 * @typedef {{ layer: 'user' | 'project', root: string | undefined }} Pick
 * @typedef {{ root: string | undefined, name: string }} Project
 */

/** The agent whose memory the page edits: the selected one, else the first with any. */
function memoryAgent() {
  const withMemory = store.agents.filter((a) => a.memory);
  return withMemory.find((a) => a.name === store.agent) ?? withMemory[0];
}

/** Whether Settings offers the Memory page. */
export function hasMemory() {
  return !!memoryAgent();
}

/** Whether that memory is kept per project (a sidebar folder can open its own). */
export function hasProjectMemory() {
  return !!memoryAgent()?.memory_projects;
}

const droppedLines = (content) =>
  content.split('\n').filter((l) => l.trim() && !l.trim().startsWith('- ')).length;

/** @param {import('./api.js').MemoryLayer} where @param {Notes} data @returns {LayerState} */
const layerState = (where, data) => ({
  where,
  saved: data.content,
  draft: data.content,
  budget: data.budget,
  dreamedAt: data.dreamed_at,
});

/**
 * The directories whose project memory may be picked: the list's, less
 * General. A server without directories has the one its agents work in —
 * no root to send, and its name comes back with the notes.
 * @returns {Project[]}
 */
function projectChoices() {
  if (!workspacesOn()) return [{ root: undefined, name: '' }];
  return workspaceChoices()
    .filter((w) => w.kind !== 'general')
    .map((w) => ({ root: w.root, name: workspaceName(w.root) }));
}

/** @param {string} agent @param {Pick} pick @returns {import('./api.js').MemoryLayer} */
const whereOf = (agent, pick) =>
  pick.layer === 'user'
    ? { agent }
    : { agent, layer: 'project', ...(pick.root ? { root: pick.root } : {}) };

/**
 * The Memory page of one Settings dialog. Its pick and unsaved edits live
 * here, so they survive a visit to another tab.
 * @param {{ root?: string }} [start] Open on this directory's project memory.
 * @returns {() => HTMLElement} Builds the page.
 */
export function memoryPane(start = {}) {
  const agent = memoryAgent();
  const projects = agent?.memory_projects ? projectChoices() : [];
  const here = start.root ?? scopeRoot();
  /** @type {Pick} */
  const pick = {
    layer: start.root && projects.length ? 'project' : 'user',
    root: (projects.find((p) => p.root === here) ?? projects[0])?.root,
  };
  /** @type {Map<string, LayerState>} loaded Notes, by scope */
  const layers = new Map();
  return () => buildPane(agent?.name ?? '', pick, layers, projects);
}

/**
 * @param {string} agent
 * @param {Pick} pick
 * @param {Map<string, LayerState>} layers
 * @param {Project[]} projects Empty: no project scope.
 */
function buildPane(agent, pick, layers, projects) {
  const panel = document.createElement('div');
  panel.className = 'lv-settings-panel lv-memory-pane';
  panel.innerHTML = `
    <div class="lv-memory-scope"></div>
    <p class="lv-memory-hint"></p>
    <label class="lv-vh" for="memory-editor">${t('memory.editorLabel')}</label>
    <textarea id="memory-editor" class="lv-dialog-input lv-memory-editor" spellcheck="false" disabled></textarea>
    <div class="lv-memory-foot">
      <span class="lv-memory-meter"></span>
      <div class="lv-memory-actions">
        <button type="button" class="lv-btn lv-btn-ghost lv-memory-clear">${t('memory.clear')}</button>
        <button type="button" class="lv-btn lv-btn-ghost lv-memory-dream">${icon('sparkles', { size: 14 })} ${t('memory.dream')}</button>
        <button type="button" class="lv-btn lv-btn-primary lv-memory-save">${t('dialog.save')}</button>
      </div>
    </div>`;
  const $ = (/** @type {string} */ sel) => /** @type {HTMLElement} */ (panel.querySelector(sel));
  const scopeRow = $('.lv-memory-scope');
  const hint = $('.lv-memory-hint');
  const meter = $('.lv-memory-meter');
  const editor = /** @type {HTMLTextAreaElement} */ ($('.lv-memory-editor'));
  const clearBtn = /** @type {HTMLButtonElement} */ ($('.lv-memory-clear'));
  const dreamBtn = /** @type {HTMLButtonElement} */ ($('.lv-memory-dream'));
  const saveBtn = /** @type {HTMLButtonElement} */ ($('.lv-memory-save'));

  scopeRow.hidden = !projects.length; // only "About me"
  const layerSeg = segmented(
    [
      ['user', t('memory.tabUser')],
      ['project', t('memory.tabProject')],
    ],
    pick.layer,
    (v) => {
      pick.layer = /** @type {'user' | 'project'} */ (v);
      void show();
    },
  );
  layerSeg.setAttribute('aria-label', t('memory.scope'));
  const projectSel = document.createElement('select');
  projectSel.className = 'lv-dialog-input lv-settings-select';
  projectSel.setAttribute('aria-label', t('memory.project'));
  for (const p of projects) projectSel.add(new Option(p.name, p.root ?? ''));
  projectSel.value = pick.root ?? '';
  // Left-aligned, the select after the tabs: it comes and goes without
  // moving them.
  scopeRow.append(layerSeg, projectSel);

  const projectName = () => projects.find((p) => p.root === pick.root)?.name ?? '';

  /** The Notes on screen; null while they load or when they couldn't. */
  let active = /** @type {LayerState | null} */ (null);
  let busy = false;
  let seq = 0;

  const dirty = () => [...layers.values()].some((s) => s.draft !== s.saved);
  const syncButtons = () => {
    editor.disabled = busy || !active;
    dreamBtn.disabled = busy || !active;
    clearBtn.disabled = busy || !active;
    saveBtn.disabled = busy || !dirty();
    projectSel.disabled = busy;
    for (const radio of layerSeg.querySelectorAll('input')) radio.disabled = busy;
  };

  const syncMeter = () => {
    meter.classList.remove('lv-warn');
    if (!active) return;
    const { budget, dreamedAt } = active;
    const used = editor.value.length;
    const dropped = droppedLines(editor.value);
    const bits = [
      t('memory.chars', { used: used.toLocaleString(), budget: budget.toLocaleString() }),
    ];
    if (dropped) bits.push(t('memory.dropped', { n: dropped }));
    if (dreamedAt)
      bits.push(t('memory.dreamedAt', { when: new Date(dreamedAt * 1000).toLocaleDateString() }));
    meter.textContent = bits.join(' · ');
    meter.classList.toggle('lv-warn', dropped > 0 || used > budget);
  };

  // Puts the picked Notes on screen, loading them the first time.
  async function show() {
    const project = pick.layer === 'project';
    // One project: its name is the hint's, a select would offer no choice.
    projectSel.hidden = !project || projects.length < 2;
    // Hidden, not removed: the buttons keep their places either way.
    clearBtn.style.visibility = project ? '' : 'hidden';
    editor.placeholder = t(project ? 'memory.placeholderProject' : 'memory.placeholder');
    const setHint = () => {
      hint.textContent = project
        ? t('memory.hintProject', { name: projectName() })
        : t('memory.hint');
    };
    setHint();

    const where = whereOf(agent, pick);
    const key = JSON.stringify(where);
    const token = ++seq;
    active = layers.get(key) ?? null;
    if (!active) {
      editor.value = '';
      meter.textContent = t('memory.loading');
      syncButtons();
      try {
        const data = await api.getMemory(where);
        if (token !== seq) return;
        // Without directories the project's name comes with its notes.
        const p = project && projects.find((c) => c.root === pick.root);
        if (p && !p.name) {
          p.name = workspaceName(data.scope_key);
          setHint();
        }
        active = layerState(where, data);
        layers.set(key, active);
      } catch (err) {
        if (token !== seq) return;
        meter.textContent = err.message || t('memory.loadFailed');
        meter.classList.add('lv-warn');
        syncButtons();
        return;
      }
    }
    editor.value = active.draft;
    syncMeter();
    syncButtons();
  }

  const setBusy = (on) => {
    busy = on;
    syncButtons();
  };

  // Saves every Notes edited on this page, the ones out of view too.
  async function save() {
    setBusy(true);
    try {
      for (const state of layers.values()) {
        if (state.draft === state.saved) continue;
        const data = await api.putMemory({ ...state.where, content: state.draft });
        state.saved = state.draft = data.content;
      }
      if (active) editor.value = active.draft;
      toast(t('toast.memorySaved'));
    } catch (err) {
      toast(err.message || t('memory.saveFailed'), { type: 'error' });
    } finally {
      setBusy(false);
      syncMeter();
    }
  }

  // The dream pass rewrites the whole list server-side (the previous body
  // lands in MEMORY.md.bak); the editor refreshes to the tidied result.
  async function dreamNow() {
    const state = /** @type {LayerState} */ (active);
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
    const state = /** @type {LayerState} */ (active);
    const name = projectName();
    if (!(await confirmDialog(t('memory.clearConfirm', { name })))) return;
    setBusy(true);
    try {
      const data = await api.clearMemory(state.where);
      Object.assign(state, layerState(state.where, data));
      editor.value = data.content;
      toast(t('toast.memoryCleared', { name }));
    } catch (err) {
      toast(err.message || t('memory.clearFailed'), { type: 'error' });
    } finally {
      setBusy(false);
      syncMeter();
    }
  }

  projectSel.addEventListener('change', () => {
    pick.root = projectSel.value || undefined;
    void show();
  });
  editor.addEventListener('input', () => {
    if (active) active.draft = editor.value;
    syncMeter();
    syncButtons();
  });
  editor.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      if (!saveBtn.disabled) void save();
    }
  });
  saveBtn.addEventListener('click', save);
  dreamBtn.addEventListener('click', dreamNow);
  clearBtn.addEventListener('click', clearProject);

  void show();
  return panel;
}
