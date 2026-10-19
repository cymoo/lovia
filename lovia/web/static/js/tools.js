// Tool cards — the transcript's view of one tool call: its summary, the full
// parameters, and the result (text, an image strip, or a UI component).
//
// The live stream and history replay (chat.js) both draw a card with
// buildToolNode and settle it with setToolResult, so a reopened chat shows
// exactly what the live run did. Each card carries a ToolState, mirrored onto
// it as `data-status` — public, like `data-tool`, for deployer CSS.

import { api } from './api.js';
import {
  mountComponent,
  parseEnvelope,
  registerRenderer,
  unmountComponent,
} from './components.js';
import { t } from './i18n.js';
import { icon } from './icons.js';
import { store } from './store.js';
import { TOOL_VIEW_KIND, toolInfo, toolViewField, watchToolViews } from './tool-views.js';
import { copyToClipboard, openImageLightbox } from './ui.js';
import { cloneTemplate, escapeHtml, highlightIn } from './util.js';

/** @typedef {'running' | 'awaiting' | 'done' | 'error'} ToolStatus */
/**
 * @typedef {object} ToolState
 * @property {string} id
 * @property {string} name
 * @property {string | undefined} arguments The raw JSON the model emitted;
 *   undefined past ARGS_KEEP_MAX (write_file args can be megabytes).
 * @property {ToolStatus} status
 * @property {string | undefined} result Undefined until the call settles.
 * @property {boolean} isError
 * @property {any[] | undefined} images
 */

// Tools whose `path` argument points into the agent's workspace — their cards
// offer "open in the Files panel", and read_file results highlight by
// extension.
const PATH_TOOLS = new Set(['read_file', 'write_file', 'edit_file']);
// Tools whose completion means workspace files may have changed since the
// Files panel last looked. shell covers its own (foreground) run; the
// background tools are the observation points of a process that keeps
// writing while nobody watches — its start ('shell' again), each output
// poll, and the kill.
export const STALE_TOOLS = new Set(['shell', 'read_process_output', 'kill_process']);
const RESULT_HL_MAX = 200_000; // chars — hljs over megabyte dumps janks the tab
const RESULT_EXPANDABLE_LINES = 12; // roughly what the capped height shows
const ARGS_KEEP_MAX = 256 * 1024;

/** @type {WeakMap<HTMLElement, ToolState>} */
const states = new WeakMap();

// A tool view's `render` draws through the component machinery (errors
// contained, async cleanup, theme re-renders), under a kind of lovia's own.
registerRenderer(TOOL_VIEW_KIND, {
  render: (el, props, ctx) => {
    const tool = /** @type {import('./tool-views.js').ToolInfo} */ (props);
    return toolViewField(tool.name, 'render')?.(el, tool, ctx);
  },
  snapshot: (props, ctx) => {
    const tool = /** @type {import('./tool-views.js').ToolInfo} */ (props);
    return toolViewField(tool.name, 'snapshot')?.(tool, ctx) ?? '';
  },
});

// ---- Arguments -------------------------------------------------------------

function argValue(v) {
  if (typeof v === 'string') {
    const oneLine = v.replace(/\s+/g, ' ').trim();
    return oneLine.length > 60 ? `${oneLine.slice(0, 59)}…` : oneLine;
  }
  return JSON.stringify(v);
}

// old/new pairs (edit_file) ARE a diff: the arguments are already the two
// sides of the change, so no diff algorithm is needed to present them as one.
function isDiffArgs(obj) {
  return typeof obj.old === 'string' && typeof obj.new === 'string';
}

// Arguments in render order. `arguments` is the raw JSON the model emitted and
// its key order is not stable — the same call can come back path-first once and
// new-before-old the next time. Two keys are pinned so a card always reads the
// same way: `path` leads (it is the subject of the call, and the first declared
// parameter of every workspace tool that takes one; `command` plays that role
// for shell-style tools), and a diff pair renders old-then-new at whichever
// side the model mentioned first. Every other key keeps the order it came in.
function argEntries(obj) {
  const entries = Object.entries(obj);
  const diff = isDiffArgs(obj);
  const lead =
    typeof obj.path === 'string' ? 'path'
      : typeof obj.command === 'string' ? 'command'
        : null;
  if (!diff && !lead) return entries;
  const ordered = [];
  let placed = false;
  for (const [k, v] of entries) {
    if (k === lead) continue; // hoisted to the front below
    if (diff && (k === 'old' || k === 'new')) {
      if (placed) continue;
      placed = true;
      ordered.push(['old', obj.old], ['new', obj.new]);
      continue;
    }
    ordered.push([k, v]);
  }
  if (lead) ordered.unshift([lead, obj[lead]]);
  return ordered;
}

