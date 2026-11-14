// mcp-config.js — Settings → MCP: the servers every agent whose MCP switch is
// on connects to, once per message. A list (each server's last connection,
// an on/off switch, test, edit, delete) ⇄ a JSON editor that takes the
// mcpServers document other MCP clients use. Secrets never reach the page:
// env and header values arrive masked, and a mask sent back unchanged keeps
// what the server stored (lovia/web/config/mcp.py).
import { api } from './api.js';
import { t } from './i18n.js';
import { toast } from './toast.js';
import { confirmDialog } from './ui.js';
import { formatDateTime, formatTimeSmart } from './util.js';
import { cfg, el, field, ghostBtn, loadConfig, readOnlyNotice, scopeFootnote } from './model-config.js';
import { switchControl } from './agent-config.js';

/** @typedef {{ ok: boolean, at: number, tools: string[], error: string | null }} McpStatus */
/** @typedef {{ name: string, enabled: boolean, transport: 'stdio' | 'http', command: string | null, args: string[], url: string | null, spec: Record<string, unknown>, status: McpStatus | null }} McpServer */

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

/** The Settings → MCP pane. Owns its rerenders; safe to rebuild on tab switches. */
export function buildMcpPane() {
  const pane = el('div', 'lv-cfg-pane');
  /** @type {{ name: 'list' } | { name: 'form', server: string | null }} */
  let view = { name: 'list' };
  let busy = false; // one write at a time: each one rebuilds the agents
  /** Servers being tested now: their rows say so until the answer is in. */
  const testing = new Set();

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
      await loadConfig();
    } catch (err) {
      toast(String(err.message || err), { type: 'error' });
    } finally {
      busy = false;
    }
    render();
  }

  function openForm(/** @type {string | null} */ server) {
    view = { name: 'form', server };
    render();
  }

  function renderList() {
    const c = cfg();
    const mcp = c.mcp;
    /** @type {McpServer[]} */
    const servers = mcp.servers;
    const box = el('div');

    const head = el('div', 'lv-cfg-pane-head');
    head.appendChild(el('span', 'lv-cfg-pane-title', t('mcp.title')));
    if (servers.length) {
      const add = el('button', 'lv-btn lv-btn-sm', `＋ ${t('mcp.add')}`);
      add.setAttribute('type', 'button');
      add.toggleAttribute('disabled', !c.writable);
      add.addEventListener('click', () => openForm(null));
      head.appendChild(add);
    }
    box.appendChild(head);
    box.appendChild(el('div', 'lv-cfg-hint lv-mcp-intro', t('mcp.intro')));

    if (!servers.length) {
      const empty = el('div', 'lv-mcp-empty');
      empty.appendChild(el('div', 'lv-cfg-hint', t('mcp.empty')));
      const add = el('button', 'lv-btn lv-btn-primary lv-btn-sm', `＋ ${t('mcp.add')}`);
      add.setAttribute('type', 'button');
      add.toggleAttribute('disabled', !c.writable);
      add.addEventListener('click', () => openForm(null));
      empty.appendChild(add);
      box.append(empty, scopeFootnote());
      return box;
    }

    const list = el('div', 'lv-model-list');
    for (const s of servers) list.appendChild(row(s, c.writable));
    box.append(list, scopeFootnote());
    return box;
  }

  /**
   * One server's row: name and how it connects, its last connection (and
   * the tools it had), then its controls.
   * @param {McpServer} s
   * @param {boolean} writable
   */
  function row(s, writable) {
    const node = el('div', 'lv-model-row lv-mcp-row');
    if (!s.enabled) node.classList.add('lv-off');
    const info = el('div', 'lv-model-row-info');

    const name = el('div', 'lv-model-row-name', s.name);
    name.appendChild(el('span', 'lv-badge lv-badge-plain', s.transport === 'stdio' ? 'stdio' : 'HTTP'));
    if (!s.enabled) name.appendChild(el('span', 'lv-badge lv-badge-plain', t('mcp.off')));
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
    const sw = switchControl(s.enabled);
    sw.node.setAttribute('aria-label', t('mcp.enabled', { name: s.name }));
    sw.node.title = t(s.enabled ? 'mcp.turnOff' : 'mcp.turnOn');
    sw.node.toggleAttribute('disabled', !writable);
    sw.node.addEventListener('change', () =>
      act(() => api.setMcpServerEnabled(s.name, sw.value())),
    );
    actions.append(test, edit, del);
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
    const back = el('button', 'lv-btn-icon lv-cfg-back', '‹');
    back.setAttribute('type', 'button');
    back.setAttribute('aria-label', t('cfg.backLabel'));
    const leave = () => {
      view = { name: 'list' };
      render();
    };
    back.addEventListener('click', leave);
    const title = el('span', 'lv-cfg-pane-title');
    if (stored) title.append(`${t('mcp.edit')} `, el('span', 'lv-mcp-title-name', stored.name));
    else title.textContent = t('mcp.newTitle');
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
    form.appendChild(
      field(t('mcp.json'), area, hasSecrets ? `${t('mcp.jsonHint')} ${t('mcp.maskHint')}` : t('mcp.jsonHint')),
    );
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
        else await api.addMcpServers(document);
        await loadConfig();
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
  // cached may be behind: show it now, then what the server says.
  if (cfg()) {
    loadConfig().then(
      () => {
        if (view.name === 'list') render();
      },
      () => {},
    );
  }
  return pane;
}
