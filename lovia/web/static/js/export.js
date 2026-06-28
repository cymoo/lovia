// Client-side "export as HTML" — a self-contained, styled snapshot of a chat.
//
// Markdown export stays server-side (see `exportSession` in sessions.js); HTML
// is built here because it reuses the browser's already-loaded marked +
// DOMPurify + highlight.js, so the output matches what the user sees with no new
// server dependency. Source data is the existing JSON export endpoint, which
// carries reasoning + tool calls per message.
import { api } from './api.js';
import { toast } from './toast.js';

// Turn a session title into a safe download filename. Replaces characters that
// are illegal on common filesystems, collapses whitespace, drops trailing dots
// (Windows), and caps length.
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

function escapeHtml(s) {
  return String(s).replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );
}

function mdToHtml(text) {
  if (!text || !text.trim()) return '';
  if (typeof marked === 'undefined') return `<p>${escapeHtml(text)}</p>`;
  const raw = marked.parse(text);
  return typeof DOMPurify !== 'undefined' ? DOMPurify.sanitize(raw) : raw;
}

// Content may be a plain string or (multimodal) a part list — mirror the
// server's display_text: use the string, else a JSON dump.
function contentText(content) {
  if (typeof content === 'string') return content;
  if (content == null) return '';
  return JSON.stringify(content, null, 2);
}

function renderMessage(m) {
  const text = contentText(m.content);
  const tools = m.tool_calls || [];
  if (!text.trim() && !m.reasoning && !tools.length) return '';

  const role = (m.role || '').replace(/^\w/, (c) => c.toUpperCase());
  const out = [`<section class="msg msg-${(m.role || 'assistant').toLowerCase()}">`];
  out.push(`<div class="msg-role">${escapeHtml(role)}</div>`);
  if (m.reasoning) {
    out.push(
      `<div class="msg-reasoning"><div class="reasoning-label">💭 Thinking</div>${mdToHtml(m.reasoning)}</div>`,
    );
  }
  if (text.trim()) out.push(`<div class="msg-body">${mdToHtml(text)}</div>`);
  for (const tc of tools) {
    const fence = '```json\n' + (tc.arguments || '') + '\n```';
    out.push(
      `<div class="msg-tool"><div class="tool-label">Tool: ${escapeHtml(tc.name || '')}</div>${mdToHtml(fence)}</div>`,
    );
  }
  out.push('</section>');
  return out.join('');
}