// A one-line `(k: v, …)` preview for the tool bubble's summary. The full
// values live in the expanded card's params rows (fillParams). `omit` drops
// keys the card already surfaces elsewhere (e.g. shell's description).
function formatArgs(args, omit) {
  if (!args) return '()';
  let obj;
  try {
    obj = JSON.parse(args);
  } catch {
    return `(${args})`;
  }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return `(${args})`;
  const entries = argEntries(obj).filter(([k]) => !omit?.includes(k));
  if (entries.length === 0) return '()';
  return `(${entries.map(([k, v]) => `${k}: ${argValue(v)}`).join(', ')})`;
}

/**
 * Full arguments as key/value rows — the one renderer behind both the
 * expanded tool card and the approval card. Values stay plain text: args are
 * model *inputs*, so no linkification. Short values sit inline next to their
 * key; multi-line or long ones become full-width scrollable blocks. Empty
 * args append nothing, leaving the container :empty so CSS hides it.
 * @param {Element | null} container
 * @param {string | undefined} args
 */
export function fillParams(container, args) {
  if (!container || !args) return;
  const addBlock = (text) => {
    const div = document.createElement('div');
    div.className = 'param-val block';
    div.textContent = text;
    container.appendChild(div);
  };
  let obj;
  try {
    obj = JSON.parse(args);
  } catch {
    addBlock(String(args)); // unparsable — show the raw payload
    return;
  }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) {
    addBlock(String(args));
    return;
  }
  // Color the two sides of a diff so an approval is reviewed as a change,
  // not as two look-alike walls of text.
  const isDiff = isDiffArgs(obj);
  for (const [k, v] of argEntries(obj)) {
    if (isDiff && (k === 'old' || k === 'new')) {
      const old = k === 'old';
      const key = document.createElement('div');
      key.className = 'param-key';
      key.textContent = old ? t('tool.old') : t('tool.new');
      const value = document.createElement('div');
      value.className = `param-val block ${old ? 'diff-old' : 'diff-new'}`;
      value.textContent = v;
      container.append(key, value);
      continue;
    }
    let val = typeof v === 'string' ? v : JSON.stringify(v);
    const block = val.includes('\n') || val.length > 80;
    if (block && typeof v !== 'string') val = JSON.stringify(v, null, 2);
    const key = document.createElement('div');
    key.className = 'param-key';
    key.textContent = k;
    const value = document.createElement('div');
    value.className = block ? 'param-val block' : 'param-val';
    value.textContent = val;
    container.append(key, value);
  }
}

/**
 * The `path` argument of a call, when it has one.
 * @param {string | undefined} args
 * @returns {string | null}
 */
export function toolPath(args) {
  try {
    const p = JSON.parse(args || '{}').path;
    return typeof p === 'string' && p ? p : null;
  } catch {
    return null;
  }
}

// ---- Cards -------------------------------------------------------------------

function toolActionBtn(iconName, title) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'tool-action';
  b.title = title;
  b.setAttribute('aria-label', title);
  b.innerHTML = icon(iconName, { size: 12 });
  return b;
}

// The shell tool's optional `description` argument — a model-written one-liner
// for the user. Shown as a muted subtitle under the summary; the command stays
// the summary's primary text (the description is the model's claim about the
// command, not ground truth).
function toolDescription(call) {
  if (call.name !== 'shell') return null;
  try {
    const d = JSON.parse(call.arguments || '{}').description;
    return typeof d === 'string' && d.trim() ? d.trim() : null;
  } catch {
    return null;
  }
}

/**
 * A fresh card for `call`, still running.
 * @param {{ id?: string, name: string, arguments?: string }} call
 * @returns {HTMLElement}
 */
