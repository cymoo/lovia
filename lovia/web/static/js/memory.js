// Memory editor — what the agent remembers about you, editable.
//
// The Memory plugin's Notes are a small always-in-context fact list, stored as
// "- fact" markdown lines. This dialog shows that body verbatim and saves a
// wholesale replacement; the server re-applies the plugin's own normalization
// (bullets only, dedup), so the meter warns *before* saving when a line would
// be dropped. Opened from the sidebar footer; visible only for agents that
// carry the plugin (AgentInfo.memory).
import { t } from './i18n.js';
import { api } from './api.js';
import { store } from './store.js';
import { showDialog } from './ui.js';
import { toast } from './toast.js';
import { icon } from './icons.js';

let btn = null;

function updateVisibility() {
  const agent = store.agents.find((a) => a.name === store.agent);
  btn?.classList.toggle('hidden', !agent?.memory);
}

const droppedLines = (content) =>
  content.split('\n').filter((l) => l.trim() && !l.trim().startsWith('- ')).length;

async function openMemoryDialog() {
  const panel = document.createElement('div');
  panel.className = 'memory-panel';
  // Name the agent only when there's more than one to tell apart.
  const who = store.agents.length > 1 && store.agent ? ` — ${store.agent}` : '';
  panel.innerHTML = `
    <div class="memory-head">
      <h3>${t('memory.title')}${who}</h3>
      <button type="button" class="btn-icon memory-close" aria-label="${t('dialog.close')}">${icon('x', { size: 16 })}</button>
    </div>
    <p class="memory-hint">${t('memory.hint')}</p>
    <label class="vh" for="memory-editor">${t('memory.editorLabel')}</label>
    <textarea id="memory-editor" class="dialog-input memory-editor" spellcheck="false"
      placeholder="${t('memory.placeholder')}" disabled></textarea>
    <div class="memory-foot">
      <span class="memory-meter">${t('memory.loading')}</span>
      <div class="memory-actions">
        <button type="button" class="btn btn-ghost memory-dream" disabled
          title="${t('memory.dream')}">${icon('moon', { size: 14 })} ${t('memory.dream')}</button>
        <button type="button" class="btn btn-ghost memory-cancel">${t('dialog.cancel')}</button>
        <button type="button" class="btn btn-primary memory-save" disabled>${t('dialog.save')}</button>
      </div>
    </div>`;

  const editor = /** @type {HTMLTextAreaElement} */ (panel.querySelector('.memory-editor'));
  const meter = panel.querySelector('.memory-meter');
  const saveBtn = /** @type {HTMLButtonElement} */ (panel.querySelector('.memory-save'));
  const dreamBtn = /** @type {HTMLButtonElement} */ (panel.querySelector('.memory-dream'));
  let budget = 0;
  let dreamedAt = null;

  const dialog = showDialog({ body: panel });
  dialog.classList.add('dialog-wide');
  panel.querySelector('.memory-close').addEventListener('click', () => dialog.close());
  panel.querySelector('.memory-cancel').addEventListener('click', () => dialog.close());

  const syncMeter = () => {
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
      bits.push(t('memory.dreamedAt', { when: new Date(dreamedAt * 1000).toLocaleString() }));
    meter.textContent = bits.join(' · ');
    meter.classList.toggle('warn', dropped > 0 || used > budget);
  };

  async function save() {
    saveBtn.disabled = true;
    try {
      await api.putMemory({ agent: store.agent, content: editor.value });
      toast(t('toast.memorySaved'));
      dialog.close();
    } catch (err) {
      toast(err.message || t('memory.saveFailed'), { type: 'error' });
      saveBtn.disabled = false;
    }
  }

  // The dream pass rewrites the whole list server-side (the previous body
  // lands in MEMORY.md.bak); the editor refreshes to the tidied result.
  async function dreamNow() {
    dreamBtn.disabled = true;
    saveBtn.disabled = true;
    editor.disabled = true;
    meter.textContent = t('memory.dreaming');
    try {
      const data = await api.dreamMemory({ agent: store.agent });
      budget = data.budget;
      dreamedAt = data.dreamed_at;
      editor.value = data.content;
      toast(t('toast.memoryDreamed', { before: data.before, after: data.after }));
    } catch (err) {
      toast(err.message || t('memory.dreamFailed'), { type: 'error' });
    } finally {
      editor.disabled = false;
      saveBtn.disabled = false;
      dreamBtn.disabled = false;
      syncMeter();
    }
  }

  try {
    const data = await api.getMemory({ agent: store.agent });
    budget = data.budget;
    dreamedAt = data.dreamed_at;
    editor.value = data.content;
    editor.disabled = false;
    saveBtn.disabled = false;
    dreamBtn.disabled = false;
    syncMeter();
    editor.focus();
    editor.setSelectionRange(editor.value.length, editor.value.length);
  } catch (err) {
    meter.textContent = err.message || t('memory.loadFailed');
    meter.classList.add('warn');
    return;
  }

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
