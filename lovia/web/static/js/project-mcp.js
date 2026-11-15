// project-mcp.js — the MCP servers the chat's project names in its own
// .mcp.json: a line over the composer while any is undecided, and the dialog
// that turns each on or off for this project.
//
// A server runs only once it was turned on here, for the entry as written:
// the file arrives with the repository, and it names commands to run. An
// edited entry is undecided again. Nothing waits on the answer — the chat
// runs without a server that isn't on — and a change applies from the next
// run (lovia/web/project_mcp.py).
import { api } from './api.js';
import { store } from './store.js';
import { t } from './i18n.js';
import { toast } from './toast.js';
import { icon } from './icons.js';
import { showDialog } from './ui.js';
import { draftWorkspaceRoot } from './workspaces.js';
import { switchControl } from './assistant-config.js';

/** @typedef {{ name: string, spec: any, state: 'on' | 'off' | 'pending', error: string | null }} ProjectServer */
/** @typedef {{ project: string | null, error: string | null, servers: ProjectServer[] }} Listing */

/** @type {Listing | null} */
let _listing = null;
let _asked = 0; // bumped per request: a slower, older answer is dropped

/** Which chat's project: the open chat, else where a draft will open. */
function scope() {
  return store.sessionId ? { session: store.sessionId } : { root: draftWorkspaceRoot() };
}

/** What an entry runs or where it is, on one line. @param {any} spec */
export function target(spec) {
  if (!spec || typeof spec !== 'object') return '';
  if (typeof spec.command === 'string') {
    const args = Array.isArray(spec.args) ? spec.args : [];
    return [spec.command, ...args].join(' ');
  }
  return String(spec.url ?? spec.httpUrl ?? spec.serverUrl ?? '');
}

async function refresh() {
  if (!store.canConfigure) return;
  const mine = ++_asked;
  let listing = null;
  try {
    listing = await api.getProjectMcp(scope());
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
  open.addEventListener('click', openDialog);
  note.replaceChildren(text, open);
  note.insertAdjacentHTML('afterbegin', icon('server', { size: 15 }));
}

/** @param {ProjectServer} s @param {() => void} redraw */
function row(s, redraw) {
  const item = document.createElement('div');
  item.className = 'lv-pmcp-row';
  const main = document.createElement('div');
  main.className = 'lv-pmcp-main';
  const head = document.createElement('div');
  head.className = 'lv-pmcp-name';
  head.textContent = s.name;
  if (s.state === 'pending' && !s.error) {
    const badge = document.createElement('span');
    badge.className = 'lv-badge';
    badge.textContent = t('pmcp.undecided');
    head.appendChild(badge);
  }
  const what = document.createElement('div');
  what.className = 'lv-pmcp-target';
  what.textContent = target(s.spec);
  main.append(head, what);
  if (s.error) {
    const why = document.createElement('div');
    why.className = 'lv-pmcp-error';
    why.textContent = s.error;
    main.appendChild(why);
  }
  // The whole entry, as written: what turning it on agrees to.
  const detail = document.createElement('details');
  detail.className = 'lv-pmcp-detail';
  const summary = document.createElement('summary');
  summary.textContent = t('pmcp.entry');
  const code = document.createElement('pre');
  code.textContent = JSON.stringify(s.spec, null, 2);
  detail.append(summary, code);
  main.appendChild(detail);

  const sw = switchControl(s.state === 'on');
  sw.node.setAttribute('aria-label', t('pmcp.switch', { name: s.name }));
  sw.node.toggleAttribute('disabled', !!s.error);
  sw.node.addEventListener('change', async () => {
    try {
      _listing = await api.setProjectMcp({ ...scope(), name: s.name, on: sw.value() });
    } catch (err) {
      toast(String(err.message || err), { type: 'error' });
      await refresh();
    }
    renderNote();
    redraw();
  });
  item.append(main, sw.node);
  return item;
}

function openDialog() {
  const body = document.createElement('div');
  body.className = 'lv-pmcp';
  const draw = () => {
    const listing = _listing;
    const title = document.createElement('h3');
    title.className = 'lv-pmcp-title';
    title.textContent = t('pmcp.title');
    const intro = document.createElement('p');
    intro.className = 'lv-pmcp-intro';
    intro.textContent = t('pmcp.intro', { file: `${listing?.project ?? ''}/.mcp.json` });
    const list = document.createElement('div');
    list.className = 'lv-pmcp-list';
    for (const s of listing?.servers ?? []) list.appendChild(row(s, draw));
    const nodes = [title, intro, list];
    if (listing?.error) {
      const problem = document.createElement('div');
      problem.className = 'lv-pmcp-error';
      problem.textContent = listing.error;
      nodes.push(problem);
    }
    const foot = document.createElement('div');
    foot.className = 'lv-cfg-footnote';
    foot.textContent = t('pmcp.applies');
    nodes.push(foot);
    body.replaceChildren(...nodes);
  };
  draw();
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'lv-btn';
  close.textContent = t('pmcp.done');
  const dialog = showDialog({ body, actions: close });
  dialog.classList.add('lv-dialog-wide');
  close.addEventListener('click', () => dialog.close());
}

export function initProjectMcp() {
  // The chat or its directory changed; a run may have edited the file.
  for (const event of ['workspace-scope-changed', 'session-switched', 'new-chat', 'run-end']) {
    store.on(event, refresh);
  }
  refresh();
}
