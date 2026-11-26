// mcp-config.js — Settings → MCP: the servers chats connect to, once per
// message. A list (each server's last connection, an on/off switch, test,
// edit, delete) ⇄ a JSON editor that takes the mcpServers document other
// MCP clients use. Secrets never reach the page: env and header values
// arrive masked, and a mask sent back unchanged keeps what the server
// stored (lovia/web/config/mcp.py).
//
// Two scopes, one list: "All projects" switches a server for every chat;
// "One project" shows the same servers as that project has them — a switch
// there is the project's alone — above the ones the project's own
// .mcp.json names. Those run only once turned on here, for the entry as
// written: the file arrives with the repository and names commands to run
// (lovia/web/project_mcp.py). A server added in a project's scope is saved
// like any other, off everywhere but there.
import { api } from './api.js';
import { store } from './store.js';
import { t } from './i18n.js';
import { toast } from './toast.js';
import { confirmDialog, segmented } from './ui.js';
import { formatDateTime, formatTimeSmart } from './util.js';
import { backButton, cfg, el, field, ghostBtn, loadConfig, readOnlyNotice } from './model-config.js';
import { switchControl } from './assistant-config.js';
import { scopeRoot, workspaceChoices, workspaceName, workspacesOn } from './workspaces.js';

/** @typedef {{ ok: boolean, at: number, tools: string[], error: string | null }} McpStatus */
/** @typedef {{ name: string, enabled: boolean, transport: 'stdio' | 'http', command: string | null, args: string[], url: string | null, spec: Record<string, unknown>, status: McpStatus | null, projects: string[] }} McpServer */
/** One the project's .mcp.json names. @typedef {{ name: string, spec: any, state: 'on' | 'off' | 'pending', error: string | null, status: McpStatus | null }} OwnServer */
/** A global server as the project has it. @typedef {{ name: string, on: boolean, enabled: boolean, replaced: boolean }} Here */
/** @typedef {{ project: string | null, local: boolean, error: string | null, servers: OwnServer[], global: Here[] }} Listing */
/** The scope on screen; `root` undefined: the server's one directory. @typedef {{ layer: 'global' | 'project', root: string | undefined }} Pick */

const EXAMPLE = `{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"]
    },
    "docs": {
      "type": "http",
      "url": "https://example.com/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}`;

/**
 * What a server runs or where it is, on one line.
 * @param {McpServer} s
 */
function target(s) {
  return s.transport === 'stdio' ? [s.command, ...s.args].join(' ') : s.url || '';
}

/** The same of an entry as a .mcp.json writes it. @param {any} spec */
function specTarget(spec) {
  if (!spec || typeof spec !== 'object') return '';
  if (typeof spec.command === 'string') {
    return [spec.command, ...(Array.isArray(spec.args) ? spec.args : [])].join(' ');
  }
  return String(spec.url ?? spec.httpUrl ?? spec.serverUrl ?? '');
}

/**
 * The last connection, as a line: dot, outcome, tool count, when.
 * @param {McpStatus | null} status
 */
function statusLine(status) {
  const line = el('div', 'lv-mcp-status');
  const dot = el('span', 'lv-mcp-dot');
  line.appendChild(dot);
  if (!status) {
    line.classList.add('lv-mcp-status-none');
    line.appendChild(el('span', '', t('mcp.statusNone')));
    return line;
  }
  line.classList.add(status.ok ? 'lv-mcp-status-ok' : 'lv-mcp-status-fail');
  const when = el('time', 'lv-mcp-when', formatTimeSmart(status.at));
  when.title = formatDateTime(status.at, { seconds: true });
  line.appendChild(
    el('span', '', status.ok ? t('mcp.statusOk', { n: status.tools.length }) : t('mcp.statusFail')),
  );
  line.append(' · ', when);
  return line;
}

function testingLine() {
  const line = el('div', 'lv-mcp-status lv-mcp-status-testing');
  line.append(el('span', 'lv-mcp-dot'), el('span', '', t('mcp.testing')));
  return line;
}

/**
 * The MCP page of one Settings dialog. Its scope lives here, so it
 * survives a visit to another tab.
 * @param {{ root?: string }} [start] Open on this directory's project.
 * @returns {() => HTMLElement} Builds the page.
 */
