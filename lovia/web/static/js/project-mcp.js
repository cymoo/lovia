// project-mcp.js — the MCP servers of a chat's project: the dialog that
// turns them on or off there (`/mcp`, and a sidebar folder's ⋯ menu), and a
// line over the composer while the project's own are undecided.
//
// Two kinds of server, one switch each:
//
// - the project's own, from its .mcp.json. One runs only once it was turned
//   on here, for the entry as written: the file arrives with the repository
//   and names commands to run. An edited entry is undecided again.
// - the global ones (Settings → MCP), which a project can have on or off
//   for itself whatever their own switch says.
//
// Nothing waits on an answer — the chat runs without a server that isn't
// on — and a change applies from the next run (lovia/web/project_mcp.py).
import { api } from './api.js';
import { store } from './store.js';
import { t } from './i18n.js';
import { toast } from './toast.js';
import { icon } from './icons.js';
import { showDialog } from './ui.js';
import { draftWorkspaceRoot } from './workspaces.js';
import { switchControl } from './assistant-config.js';

/** @typedef {{ name: string, spec: any, state: 'on' | 'off' | 'pending', error: string | null }} ProjectServer */
/** @typedef {{ name: string, spec: any, enabled: boolean, on: boolean, replaced: boolean }} GlobalServer */
/** @typedef {{ project: string | null, local: boolean, error: string | null, servers: ProjectServer[], global: GlobalServer[] }} Listing */
/** Whose project: a chat's, or the directory a draft opens in. @typedef {{ session?: string, root?: string }} Scope */

/** The open chat's project, for the composer line. @type {Listing | null} */
let _listing = null;
let _asked = 0; // bumped per request: a slower, older answer is dropped

/** @returns {Scope} */
function chatScope() {
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
  open.addEventListener('click', () => _listing && openDialog(chatScope(), _listing));
  note.replaceChildren(text, open);
  note.insertAdjacentHTML('afterbegin', icon('server', { size: 15 }));
}