export function buildToolNode(call) {
  const node = cloneTemplate('tmpl-tool');
  node.querySelector('.tool-name').textContent = call.name;
  const desc = toolDescription(call);
  node.querySelector('.tool-args').textContent = formatArgs(
    call.arguments,
    desc ? ['description'] : undefined,
  );
  if (desc) {
    const sub = document.createElement('span');
    sub.className = 'tool-desc';
    sub.textContent = desc;
    sub.title = desc; // ellipsized subtitle stays readable on hover
    node.querySelector('summary')?.appendChild(sub);
  }
  fillParams(node.querySelector('.tool-params'), call.arguments);
  node.dataset.tool = call.name;
  // Identity, so a card already on screen can be recognised rather than drawn
  // twice — see chat.js appendTool.
  if (call.id) node.dataset.callId = call.id;
  states.set(node, {
    id: call.id ?? '',
    name: call.name,
    arguments:
      call.arguments && call.arguments.length <= ARGS_KEEP_MAX ? call.arguments : undefined,
    status: 'running',
    result: undefined,
    isError: false,
    images: undefined,
  });
  node.dataset.status = 'running';
  applySummary(node);
  const path = toolPath(call.arguments);
  if (path && PATH_TOOLS.has(call.name)) {
    node.dataset.toolPath = path;
    const open = toolActionBtn('folder', t('tool.openInFiles'));
    open.classList.add('tool-open-file');
    open.addEventListener('click', (e) => {
      // Inside <summary>: don't let the click also toggle the card.
      e.preventDefault();
      e.stopPropagation();
      store.emit('open-workspace-file', { path });
    });
    node.querySelector('summary')?.appendChild(open);
  }
  return node;
}

/**
 * Mark a card as waiting on the user (an approval, a question) or back to
 * running once they answered. A settled card keeps its status.
 * @param {HTMLElement | null} node
 * @param {'running' | 'awaiting'} status
 */
export function setToolStatus(node, status) {
  const state = node && states.get(node);
  if (!state || state.result !== undefined || state.status === status) return;
  state.status = status;
  node.dataset.status = status;
  refreshTool(node, true);
}

/**
 * Bring a card's view-driven parts up to date: its summary line and, until
 * its result is a UI envelope, the body a tool view draws. Call once a fresh
 * card is in the document; setToolResult and setToolStatus call it too.
 * @param {HTMLElement} node
 * @param {boolean} [live]
 */
export function refreshTool(node, live = false) {
  applySummary(node);
  syncToolUi(node, live);
}

// The one-line summary a tool view gives in place of `name(args)`; the
// default comes back when it gives none (or throws).
function applySummary(node) {
  const state = states.get(node);
  const summary = node.querySelector('summary');
  if (!state || !summary) return;
  let text = null;
  const fn = toolViewField(state.name, 'summary');
  if (fn) {
    try {
      const out = fn(toolInfo(state));
      if (typeof out === 'string' && out.trim()) text = out.trim();
    } catch (err) {
      console.error(`lovia tool view "${state.name}" summary failed:`, err);
    }
  }
  let el = summary.querySelector(':scope > .tool-summary');
  if (text === null) {
    el?.remove();
    node.classList.remove('tool--summary');
    return;
  }
  if (!el) {
    el = document.createElement('span');
    el.className = 'tool-summary';
    summary.querySelector('.tool-args')?.after(el);
  }
  el.textContent = text;
  // The call it stands for, on hover.
  /** @type {HTMLElement} */ (el).title =
    `${state.name} ${node.querySelector('.tool-args')?.textContent ?? ''}`.trim();
  node.classList.add('tool--summary');
}

// Re-apply a view registered late (a slow extension) to the cards on screen
// it covers. A card showing a result's own UI envelope keeps it as is:
// redrawing would replay it (an html page would come back paused).
watchToolViews((name) => {
  for (const node of document.querySelectorAll('#transcript [data-part="tool"]')) {
    const card = /** @type {HTMLElement} */ (node);
    if (name !== '*' && card.dataset.tool !== name) continue;
    applySummary(card);
    if (card.dataset.ui !== 'envelope') syncToolUi(card, false);
  }
});

/**
 * The tool card for `id` anywhere in the transcript — the live registry first,
 * then the DOM, which is where a card replayed by the history render lives.
 * @param {string} id Tool call id.
 * @returns {HTMLElement | null}
 */
