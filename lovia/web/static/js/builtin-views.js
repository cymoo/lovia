// How lovia's built-in tools read on their cards: "Read file a.md" instead of
// `read_file (path: a.md)`, and the short label an activity group's digest
// uses. Registered as defaults (tool-views.js setDefaultView), so any view a
// deployer registers — the '*' fallback included — still wins. A call whose
// arguments don't fit (a renamed or look-alike tool) keeps the plain
// `name(args)` line.

import { t } from './i18n.js';
import { icon } from './icons.js';
import { setDefaultView } from './tool-views.js';

/** The event a started chat's card fires to open it (sessions.js listens). */
export const OPEN_SESSION_EVENT = 'lovia-open-session';

/** @param {unknown} v */
const str = (v) => (typeof v === 'string' && v.trim() ? v.replace(/\s+/g, ' ').trim() : null);
/** @param {unknown} v */
const quoted = (v) => (str(v) ? `“${str(v)}”` : null);

/**
 * The parts of a tool's line: its label key, and what it acted on (from the
 * arguments; null when they lack it, which keeps the default line).
 * @type {Record<string, [string, ((args: any) => string | null) | null]>}
 */
const TOOLS = {
  read_file: ['toolview.readFile', (a) => str(a.path)],
  write_file: ['toolview.writeFile', (a) => str(a.path)],
  edit_file: ['toolview.editFile', (a) => str(a.path)],
  view_image: ['toolview.viewImage', (a) => str(a.path)],
  // `.` is the workspace root, as an omitted path is: the bare label for both.
  list_files: ['toolview.listFiles', (a) => str(a.path)?.replace(/^\.\/?$/, '') ?? ''],
  grep_files: ['toolview.grepFiles', (a) => quoted(a.pattern)],
  read_process_output: ['toolview.processOutput', (a) => str(a.process_id)],
  kill_process: ['toolview.killProcess', (a) => str(a.process_id)],
  web_search: ['toolview.webSearch', (a) => quoted(a.query)],
  web_fetch: ['toolview.webFetch', (a) => str(a.url)],
  // Retired tool names: older chats still hold their calls.
  read_page: ['toolview.webFetch', (a) => str(a.url)],
  http_request: ['toolview.http', (a) => str(a.url) && `${str(a.method) ?? 'GET'} ${str(a.url)}`],
  recall: ['toolview.recall', (a) => quoted(a.query)],
  remember: ['toolview.remember', (a) => quoted(a.fact)],
  forget: ['toolview.forget', (a) => quoted(a.fact)],
  now: ['toolview.now', null],
  show_table: ['toolview.table', (a) => str(a.title) ?? ''],
  show_html: ['toolview.page', (a) => str(a.title) ?? ''],
  show_chart: ['toolview.chart', (a) => chartTitle(a.option) ?? ''],
  start_session: ['toolview.startSession', (a) => quoted(a.title) ?? ''],
};

/** @param {any} option An ECharts option: `title` is an object or a list. */
function chartTitle(option) {
  const title = Array.isArray(option?.title) ? option.title[0] : option?.title;
  return str(title?.text);
}

/**
 * The card line for a call of a built-in tool, or null for the default.
 * @param {string} name
 * @param {unknown} args
 * @returns {string | null}
 */
export function builtinSummary(name, args) {
  if (name === 'shell') return args && typeof args === 'object' ? withPrompt(/** @type {any} */ (args).command) : null;
  const spec = TOOLS[name];
  if (!spec) return null;
  const [key, subject] = spec;
  if (!subject) return t(key);
  if (!args || typeof args !== 'object') return null;
  const what = subject(args);
  if (what === null) return null;
  return what ? `${t(key)} ${what}` : t(key);
}

// A shell card reads as the command line itself.
/** @param {unknown} command */
function withPrompt(command) {
  const line = str(command);
  return line && `$ ${line}`;
}

/**
 * The chat a start_session call opened, from its result.
 * @param {string | undefined} result
 * @returns {{ session_id: string, title?: string } | null}
 */
function startedChat(result) {
  try {
    const chat = JSON.parse(result ?? '');
    return typeof chat?.session_id === 'string' ? chat : null;
  } catch {
    return null;
  }
}

// The card of a chat start_session opened is the way there. A view draws a
// card's body in every state, so a failed call shows its error here.
/** @type {import('./tool-views.js').ToolView['render']} */
function renderStartedChat(el, tool) {
  el.replaceChildren();
  if (tool.isError) {
    const notice = document.createElement('div');
    notice.className = 'tool-ui-notice';
    notice.textContent = tool.result ?? '';
    el.append(notice);
    return;
  }
  const chat = startedChat(tool.result);
  if (!chat) return;
  const open = document.createElement('button');
  open.type = 'button';
  open.className = 'btn btn-ghost btn-sm chat-link';
  open.innerHTML = icon('square-pen', { size: 14 });
  const title = document.createElement('span');
  title.textContent = chat.title || t('session.newChat');
  open.append(title);
  open.insertAdjacentHTML('beforeend', icon('chevron-right', { size: 14 }));
  open.title = t('toolview.openChat');
  open.addEventListener('click', () => {
    open.dispatchEvent(
      new CustomEvent(OPEN_SESSION_EVENT, { bubbles: true, detail: chat.session_id }),
    );
  });
  el.append(open);
}

for (const name of ['shell', ...Object.keys(TOOLS)]) {
  const key = name === 'shell' ? 'toolview.shell' : TOOLS[name][0];
  setDefaultView(name, {
    summary: (tool) => builtinSummary(tool.name, tool.args),
    render: name === 'start_session' ? renderStartedChat : undefined,
    label: () => t(key),
  });
}