/** @param {string} className @param {string} [text] */
function el(className, text) {
  const node = document.createElement('div');
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/**
 * One server: its name, what it runs, a note or two, and its switch.
 * @param {object} s
 * @param {string} s.name
 * @param {any} s.spec
 * @param {boolean} s.on
 * @param {string} [s.badge] Beside the name.
 * @param {string} [s.note] Under what it runs: how this project differs.
 * @param {string | null} [s.error] Why it can't be used; the switch is off limits then.
 * @param {boolean} [s.fixed] The switch can't be used here.
 * @param {(on: boolean) => void} decide
 */
function row(s, decide) {
  const item = el('lv-pmcp-row');
  const main = el('lv-pmcp-main');
  const head = el('lv-pmcp-name', s.name);
  if (s.badge) {
    const badge = document.createElement('span');
    badge.className = 'lv-badge';
    badge.textContent = s.badge;
    head.appendChild(badge);
  }
  main.append(head, el('lv-pmcp-target', target(s.spec)));
  if (s.note) main.appendChild(el('lv-pmcp-note', s.note));
  if (s.error) main.appendChild(el('lv-pmcp-error', s.error));
  // The whole entry, as written: what turning it on agrees to.
  const detail = document.createElement('details');
  detail.className = 'lv-pmcp-detail';
  const summary = document.createElement('summary');
  summary.textContent = t('pmcp.entry');
  const code = document.createElement('pre');
  code.textContent = JSON.stringify(s.spec, null, 2);
  detail.append(summary, code);
  main.appendChild(detail);

  const sw = switchControl(s.on);
  sw.node.setAttribute('aria-label', t('pmcp.switch', { name: s.name }));
  sw.node.toggleAttribute('disabled', !!s.error || !!s.fixed);
  sw.node.addEventListener('change', () => decide(sw.value()));
  item.append(main, sw.node);
  return item;
}

/**
 * @param {Scope} scope
 * @param {Listing} listing
 */
function openDialog(scope, listing) {
  const body = el('lv-pmcp');

  /** @param {'project' | 'global'} source @param {string} name @param {boolean} on */
  async function decide(source, name, on) {
    try {
      listing = await api.setProjectMcp({ ...scope, source, name, on });
    } catch (err) {
      toast(String(err.message || err), { type: 'error' });
    }
    draw();
    refresh(); // the composer line counts what is still undecided
  }

  /** The project's own: what its .mcp.json names. */
  function own() {
    const nodes = [el('lv-pmcp-heading', t('pmcp.own'))];
    // Nothing listed: there is no file — unless the error below says it
    // is there and unreadable.
    if (listing.servers.length || !listing.error) {
      nodes.push(
        el(
          'lv-pmcp-intro',
          listing.servers.length
            ? t('pmcp.intro', { file: `${listing.project}/.mcp.json` })
            : t('pmcp.none'),
        ),
      );
    }
    for (const s of listing.servers) {
      nodes.push(
        row(
          {
            name: s.name,
            spec: s.spec,
            on: s.state === 'on',
            badge: s.state === 'pending' && !s.error ? t('pmcp.undecided') : undefined,
            error: s.error,
          },
          (on) => decide('project', s.name, on),
        ),
      );
    }
    if (listing.error) nodes.push(el('lv-pmcp-error', listing.error));
    return nodes;
  }

  /** Settings → MCP's, as this project has them. */
  function everywhere() {
    const nodes = [
      el('lv-pmcp-heading', t('pmcp.global')),
      el('lv-pmcp-intro', t(listing.project ? 'pmcp.globalIntro' : 'pmcp.globalNoProject')),
    ];
    for (const s of listing.global) {
      let note;
      if (s.replaced) note = t('pmcp.replaced');
      else if (s.on !== s.enabled) note = t(s.on ? 'pmcp.onHere' : 'pmcp.offHere');
      nodes.push(
        row(
          { name: s.name, spec: s.spec, on: s.on && !s.replaced, note, fixed: !listing.project || s.replaced },
          (on) => decide('global', s.name, on),
        ),
      );
    }
    return nodes;
  }

  function draw() {
    const title = document.createElement('h3');
    title.className = 'lv-pmcp-title';
    title.textContent = t(listing.project ? 'pmcp.title' : 'pmcp.titleChat');
    const list = el('lv-pmcp-list');
    // A project on this machine may have a file of its own; any chat sees
    // the global servers.
    if (listing.local) list.append(...own());
    if (listing.global.length) list.append(...everywhere());
    else if (!listing.local) list.append(el('lv-pmcp-intro', t('pmcp.empty')));
    // Redrawn after a switch: stay where the list was scrolled to.
    const at = body.querySelector('.lv-pmcp-list')?.scrollTop ?? 0;
    body.replaceChildren(title, list, el('lv-cfg-footnote', t('pmcp.applies')));
    list.scrollTop = at;
  }

  draw();
  const actions = el('lv-pmcp-actions');
  const manage = document.createElement('button');
  manage.type = 'button';
  manage.className = 'lv-btn lv-btn-ghost';
  manage.textContent = t('pmcp.manage');
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'lv-btn';
  close.textContent = t('pmcp.done');
  actions.append(manage, close);
  const dialog = showDialog({ body, actions });
  dialog.classList.add('lv-dialog-wide');
  close.addEventListener('click', () => dialog.close());
  manage.addEventListener('click', async () => {
    const { openSettings } = await import('./settings.js');
    openSettings('mcp'); // takes this dialog's place
  });
}

/** @param {Scope} scope */
async function open(scope) {
  try {
    openDialog(scope, await api.getProjectMcp(scope));
  } catch (err) {
    toast(String(err.message || err), { type: 'error' });
  }
}

/** The servers of the project in `root`: a sidebar folder's ⋯ menu. @param {string} root */
export const openProjectMcp = (root) => open({ root });

/** The open chat's servers: `/mcp`. */
export const openChatMcp = () => open(chatScope());

export function initProjectMcp() {
  // The chat's directory changed, or a run that may have edited the file ended.
  store.on('workspace-scope-changed', refresh);
  store.on('run-end', refresh);
  refresh();
}