export function mcpPane(start = {}) {
  // Without directories the server has the one its agent works in: no root
  // to send, and the listing names it.
  const projects = workspacesOn()
    ? workspaceChoices()
        .filter((w) => w.kind !== 'general')
        .map((w) => ({ root: /** @type {string | undefined} */ (w.root) }))
    : [{ root: undefined }];
  // A directory that isn't listed (a chat's, opened from it) is still one.
  if (start.root && workspacesOn() && !projects.some((p) => p.root === start.root)) {
    projects.push({ root: start.root });
  }
  const here = start.root ?? scopeRoot();
  /** @type {Pick} */
  const pick = {
    layer: start.root && projects.length ? 'project' : 'global',
    root: (projects.find((p) => p.root === here) ?? projects[0])?.root,
  };
  return () => buildPane(pick, projects);
}

/**
 * Owns its rerenders; safe to rebuild on tab switches.
 * @param {Pick} pick
 * @param {{ root: string | undefined }[]} projects Empty: no project scope.
 */
function buildPane(pick, projects) {
  const pane = el('div', 'lv-cfg-pane');
  /** @type {{ name: 'list' } | { name: 'form', server: string | null }} */
  let view = { name: 'list' };
  let busy = false; // one write at a time: each one rebuilds the agents
  /** Servers being tested now: their rows say so until the answer is in. */
  const testing = new Set();
  /** The picked project's servers; null in the global scope, or until read. */
  let listing = /** @type {Listing | null} */ (null);
  let asked = 0; // bumped per read: a slower, older answer is dropped

  const inProject = () => pick.layer === 'project';
  const projectName = () => workspaceName(listing?.project ?? pick.root);

  /** Read the picked project's servers; nothing to read in the global scope. */
  async function loadListing() {
    const mine = ++asked;
    if (!inProject()) {
      listing = null;
      return;
    }
    const read = await api.getProjectMcp(pick.root ? { root: pick.root } : {});
    if (mine === asked) listing = read;
  }

  /** Another scope: its servers, then the list. */
  async function rescope() {
    listing = null;
    view = { name: 'list' };
    await rerenderScoped();
    try {
      await loadListing();
    } catch (err) {
      toast(String(err.message || err), { type: 'error' });
    }
    await rerenderScoped();
  }

  /** Render, keeping the keyboard in the scope row it was working: the
   *  row is rebuilt with the list. */
  async function rerenderScoped() {
    const scope = pane.querySelector('.lv-scope-row');
    const at = scope?.contains(document.activeElement) ? document.activeElement?.tagName : null;
    await render();
    if (!at) return;
    const again = pane.querySelector(
      at === 'SELECT' ? '.lv-scope-row select' : '.lv-scope-row input:checked',
    );
    /** @type {HTMLElement | null} */ (again)?.focus();
  }

  /**
   * Test saved servers — each on its own, at once — and show each outcome as
   * it lands. Also run right after a save, so the list never says "not
   * connected yet" about a server just tried in the editor.
   * @param {string[]} names
   */
  function testSaved(names) {
    for (const name of names) {
      if (testing.has(name)) continue;
      testing.add(name);
      api
        .testMcpServers({ name })
        .catch((err) => toast(String(err.message || err), { type: 'error' }))
        .finally(async () => {
          testing.delete(name);
          try {
            await loadConfig();
          } catch {
            /* the next render shows what there is */
          }
          if (view.name === 'list' && pane.isConnected) render();
        });
    }
    if (view.name === 'list') render();
  }

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
    pane.replaceChildren(view.name === 'list' ? renderList() : renderForm(view.server));
    const ro = readOnlyNotice();
    if (ro) pane.prepend(ro);
  }

  /**
   * Run a write, then show the config it leaves; a failure is a toast,
   * since the rerender would wipe anything said in place.
   * @param {() => Promise<unknown>} write
   */
  async function act(write) {
    if (busy) return;
    busy = true;
    try {
      await write();
      await Promise.all([loadConfig(), loadListing()]);
    } catch (err) {
      toast(String(err.message || err), { type: 'error' });
    } finally {
      busy = false;
    }
    render();
    // The line over the composer counts what a project still has to confirm.
    store.emit('project-mcp-changed');
  }

  /** "All projects" ⇄ "One project", and which. */
  function scopeRow() {
    const node = el('div', 'lv-scope-row');
    const seg = segmented(
      [
        ['global', t('pmcp.all')],
        ['project', t('pmcp.one')],
      ],
      pick.layer,
      (v) => {
        pick.layer = /** @type {Pick['layer']} */ (v);
        void rescope();
      },
    );
    seg.setAttribute('aria-label', t('pmcp.scope'));
    node.appendChild(seg);
    // One project: its name is in the intro, a select would offer no choice.
    if (inProject() && projects.length > 1) {
      const select = /** @type {HTMLSelectElement} */ (el('select', 'lv-dialog-input lv-settings-select'));
      select.setAttribute('aria-label', t('pmcp.project'));
      for (const p of projects) select.add(new Option(workspaceName(p.root), p.root ?? ''));
      select.value = pick.root ?? '';
      select.addEventListener('change', () => {
        pick.root = select.value || undefined;
        void rescope();
      });
      node.appendChild(select);
    }
    return node;
  }

  /** The list's head: the page's title, and a button to add a server. */
  function listHead(/** @type {boolean} */ withAdd) {
    const head = el('div', 'lv-cfg-pane-head');
    head.appendChild(el('span', 'lv-cfg-pane-title', t('mcp.title')));
    if (withAdd) head.appendChild(addButton('lv-btn lv-btn-sm'));
    return head;
  }

  function addButton(/** @type {string} */ className) {
    const add = el('button', className, `＋ ${t(inProject() ? 'pmcp.add' : 'mcp.add')}`);
    add.setAttribute('type', 'button');
    add.toggleAttribute('disabled', !cfg().writable);
    add.addEventListener('click', () => openForm(null));
    return add;
  }

  function openForm(/** @type {string | null} */ server) {
    view = { name: 'form', server };
    render();
  }

  function renderList() {
    const c = cfg();
    /** @type {McpServer[]} */
    const servers = c.mcp.servers;
    const box = el('div');
    if (projects.length) box.appendChild(scopeRow());
    if (inProject()) {
      box.append(...renderProject(servers));
      return box;
    }

    box.appendChild(listHead(servers.length > 0));
    box.appendChild(el('div', 'lv-cfg-hint lv-mcp-intro', t('mcp.intro')));

    if (!servers.length) {
      const empty = el('div', 'lv-mcp-empty');
      empty.append(el('div', 'lv-cfg-hint', t('mcp.empty')), addButton('lv-btn lv-btn-primary lv-btn-sm'));
      box.append(empty);
      return box;
    }

    const list = el('div', 'lv-model-list');
    for (const s of servers) list.appendChild(row(s, c.writable));
    box.append(list);
    return box;
  }

  /**
   * One project's scope: the servers its own .mcp.json names, each to
   * confirm, then the added ones as the project has them.
   * @param {McpServer[]} servers
   */
  function renderProject(servers) {
    if (!listing) return [listHead(false), el('div', 'lv-cfg-hint', '…')];
    if (!listing.project) {
      return [listHead(false), el('div', 'lv-cfg-hint lv-mcp-intro', t('pmcp.noProject'))];
    }
    const nodes = [
      listHead(true),
      el('div', 'lv-cfg-hint lv-mcp-intro', t('pmcp.intro', { name: projectName() })),
    ];
    const own = listing.servers.length > 0 || !!listing.error;
    if (own) {
      nodes.push(
        el('div', 'lv-mcp-heading', t('pmcp.own')),
        el('div', 'lv-cfg-hint lv-mcp-intro', t('pmcp.ownIntro')),
      );
      if (listing.servers.length) {
        const list = el('div', 'lv-model-list');
        for (const s of listing.servers) list.appendChild(ownRow(s));
        nodes.push(list);
      }
      if (listing.error) nodes.push(el('div', 'lv-mcp-error', listing.error));
      nodes.push(el('div', 'lv-mcp-heading', t('pmcp.global')));
    }
    if (servers.length) {
      const here = new Map(listing.global.map((g) => [g.name, g]));
      const list = el('div', 'lv-model-list');
      for (const s of servers) {
        // One the listing hasn't caught up with follows its own switch, as
        // the project then has it — still a row of this scope.
        const mine = here.get(s.name) ?? { name: s.name, on: s.enabled, enabled: s.enabled, replaced: false };
        list.appendChild(row(s, cfg().writable, mine));
      }
      nodes.push(list);
    } else {
      nodes.push(el('div', 'lv-cfg-hint', t('pmcp.empty')));
    }
    // Where else a server can come from, for a project that has no file yet.
    if (listing.local && !own) nodes.push(el('div', 'lv-cfg-footnote', t('pmcp.fileHint')));
    return nodes;
  }

  /**
   * A server the project's .mcp.json names: what it runs, the entry as
   * written — what turning it on agrees to — and its switch.
   * @param {OwnServer} s
   */
  function ownRow(s) {
    const node = el('div', 'lv-model-row lv-mcp-row');
    if (s.state !== 'on') node.classList.add('lv-off');
    const info = el('div', 'lv-model-row-info');
    const name = el('div', 'lv-model-row-name', s.name);
    if (s.state === 'pending' && !s.error) name.appendChild(el('span', 'lv-badge', t('pmcp.undecided')));
    const where = el('div', 'lv-model-row-meta lv-mono lv-mcp-target', specTarget(s.spec));
    where.title = specTarget(s.spec);
    info.append(name, where);
    if (s.error) info.appendChild(el('div', 'lv-mcp-error', s.error));
    else if (s.state === 'on') info.appendChild(statusLine(s.status));
    const details = /** @type {HTMLDetailsElement} */ (el('details', 'lv-mcp-tools'));
    details.append(
      el('summary', '', t('pmcp.entry')),
      el('pre', 'lv-mcp-entry', JSON.stringify(s.spec, null, 2)),
    );
    info.appendChild(details);
    const sw = switchControl(s.state === 'on');
    sw.node.setAttribute('aria-label', t('pmcp.switch', { name: s.name }));
    sw.node.toggleAttribute('disabled', !!s.error);
    sw.node.addEventListener('change', () => act(() => setHere('project', s.name, sw.value())));
    node.append(info, sw.node);
    return node;
  }

  /** Turn a server on or off for the picked project. */
  function setHere(/** @type {'project' | 'global'} */ source, /** @type {string} */ name, /** @type {boolean} */ on) {
    return api.setProjectMcp({ ...(pick.root ? { root: pick.root } : {}), source, name, on });
  }

  /**
   * One server's row: name and how it connects, its last connection (and
   * the tools it had), then its controls.
   * @param {McpServer} s
   * @param {boolean} writable
   * @param {Here} [here] In a project's scope: the server as that project
   *   has it, which is what the switch then says and changes.
   */
  function row(s, writable, here) {
    const node = el('div', 'lv-model-row lv-mcp-row');
    const on = here ? here.on && !here.replaced : s.enabled;
    if (!on) node.classList.add('lv-off');
    const info = el('div', 'lv-model-row-info');

    const name = el('div', 'lv-model-row-name', s.name);
    name.appendChild(el('span', 'lv-badge lv-badge-plain', s.transport === 'stdio' ? 'stdio' : 'HTTP'));
    // How this scope differs from the other, where it does.
    let note;
    if (here?.replaced) note = t('pmcp.replaced');
    else if (here) note = here.on === here.enabled ? undefined : t(here.on ? 'pmcp.onHere' : 'pmcp.offHere');
    else if (!s.enabled) note = s.projects.length ? t('mcp.onlyIn', { n: s.projects.length }) : t('mcp.off');
    if (note) name.appendChild(el('span', 'lv-badge lv-badge-plain', note));
    const where = el('div', 'lv-model-row-meta lv-mono lv-mcp-target', target(s));
    where.title = target(s);
    const busyNow = testing.has(s.name);
    info.append(name, where, busyNow ? testingLine() : statusLine(s.status));
    if (!busyNow && s.status && !s.status.ok && s.status.error) {
      info.appendChild(el('div', 'lv-mcp-error', s.status.error));
    }
    if (!busyNow && s.status?.ok && s.status.tools.length) {
      const details = /** @type {HTMLDetailsElement} */ (el('details', 'lv-mcp-tools'));
      details.appendChild(el('summary', '', t('mcp.showTools')));
      const chips = el('div', 'lv-mcp-tool-list');
      for (const tool of s.status.tools) chips.appendChild(el('code', 'lv-mcp-tool', tool));
      details.appendChild(chips);
      info.appendChild(details);
    }
    node.appendChild(info);

    const actions = el('div', 'lv-model-row-actions');
    // A test writes nothing: a read-only config.json doesn't stop it.
    const test = ghostBtn(t('mcp.test'), () => testSaved([s.name]));
    test.toggleAttribute('disabled', busyNow);
    const edit = ghostBtn(t('mcp.edit'), () => openForm(s.name));
    const del = ghostBtn(t('cfg.delete'), async () => {
      if (!(await confirmDialog(t('mcp.deleteConfirm', { name: s.name })))) return;
      await act(() => api.deleteMcpServer(s.name));
    });
    del.classList.add('lv-danger');
    del.toggleAttribute('disabled', !writable);
    const sw = switchControl(on);
    sw.node.setAttribute('aria-label', t(here ? 'pmcp.switch' : 'mcp.enabled', { name: s.name }));
    sw.node.title = t(on ? 'mcp.turnOff' : 'mcp.turnOn');
    // A project's switch is kept beside config.json, not in it.
    sw.node.toggleAttribute('disabled', here ? here.replaced : !writable);
    sw.node.addEventListener('change', () =>
      act(() => (here ? setHere('global', s.name, sw.value()) : api.setMcpServerEnabled(s.name, sw.value()))),
    );
    // Deleting is for every project: from a project's scope, only a server
    // that isn't on for all of them — the others are switched off here.
    if (here?.enabled) actions.append(test, edit);
    else actions.append(test, edit, del);
    // The switch is state, not an action: always in view, unlike the rest.
    node.append(actions, sw.node);
    return node;
  }

  /**
   * The JSON editor: a new document, or one saved server (masked).
   * @param {string | null} serverName
   */
  function renderForm(serverName) {
    const c = cfg();
    /** @type {McpServer | undefined} */
    const stored = serverName ? c.mcp.servers.find((s) => s.name === serverName) : undefined;
    const box = el('div');

    const head = el('div', 'lv-cfg-form-head');
    const back = backButton();
    const leave = () => {
      view = { name: 'list' };
      render();
    };
    back.addEventListener('click', leave);
    const title = el('span', 'lv-cfg-pane-title');
    // A new server in a project's scope is for that project alone.
    const forProject = !stored && inProject() ? listing?.project : null;
    if (stored) title.append(`${t('mcp.edit')} `, el('span', 'lv-mcp-title-name', stored.name));
    else title.textContent = t(forProject ? 'pmcp.add' : 'mcp.newTitle');
    head.append(back, title);
    box.appendChild(head);

    const form = el('div', 'lv-cfg-form lv-mcp-form');
    const area = /** @type {HTMLTextAreaElement} */ (
      el('textarea', 'lv-dialog-input lv-cfg-input lv-mono lv-mcp-json')
    );
    area.spellcheck = false;
    area.wrap = 'off';
    area.setAttribute('autocapitalize', 'off');
    area.placeholder = EXAMPLE;
    area.value = stored ? JSON.stringify({ mcpServers: { [stored.name]: stored.spec } }, null, 2) : '';
    const fit = () => {
      const lines = (area.value || EXAMPLE).split('\n').length;
      area.rows = Math.min(22, Math.max(8, lines + 1));
    };
    fit();
    const hasSecrets = !!stored && ['env', 'headers'].some((k) => {
      const v = stored.spec[k];
      return v && typeof v === 'object' && Object.keys(v).length > 0;
    });
    const hints = [t('mcp.jsonHint')];
    if (hasSecrets) hints.push(t('mcp.maskHint'));
    if (forProject) hints.push(t('pmcp.addHint', { name: projectName() }));
    form.appendChild(field(t('mcp.json'), area, hints.join(' ')));
    const syntax = el('div', 'lv-cfg-error lv-mcp-syntax');
    syntax.hidden = true;
    const results = el('div', 'lv-mcp-results');
    results.setAttribute('aria-live', 'polite');
    form.append(syntax, results);
    box.appendChild(form);

    /** The editor's JSON, or null (said under it) when it isn't any. */
    const parse = () => {
      const raw = area.value.trim();
      if (!raw) {
        syntax.textContent = t('mcp.jsonEmpty');
        syntax.hidden = false;
        return null;
      }
      try {
        const value = JSON.parse(raw);
        syntax.hidden = true;
        area.classList.remove('lv-invalid');
        return value;
      } catch (err) {
        syntax.textContent = t('mcp.jsonInvalid', { error: String(err.message || err) });
        syntax.hidden = false;
        area.classList.add('lv-invalid');
        return null;
      }
    };
    area.addEventListener('input', () => {
      fit();
      // Quiet while typing; say what's wrong again only once it was said.
      if (!syntax.hidden) parse();
    });

    const errBox = el('div', 'lv-cfg-error');
    errBox.hidden = true;
    const actions = el('div', 'lv-cfg-form-actions');
    const test = el('button', 'lv-btn', t('mcp.test'));
    test.setAttribute('type', 'button');
    const save = el('button', 'lv-btn lv-btn-primary', t('cfg.save'));
    save.setAttribute('type', 'button');
    save.toggleAttribute('disabled', !c.writable);
    const cancel = ghostBtn(t('cfg.cancel'), leave);

    test.addEventListener('click', async () => {
      const document = parse();
      if (document === null) return;
      errBox.hidden = true;
      test.toggleAttribute('disabled', true);
      test.textContent = t('mcp.testing');
      results.replaceChildren(el('div', 'lv-cfg-hint', t('mcp.testingHint', { s: c.mcp.timeout })));
      try {
        const out = await api.testMcpServers({ document, original: stored?.name ?? null });
        results.replaceChildren(
          ...out.results.map((/** @type {McpStatus & { name: string }} */ r) => {
            const line = el('div', `lv-mcp-result ${r.ok ? 'lv-mcp-status-ok' : 'lv-mcp-status-fail'}`);
            line.appendChild(el('span', 'lv-mcp-dot'));
            line.appendChild(el('strong', '', r.name));
            line.append(
              ' ',
              r.ok
                ? t('mcp.resultOk', { n: r.tools.length, tools: r.tools.slice(0, 8).join(', ') + (r.tools.length > 8 ? ' …' : '') })
                : r.error || t('mcp.statusFail'),
            );
            return line;
          }),
        );
      } catch (err) {
        results.replaceChildren();
        errBox.textContent = String(err.message || err);
        errBox.hidden = false;
      } finally {
        test.textContent = t('mcp.test');
        test.toggleAttribute('disabled', false);
      }
    });

    const submit = async () => {
      const document = parse();
      if (document === null || save.hasAttribute('disabled')) return;
      errBox.hidden = true;
      save.toggleAttribute('disabled', true);
      try {
        if (stored) await api.updateMcpServer(stored.name, document);
        else await api.addMcpServers(document, forProject ?? undefined);
        await Promise.all([loadConfig(), loadListing()]);
        toast(t('cfg.saved'));
        view = { name: 'list' };
        // The server read the names the way the document gives them.
        const servers = document && typeof document === 'object' ? document.mcpServers ?? document : {};
        testSaved(Object.keys(servers));
      } catch (err) {
        errBox.textContent = String(err.message || err);
        errBox.hidden = false;
        save.toggleAttribute('disabled', false);
      }
    };
    save.addEventListener('click', submit);
    // ⌘/Ctrl+Enter saves from the editor, as in the composer.
    area.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        submit();
      }
    });

    actions.append(test, save, cancel);
    box.append(errBox, actions);
    queueMicrotask(() => area.focus());
    return box;
  }

  render();
  // Runs record each server's connection as they start, so what this page
  // cached may be behind: show it now, then what the server says — with
  // the picked project's servers, which are read every time.
  Promise.all([cfg() ? loadConfig() : null, loadListing()]).then(
    () => {
      if (view.name === 'list') render();
    },
    () => {},
  );
  return pane;
}
