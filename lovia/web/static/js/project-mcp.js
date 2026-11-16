// project-mcp.js — a chat's project and its MCP servers, from the composer:
// a line over it while servers the project's own .mcp.json names wait to be
// confirmed, and `/mcp`. Both open Settings → MCP on that project, where
// the servers are listed and switched (mcp-config.js).
//
// Nothing waits on an answer — the chat runs without a server that isn't
// on — and a change applies from the next run (lovia/web/project_mcp.py).
import { api } from './api.js';
import { store } from './store.js';
import { t } from './i18n.js';
import { toast } from './toast.js';
import { icon } from './icons.js';
import { draftWorkspaceRoot } from './workspaces.js';

/** @typedef {import('./mcp-config.js').Listing} Listing */

/** The open chat's project. @type {Listing | null} */
let _listing = null;
let _asked = 0; // bumped per request: a slower, older answer is dropped

/** Whose project: the open chat's, or the directory a draft opens in. */
function chatScope() {
  return store.sessionId ? { session: store.sessionId } : { root: draftWorkspaceRoot() };
}

/** Settings → MCP, on the project `listing` is of — or on every project's. */
async function openSettingsOn(/** @type {Listing} */ listing) {
  const { openSettings } = await import('./settings.js');
  openSettings('mcp', listing.project ? { root: listing.project } : {});
}

async function refresh() {
  if (!store.canConfigure) return;
  const mine = ++_asked;
  let listing = null;
  try {
    listing = await api.getProjectMcp(chatScope());
  } catch {
    /* no project to ask about (a refused directory, say): nothing to show */
  }
  if (mine !== _asked) return;
  _listing = listing;
  renderNote();
}

function renderNote() {
  const note = document.getElementById('mcp-pending');
  if (!note) return;
  const pending = (_listing?.servers ?? []).filter((s) => s.state === 'pending' && !s.error);
  note.hidden = pending.length === 0;
  if (note.hidden) return note.replaceChildren();
  const text = document.createElement('span');
  text.className = 'lv-composer-note-text';
  text.textContent = t('pmcp.pending', { n: pending.length });
  const open = document.createElement('button');
  open.type = 'button';
  open.className = 'lv-btn lv-btn-ghost lv-btn-sm';
  open.textContent = t('pmcp.review');
  open.addEventListener('click', () => _listing && void openSettingsOn(_listing));
  note.replaceChildren(text, open);
  note.insertAdjacentHTML('afterbegin', icon('server', { size: 15 }));
}

/** The open chat's servers: `/mcp`. */
export async function openChatMcp() {
  try {
    await openSettingsOn(await api.getProjectMcp(chatScope()));
  } catch (err) {
    toast(String(err.message || err), { type: 'error' });
  }
}

export function initProjectMcp() {
  // The chat's directory changed, a run that may have edited the file
  // ended, or Settings switched a server.
  store.on('workspace-scope-changed', refresh);
  store.on('run-end', refresh);
  store.on('project-mcp-changed', refresh);
  refresh();
}