export function findToolNode(id) {
  const known = store.toolNodes.get(id);
  if (known?.isConnected) return known;
  const escaped = window.CSS?.escape ? CSS.escape(id) : id.replace(/["\\]/g, '\\$&');
  return /** @type {HTMLElement | null} */ (
    document.querySelector(`#transcript .tool[data-call-id="${escaped}"]`)
  );
}

// ---- Results -----------------------------------------------------------------

// Escape arbitrary text and turn bare http(s) URLs into clickable links. Used
// for tool-result <pre> blocks so links work without markdown-rendering (which
// would mangle code / shell output); every non-URL character is escaped.
function linkifyText(text) {
  const urlRe = /https?:\/\/[^\s<>"')\]]+/g;
  let out = '';
  let last = 0;
  let m;
  while ((m = urlRe.exec(text)) !== null) {
    out += escapeHtml(text.slice(last, m.index));
    const url = m[0];
    out += `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(url)}</a>`;
    last = m.index + url.length;
  }
  out += escapeHtml(text.slice(last));
  return out;
}

// Language for a highlighted result: read_file content only — shell output is
// mixed text where wrong highlighting is worse than none.
function resultLang(node) {
  if (node.dataset.tool !== 'read_file') return null;
  const ext = (node.dataset.toolPath?.split('.').pop() || '').toLowerCase();
  return /^[a-z0-9]{1,8}$/.test(ext) ? ext : null;
}

// A successful read_file result opens with the renderer's pagination header,
// "path (lines X-Y of Z)". The model needs it to page through big files; the
// card doesn't — the path is already in the summary. Split it off so copy and
// highlighting see pure file content, and render the range as a meta chip.
// The empty-file / past-eof variants (and any custom result_renderer format)
// simply never match and pass through whole.
const READ_FILE_HEADER = /^.+ \(lines (\d+)-(\d+) of (\d+)\)(?:\n|$)/;

function splitReadFileHeader(node, text, isError) {
  if (isError || node.dataset.tool !== 'read_file') return { text, meta: null };
  const m = READ_FILE_HEADER.exec(text);
  if (!m) return { text, meta: null };
  return {
    text: text.slice(m[0].length),
    meta: { start: +m[1], end: +m[2], total: +m[3] },
  };
}

// Images a tool result carried (view_image & friends): rendered from the
// transcript-serving route, so the strip shows exactly the pixels the model
// saw — even for files since changed, deleted, or outside the workspace.
// Idempotent across repeated result updates; a fetch failure degrades each
// image to a small chip instead of a broken-image glyph.
function renderToolImages(node, images) {
  const box = node.querySelector('.tool-result-box');
  if (!box) return;
  let strip = box.querySelector('.tool-result-images');
  if (!images?.length) {
    strip?.remove();
    return;
  }
  const callId = node.dataset.callId;
  if (!callId || !store.sessionId) return;
  if (!strip) {
    strip = document.createElement('div');
    strip.className = 'tool-result-images';
    box.append(strip);
  }
  // The image IS the payload — surface it instead of hiding it behind the
  // collapsed card (matching how attachments render without a click).
  node.open = true;
  strip.replaceChildren();
  for (const im of images) {
    const img = document.createElement('img');
    img.className = 'tool-result-image';
    img.loading = 'lazy';
    img.alt = t('tool.image');
    img.src = api.toolImageUrl(store.sessionId, callId, im.index ?? 0);
    img.addEventListener('error', () => {
      const chip = document.createElement('span');
      chip.className = 'tool-image-chip';
      chip.innerHTML = icon('image', { size: 13 }); // our own SVG, trusted
      const label = document.createElement('span');
      // textContent, not markup: mime_type is tool-supplied text.
      label.textContent = im.mime_type || 'image';
      chip.append(label);
      img.replaceWith(chip);
    });
    img.addEventListener('click', () =>
      openImageLightbox(img.src, { alt: img.alt }),
    );
    strip.append(img);
  }
}

// The card's UI, drawn just above the result box (the raw output and params
// stay one "Source" toggle away): a result's UI envelope first, else a tool
// view's `render`. Idempotent across repeated updates; with neither, any UI
// is torn down.
function syncToolUi(node, live) {
  const box = node.querySelector('.tool-result-box');
  const state = states.get(node);
  if (!box || !state) return;
  let comp = state.isError || state.result === undefined
    ? null
    : parseEnvelope(state.result, state.arguments);
  if (!comp && toolViewField(state.name, 'render')) {
    comp = { kind: TOOL_VIEW_KIND, props: toolInfo(state) };
  }
  let el = /** @type {HTMLElement | null} */ (node.querySelector(':scope > .tool-ui'));
  if (!comp) {
    if (el) {
      unmountComponent(el);
      el.remove();
    }
    node.classList.remove('tool--ui', 'show-source');
    node.querySelector('.tool-source')?.remove();
    delete node.dataset.ui;
    return;
  }
  node.dataset.ui = comp.kind === TOOL_VIEW_KIND ? 'view' : 'envelope';
  if (!el) {
    el = document.createElement('div');
    el.className = 'tool-ui';
    box.before(el); // outside the box, clear of its hover actions
  }
  node.classList.add('tool--ui');
  node.open = true; // the component IS the payload, like the image strip
  if (!node.querySelector('.tool-source')) {
    const src = toolActionBtn('code', t('tool.source'));
    src.classList.add('tool-source');
    src.addEventListener('click', (e) => {
      e.preventDefault(); // inside <summary>: don't toggle the card
      e.stopPropagation();
      node.open = true;
      node.classList.toggle('show-source');
    });
    node.querySelector('summary')?.appendChild(src);
  }
  mountComponent(el, comp, { callId: node.dataset.callId ?? null, live });
}

/**
 * Settle a card with its call's result — the one renderer behind both the
 * live tool_result event and history replay: content (highlighted or
 * linkified), error styling, the image strip, UI components, and the hover
 * actions (copy, expand when clipped). Idempotent across repeated updates.
 * @param {HTMLElement} node
 * @param {unknown} result
 * @param {boolean} isError
 * @param {any[] | undefined} images
 * @param {boolean} [live] The result just arrived on the stream (components
 *   see it as ctx.live), rather than a reopened chat replaying it.
 */
export function setToolResult(node, result, isError, images, live = false) {
  const state = states.get(node);
  if (state) {
    state.result = String(result ?? '');
    state.isError = isError;
    state.images = images;
    state.status = isError ? 'error' : 'done';
  }
  node.dataset.status = isError ? 'error' : 'done';
  renderToolImages(node, images);
  refreshTool(node, live);
  const pre = /** @type {HTMLElement | null} */ (node.querySelector('.tool-result'));
  if (!pre) return;
  const { text, meta } = splitReadFileHeader(node, String(result ?? ''), isError);
  if (!text.trim()) {
    pre.style.display = 'none';
    node.querySelector('.tool-result-meta')?.remove();
    return;
  }
  // Undo the hide above: a card replayed while its call was still pending got
  // an empty result first and the real one only after the run resumed.
  pre.style.display = '';
  // Highlight only content the header vouches for: a headerless read_file
  // result is a notice (empty file, custom renderer), not code.
  const lang = meta && text.length <= RESULT_HL_MAX ? resultLang(node) : null;
  if (lang) {
    const code = document.createElement('code');
    code.className = `language-${lang}`;
    code.textContent = text;
    pre.replaceChildren(code);
    highlightIn(node);
  } else {
    pre.innerHTML = linkifyText(text);
  }
  if (isError) node.classList.add('error');

  const box = node.querySelector('.tool-result-box');
  if (!box) return;
  let metaEl = box.querySelector('.tool-result-meta');
  if (meta) {
    if (!metaEl) {
      metaEl = document.createElement('div');
      metaEl.className = 'tool-result-meta';
      pre.before(metaEl);
    }
    metaEl.textContent =
      meta.start === 1 && meta.end >= meta.total
        ? t('tool.linesAll', { total: meta.total })
        : t('tool.lines', meta);
  } else {
    metaEl?.remove(); // idempotent across repeated result updates
  }
  let actions = box.querySelector('.tool-result-actions');
  if (!actions) {
    actions = document.createElement('div');
    actions.className = 'tool-result-actions';
    box.prepend(actions);
  }
  actions.replaceChildren(); // idempotent across repeated result updates

  if (text.length > 1500 || text.split('\n').length > RESULT_EXPANDABLE_LINES) {
    const expand = toolActionBtn('maximize-2', t('tool.expand'));
    expand.addEventListener('click', () => {
      const on = pre.classList.toggle('expanded');
      expand.title = on ? t('tool.collapse') : t('tool.expand');
    });
    actions.append(expand);
  }
  const copy = toolActionBtn('copy', t('tool.copyResult'));
  copy.addEventListener('click', async () => {
    if (await copyToClipboard(text)) {
      copy.innerHTML = icon('check', { size: 12 });
      setTimeout(() => {
        copy.innerHTML = icon('copy', { size: 12 });
      }, 1500);
    }
  });
  actions.append(copy);
}
