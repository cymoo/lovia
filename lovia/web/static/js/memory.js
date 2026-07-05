// Memory editor — what the agent remembers about you, editable.
//
// The Memory plugin's Notes are a small always-in-context fact list, stored as
// "- fact" markdown lines. This dialog shows that body verbatim and saves a
// wholesale replacement; the server re-applies the plugin's own normalization
// (bullets only, dedup), so the meter warns *before* saving when a line would
// be dropped. Opened from the sidebar footer; visible only for agents that
// carry the plugin (AgentInfo.memory).
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
      <h3>Memory${who}</h3>
      <button type="button" class="btn-icon memory-close" aria-label="Close">${icon('x', { size: 16 })}</button>
    </div>
    <p class="memory-hint">Durable facts the agent carries into every chat, one per
      <code>- fact</code> line. Edits apply on save; lines that aren't bullets are dropped.</p>
    <label class="vh" for="memory-editor">Memory notes</label>
    <textarea id="memory-editor" class="dialog-input memory-editor" spellcheck="false"
      placeholder="- The user prefers …" disabled></textarea>
    <div class="memory-foot">
      <span class="memory-meter">Loading…</span>
      <div class="memory-actions">
        <button type="button" class="btn btn-ghost memory-cancel">Cancel</button>
        <button type="button" class="btn btn-primary memory-save" disabled>Save</button>
      </div>
    </div>`;

  const editor = panel.querySelector('.memory-editor');
  const meter = panel.querySelector('.memory-meter');
  const saveBtn = panel.querySelector('.memory-save');
  let budget = 0;

  const dialog = showDialog({ body: panel });
  dialog.classList.add('dialog-wide');
  panel.querySelector('.memory-close').addEventListener('click', () => dialog.close());
  panel.querySelector('.memory-cancel').addEventListener('click', () => dialog.close());

  const syncMeter = () => {
    const used = editor.value.length;
    const dropped = droppedLines(editor.value);
    const bits = [`${used.toLocaleString()} / ${budget.toLocaleString()} chars`];
    if (dropped) bits.push(`${dropped} non-bullet ${dropped === 1 ? 'line' : 'lines'} will be dropped`);
    meter.textContent = bits.join(' · ');
    meter.classList.toggle('warn', dropped > 0 || used > budget);
  };

  async function save() {
    saveBtn.disabled = true;
    try {
      await api.putMemory({ agent: store.agent, content: editor.value });
      toast('Memory saved');
      dialog.close();
    } catch (err) {
      toast(err.message || 'Couldn’t save memory', { type: 'error' });
      saveBtn.disabled = false;
    }
  }

  try {
    const data = await api.getMemory({ agent: store.agent });
    budget = data.budget;
    editor.value = data.content;
    editor.disabled = false;
    saveBtn.disabled = false;
    syncMeter();
    editor.focus();
    editor.setSelectionRange(editor.value.length, editor.value.length);
  } catch (err) {
    meter.textContent = err.message || 'Couldn’t load memory';
    meter.classList.add('warn');
    return;
  }

  editor.addEventListener('input', syncMeter);
  editor.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      if (!saveBtn.disabled) save();
    }
  });
  saveBtn.addEventListener('click', save);
}

export function initMemory() {
  btn = document.getElementById('memory-btn');
  if (!btn) return;
  btn.addEventListener('click', openMemoryDialog);
  store.on('agents-loaded', updateVisibility);
  store.on('agent-changed', updateVisibility);
}
