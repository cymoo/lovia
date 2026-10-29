// Client-side "export as HTML" — a self-contained, styled snapshot of a chat.
//
// Markdown export stays server-side (see `exportSession` in sessions.js); HTML
// is built here because it reuses the browser's already-loaded marked +
// DOMPurify + highlight.js, so the output matches what the user sees with no new
// server dependency. Source data is the existing JSON export endpoint, which
// carries reasoning + tool calls per message. A UI component draws as its
// renderer's static snapshot (components.js), when it has one.
import { api, apiError } from './api.js';
import { t } from './i18n.js';
import { parseEnvelope, snapshotComponent } from './components.js';
import { TOOL_VIEW_KIND, toolInfo, toolViewField } from './tool-views.js';
import { toast } from './toast.js';
import { escapeHtml } from './util.js';

/**
 * Turn a session title into a safe download filename — replaces filesystem-
 * illegal characters, collapses whitespace, drops trailing dots (Windows), and
 * caps length.
 * @param {string} title
 * @param {string} ext Extension without the dot (e.g. 'md', 'html').
 * @returns {string}
 */
export function exportFilename(title, ext) {
  const base = String(title || '')
    .replace(/[\\/:*?"<>|]/g, ' ') // filesystem-illegal chars → space
    .replace(/\s+/g, ' ') // collapse whitespace (incl. tabs/newlines)
    .trim()
    .replace(/\.+$/, '') // no trailing dots
    .trim()
    .slice(0, 80)
    .trim();
  return `${base || 'lovia-chat'}.${ext}`;
}

function mdToHtml(text) {
  if (!text || !text.trim()) return '';
  // The export is a standalone file that may be opened anywhere, so never emit
  // unsanitized HTML built from (possibly untrusted) chat content: without
  // DOMPurify, fall back to escaped plain text.
  if (typeof marked === 'undefined' || typeof DOMPurify === 'undefined') {
    return `<p>${escapeHtml(text)}</p>`;
  }
  return DOMPurify.sanitize(marked.parse(text));
}

// Content may be a plain string or (multimodal) a part list — mirror the
// server's display_text: use the string, else a JSON dump.
function contentText(content) {
  if (typeof content === 'string') return content;
  if (content == null) return '';
  return JSON.stringify(content, null, 2);
}

/**
 * Call id → sanitized snapshot HTML, for each tool result that is a UI
 * component whose renderer has a snapshot — or whose tool has a view with one.
 * @param {any[]} messages
 * @param {Record<string, string>} toolArgs Call id → raw arguments.
 * @param {Record<string, string>} toolNames Call id → tool name.
 * @param {string | null} sessionId
 * @returns {Promise<Map<string, string>>}
 */
async function snapshotComponents(messages, toolArgs, toolNames, sessionId) {
  const out = new Map();
  if (typeof DOMPurify === 'undefined') return out;
  const purify = DOMPurify;
  await Promise.all(
    messages.map(async (m) => {
      if (m.role !== 'tool' || !m.tool_call_id) return;
      const id = m.tool_call_id;
      const text = contentText(m.content);
      let comp = m.is_error ? null : parseEnvelope(text, toolArgs[id]);
      const name = toolNames[id];
      if (!comp && name && toolViewField(name, 'snapshot')) {
        comp = {
          kind: TOOL_VIEW_KIND,
          props: toolInfo({
            id,
            name,
            arguments: toolArgs[id],
            status: m.is_error ? 'error' : 'done',
            result: text,
            isError: !!m.is_error,
            images: m.images,
          }),
        };
      }
      if (!comp) return;
      const html = await snapshotComponent(comp, { callId: m.tool_call_id, sessionId });
      if (html) out.set(m.tool_call_id, purify.sanitize(html));
    }),
  );
  return out;
}

function renderMessage(m, toolNames = {}, snapshots = new Map()) {
  // A tool *result* message: attribute it to its call's name (results carry no
  // name of their own) and show the raw output in a <pre>, not as markdown.
  // Tool and reasoning blocks render as <details> (collapsed by default) so
  // the exported page reads as the conversation, with the machinery on demand.
  // A UI component is part of the answer instead: its snapshot, shown.
  if (m.role === 'tool') {
    const snapshot = m.tool_call_id && snapshots.get(m.tool_call_id);
    if (snapshot) return `<section class="lv-msg lv-msg-component">${snapshot}</section>`;
    const result = contentText(m.content);
    if (!result.trim()) return '';
    const name = (m.tool_call_id && toolNames[m.tool_call_id]) || m.name || '';
    const label = name ? `Tool result: ${escapeHtml(name)}` : 'Tool result';
    return (
      `<section class="lv-msg lv-msg-tool"><details><summary class="lv-tool-label">${label}</summary>` +
      `<pre class="lv-tool-output">${escapeHtml(result)}</pre></details></section>`
    );
  }

  const text = contentText(m.content);
  const tools = m.tool_calls || [];
  if (!text.trim() && !m.reasoning && !tools.length) return '';

  const role = (m.role || '').replace(/^\w/, (c) => c.toUpperCase());
  const out = [`<section class="lv-msg lv-msg-${(m.role || 'assistant').toLowerCase()}">`];
  out.push(`<div class="lv-msg-role">${escapeHtml(role)}</div>`);
  if (m.reasoning) {
    out.push(
      `<details class="lv-msg-reasoning"><summary class="lv-reasoning-label">💭 Thinking</summary>${mdToHtml(m.reasoning)}</details>`,
    );
  }
  if (text.trim()) out.push(`<div class="lv-msg-body">${mdToHtml(text)}</div>`);
  for (const tc of tools) {
    // Build the <pre><code> directly (escaped) rather than a ```json fence: it's
    // robust to backticks in the arguments and still highlighted by the pass below.
    out.push(
      `<details class="lv-msg-tool"><summary class="lv-tool-label">Tool: ${escapeHtml(tc.name || '')}</summary>` +
        `<pre><code class="language-json">${escapeHtml(tc.arguments || '')}</code></pre></details>`,
    );
  }
  out.push('</section>');
  return out.join('');
}

/**
 * Build a complete, self-contained HTML document from an export envelope.
 * `theme` is baked into the root so the file keeps the look it was exported from.
 * @param {{ title?: string, session_id?: string, messages?: any[] }} data Export envelope.
 * @param {'light' | 'dark' | string} [theme]
 * @returns {Promise<string>} A full HTML document.
 */
export async function buildExportDoc(data, theme = 'light') {
  const heading = data.title || 'Chat';
  const messages = data.messages || [];
  // call id → tool name, so a tool result message can be labelled with its
  // tool; → arguments, for components drawn from their call's arguments.
  /** @type {Record<string, string>} */
  const toolNames = {};
  /** @type {Record<string, string>} */
  const toolArgs = {};
  for (const m of messages) {
    for (const tc of m.tool_calls || []) {
      if (!tc.id) continue;
      toolNames[tc.id] = tc.name;
      toolArgs[tc.id] = tc.arguments;
    }
  }
  const snapshots = await snapshotComponents(
    messages,
    toolArgs,
    toolNames,
    data.session_id ?? null,
  );
  const body = messages.map((m) => renderMessage(m, toolNames, snapshots)).join('\n');

  // Highlight code in a detached node, exactly like the live view (skip mermaid
  // blocks — the export has no mermaid runtime, so they stay as plain code).
  const frag = document.createElement('div');
  frag.innerHTML = body;
  if (typeof hljs !== 'undefined') {
    frag.querySelectorAll('pre code').forEach((el) => {
      if (el.classList.contains('language-mermaid')) return;
      try {
        hljs.highlightElement(el);
      } catch {
        /* unknown language — leave as plain text */
      }
    });
  }

  return (
    `<!doctype html>\n<html lang="en" data-theme="${theme}">\n<head>\n` +
    `<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n` +
    `<title>${escapeHtml(heading)}</title>\n<style>${EXPORT_CSS}</style>\n</head>\n` +
    `<body>\n<main class="lv-export">\n<h1 class="lv-export-title">${escapeHtml(heading)}</h1>\n` +
    `${frag.innerHTML}\n</main>\n</body>\n</html>`
  );
}

/**
 * Fetch a session's JSON export and download it as a self-contained HTML file.
 * @param {string} sessionId
 * @param {string} [title] Used for the filename and document heading.
 * @returns {Promise<void>}
 */
export async function exportSessionHtml(sessionId, title) {
  if (!sessionId) return;
  try {
    const data = await api.exportChat(sessionId, 'json').then(async (r) => {
      if (!r.ok) throw await apiError(r);
      return r.json();
    });
    const heading = data.title || title || 'Chat';
    const theme = document.documentElement.getAttribute('data-theme') || 'light';
    const doc = await buildExportDoc({ ...data, title: heading }, theme);

    const blob = new Blob([doc], { type: 'text/html;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = exportFilename(heading, 'html');
    a.click();
    URL.revokeObjectURL(url);
    toast(t('toast.exported'));
  } catch (err) {
    console.error('export html:', err);
    toast(t('toast.exportFailed'), { type: 'error' });
  }
}

// A lean, self-contained stylesheet for the exported document. Light + dark are
// both included (selected by the snapshot's data-theme); a system font stack
// keeps the file dependency-free. hljs colors mirror static/styles.css.
const EXPORT_CSS = `
:root, [data-theme="light"] {
  --lv-bg:#fff; --lv-surface:#fff; --lv-surface-2:#f0f2f5; --lv-border:#e4e7ec;
  --lv-ink:#16181d; --lv-ink-2:#3d424c; --lv-muted:#6b7280; --lv-muted-2:#9aa1ad;
  --lv-accent:#2d8a6e; --lv-accent-text:#1c6b54; --lv-user-surface:#eaf1f9; --lv-user-border:#d3e0ef;
}
[data-theme="dark"] {
  --lv-bg:#0d0e12; --lv-surface:#16181e; --lv-surface-2:#1d2027; --lv-border:#262a33;
  --lv-ink:#e6e8ec; --lv-ink-2:#bfc3cd; --lv-muted:#8b909b; --lv-muted-2:#656b78;
  --lv-accent:#3ecb8f; --lv-accent-text:#53d19c; --lv-user-surface:#1b2942; --lv-user-border:#2d4166;
}
* { box-sizing:border-box; }
body {
  margin:0; background:var(--lv-bg); color:var(--lv-ink); line-height:1.65;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  font-size:15px; -webkit-font-smoothing:antialiased;
}
.lv-export { max-width:760px; margin:0 auto; padding:48px 22px 72px; }
.lv-export-title {
  font-size:26px; font-weight:700; letter-spacing:-0.02em; margin:0 0 32px;
  padding-bottom:14px; border-bottom:1px solid var(--lv-border);
}
.lv-msg { margin:0 0 30px; }
.lv-msg-role {
  font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:0.07em;
  color:var(--lv-muted); margin-bottom:9px;
}
.lv-msg-body, .lv-msg-reasoning, .lv-msg-tool { font-size:15px; }
.lv-msg-user .lv-msg-body {
  background:var(--lv-user-surface); border:1px solid var(--lv-user-border);
  border-radius:12px; padding:2px 16px;
}
.lv-msg-reasoning {
  border-left:2px solid var(--lv-accent); padding:2px 0 2px 14px; margin-bottom:12px;
  color:var(--lv-muted); font-size:14px;
}
.lv-reasoning-label {
  font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:0.05em;
  color:var(--lv-accent-text); margin:4px 0;
}
.lv-msg-tool { margin-top:12px; }
.lv-tool-label { font-size:12px; font-weight:600; color:var(--lv-muted); margin-bottom:4px; }
/* Reasoning / tool blocks are <details>, collapsed by default. */
summary { cursor:pointer; user-select:none; }
summary::marker { color:var(--lv-muted-2); }
summary.lv-tool-label:hover { color:var(--lv-ink-2); }
/* summary is the first child of a <details>, so the first *content* child is :nth-child(2). */
.lv-msg-body > :first-child, .lv-msg-reasoning > :nth-child(2) { margin-top:0; }
.lv-msg-body > :last-child { margin-bottom:0; }
p { margin:0 0 14px; }
h1,h2,h3,h4 { line-height:1.3; margin:24px 0 12px; font-weight:650; }
h1 { font-size:22px; } h2 { font-size:19px; } h3 { font-size:17px; }
a { color:var(--lv-accent-text); text-decoration:underline; text-underline-offset:2px; }
ul,ol { margin:0 0 14px; padding-left:24px; }
li { margin:4px 0; }
blockquote {
  margin:0 0 14px; padding:2px 0 2px 16px; border-left:3px solid var(--lv-border);
  color:var(--lv-muted);
}
hr { border:none; border-top:1px solid var(--lv-border); margin:24px 0; }
table { border-collapse:collapse; margin:0 0 14px; font-size:14px; display:block; overflow-x:auto; }
th,td { border:1px solid var(--lv-border); padding:6px 11px; text-align:left; }
th[align=center],td[align=center] { text-align:center; }
th[align=right],td[align=right] { text-align:right; }
th { background:var(--lv-surface-2); font-weight:600; }
/* UI component snapshots (tables.js / charts.js). */
figure { margin:0; }
figcaption { font-weight:600; margin:0 0 8px; }
figcaption span { display:block; font-size:13px; font-weight:400; color:var(--lv-muted); }
figure img { display:block; max-width:100%; height:auto; }
th.lv-num, td.lv-num { text-align:right; font-variant-numeric:tabular-nums; }
code {
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:0.88em;
}
:not(pre) > code {
  background:var(--lv-surface-2); border:1px solid var(--lv-border);
  padding:1.5px 5px; border-radius:5px;
}
pre {
  background:var(--lv-surface-2); border:1px solid var(--lv-border); border-radius:10px;
  padding:14px 16px; overflow-x:auto; margin:0 0 14px; line-height:1.6;
}
pre code { background:none; border:none; padding:0; font-size:13px; }
/* highlight.js — mirrors static/styles.css */
.hljs { color:var(--lv-ink-2); background:transparent; }
.hljs-keyword,.hljs-selector-tag,.hljs-type { color:#7b3fa3; }
.hljs-string,.hljs-addition { color:#2d7a4a; }
.hljs-comment,.hljs-quote { color:var(--lv-muted-2); font-style:italic; }
.hljs-number,.hljs-literal { color:#b05e2a; }
.hljs-title,.hljs-name,.hljs-function { color:#3b5ea0; }
.hljs-attr,.hljs-attribute { color:#b8860b; }
.hljs-built_in { color:#2d8a6e; }
.hljs-meta { color:var(--lv-muted); }
[data-theme="dark"] .hljs-keyword,[data-theme="dark"] .hljs-selector-tag,[data-theme="dark"] .hljs-type { color:#b7a4f0; }
[data-theme="dark"] .hljs-string,[data-theme="dark"] .hljs-addition { color:#83d69b; }
[data-theme="dark"] .hljs-number,[data-theme="dark"] .hljs-literal { color:#e0a56a; }
[data-theme="dark"] .hljs-title,[data-theme="dark"] .hljs-name,[data-theme="dark"] .hljs-function { color:#79b0f2; }
[data-theme="dark"] .hljs-attr,[data-theme="dark"] .hljs-attribute { color:#d8b45f; }
[data-theme="dark"] .hljs-built_in { color:#53d19c; }
`;