export async function exportSessionHtml(sessionId, title) {
  if (!sessionId) return;
  try {
    const data = await fetch(api.exportUrl(sessionId, 'json')).then((r) => {
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
      return r.json();
    });
    const heading = data.title || title || 'Chat';
    const body = (data.messages || []).map(renderMessage).join('\n');

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

    const theme = document.documentElement.getAttribute('data-theme') || 'light';
    const doc =
      `<!doctype html>\n<html lang="en" data-theme="${theme}">\n<head>\n` +
      `<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n` +
      `<title>${escapeHtml(heading)}</title>\n<style>${EXPORT_CSS}</style>\n</head>\n` +
      `<body>\n<main class="export">\n<h1 class="export-title">${escapeHtml(heading)}</h1>\n` +
      `${frag.innerHTML}\n</main>\n</body>\n</html>`;

    const blob = new Blob([doc], { type: 'text/html;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = exportFilename(heading, 'html');
    a.click();
    URL.revokeObjectURL(url);
    toast('Chat exported');
  } catch (err) {
    console.error('export html:', err);
    toast('Export failed', { type: 'error' });
  }
}

// A lean, self-contained stylesheet for the exported document. Light + dark are
// both included (selected by the snapshot's data-theme); a system font stack
// keeps the file dependency-free. hljs colors mirror static/styles.css.
const EXPORT_CSS = `
:root, [data-theme="light"] {
  --bg:#faf9f7; --surface:#fff; --surface-2:#f5f4f1; --border:#e8e5e0;
  --ink:#181716; --ink-2:#3d3a36; --muted:#6f6b65; --muted-2:#a8a49e;
  --accent:#2d8a6e; --accent-text:#1d6b54; --user-surface:#eef3f8; --user-border:#d4e0ec;
}
[data-theme="dark"] {
  --bg:#0f0e0d; --surface:#1a1917; --surface-2:#232220; --border:#383632;
  --ink:#eeebe4; --ink-2:#c8c4bb; --muted:#8b8680; --muted-2:#635f5a;
  --accent:#3dba8e; --accent-text:#5ccaa0; --user-surface:#1c2630; --user-border:#334556;
}
* { box-sizing:border-box; }
body {
  margin:0; background:var(--bg); color:var(--ink); line-height:1.65;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  font-size:15px; -webkit-font-smoothing:antialiased;
}
.export { max-width:760px; margin:0 auto; padding:48px 22px 72px; }
.export-title {
  font-size:26px; font-weight:700; letter-spacing:-0.02em; margin:0 0 32px;
  padding-bottom:14px; border-bottom:1px solid var(--border);
}
.msg { margin:0 0 30px; }
.msg-role {
  font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:0.07em;
  color:var(--muted); margin-bottom:9px;
}
.msg-body, .msg-reasoning, .msg-tool { font-size:15px; }
.msg-user .msg-body {
  background:var(--user-surface); border:1px solid var(--user-border);
  border-radius:12px; padding:2px 16px;
}
.msg-reasoning {
  border-left:2px solid var(--accent); padding:2px 0 2px 14px; margin-bottom:12px;
  color:var(--muted); font-size:14px;
}
.reasoning-label {
  font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:0.05em;
  color:var(--accent-text); margin:4px 0;
}
.msg-tool { margin-top:12px; }
.tool-label { font-size:12px; font-weight:600; color:var(--muted); margin-bottom:4px; }
.msg-body > :first-child, .msg-reasoning > :nth-child(2) { margin-top:0; }
.msg-body > :last-child { margin-bottom:0; }
p { margin:0 0 14px; }
h1,h2,h3,h4 { line-height:1.3; margin:24px 0 12px; font-weight:650; }
h1 { font-size:22px; } h2 { font-size:19px; } h3 { font-size:17px; }
a { color:var(--accent-text); text-decoration:underline; text-underline-offset:2px; }
ul,ol { margin:0 0 14px; padding-left:24px; }
li { margin:4px 0; }
blockquote {
  margin:0 0 14px; padding:2px 0 2px 16px; border-left:3px solid var(--border);
  color:var(--muted);
}
hr { border:none; border-top:1px solid var(--border); margin:24px 0; }
table { border-collapse:collapse; margin:0 0 14px; font-size:14px; display:block; overflow-x:auto; }
th,td { border:1px solid var(--border); padding:6px 11px; text-align:left; }
th { background:var(--surface-2); font-weight:600; }
code {
  font-family:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:0.88em;
}
:not(pre) > code {
  background:var(--surface-2); border:1px solid var(--border);
  padding:1.5px 5px; border-radius:5px;
}
pre {
  background:var(--surface-2); border:1px solid var(--border); border-radius:10px;
  padding:14px 16px; overflow-x:auto; margin:0 0 14px; line-height:1.6;
}
pre code { background:none; border:none; padding:0; font-size:13px; }
/* highlight.js — mirrors static/styles.css */
.hljs { color:var(--ink-2); background:transparent; }
.hljs-keyword,.hljs-selector-tag,.hljs-type { color:#7b3fa3; }
.hljs-string,.hljs-addition { color:#2d7a4a; }
.hljs-comment,.hljs-quote { color:var(--muted-2); font-style:italic; }
.hljs-number,.hljs-literal { color:#b05e2a; }
.hljs-title,.hljs-name,.hljs-function { color:#3b5ea0; }
.hljs-attr,.hljs-attribute { color:#b8860b; }
.hljs-built_in { color:#2d8a6e; }
.hljs-meta { color:var(--muted); }
[data-theme="dark"] .hljs-keyword,[data-theme="dark"] .hljs-selector-tag,[data-theme="dark"] .hljs-type { color:#c8a0e0; }
[data-theme="dark"] .hljs-string,[data-theme="dark"] .hljs-addition { color:#6dcb80; }
[data-theme="dark"] .hljs-number,[data-theme="dark"] .hljs-literal { color:#e0a060; }
[data-theme="dark"] .hljs-title,[data-theme="dark"] .hljs-name,[data-theme="dark"] .hljs-function { color:#80a0d0; }
[data-theme="dark"] .hljs-attr,[data-theme="dark"] .hljs-attribute { color:#d0b040; }
[data-theme="dark"] .hljs-built_in { color:#5ccaa0; }
`;
