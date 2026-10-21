// @ts-check
// Acme's extension for the lovia chat page. Loaded by
// examples/33_web_extensions.py through ChatUI(scripts=["extension.js"]), before
// the page starts; it touches the page only through window.lovia. The strings
// come from ChatUI(messages=...) via lovia.t.

import { html, render as mount } from 'htm/preact';
import { useState } from 'preact/hooks';

const { lovia } = window;

// ---- Tool views ----------------------------------------------------------

/**
 * The string for `key`, or null when there is none (`t` hands back the key).
 * @param {string} key
 * @param {Record<string, string | number>} [params]
 */
function label(key, params) {
  const text = lovia.t(key, params);
  return text === key ? null : text;
}

// Every tool: a plain-language line instead of `name(args)` — "Opening a
// ticket", then "Opened a ticket" once it settled. A tool without strings of
// its own keeps the default line.
lovia.registerToolView('*', {
  summary(tool) {
    const settled = tool.status === 'done' ? label(`acme.tool.${tool.name}.done`) : null;
    return settled ?? label(`acme.tool.${tool.name}`);
  },
});

/**
 * The hits a search_docs result carries; [] for anything else.
 * @param {string | undefined} result
 * @returns {{ title: string, url: string, snippet: string }[]}
 */
function hits(result) {
  try {
    const parsed = JSON.parse(result ?? '');
    return Array.isArray(parsed?.results) ? parsed.results : [];
  } catch {
    return [];
  }
}

lovia.registerToolView('search_docs', {
  summary(tool) {
    const query = String(/** @type {any} */ (tool.args)?.query ?? '');
    return tool.status === 'done'
      ? label('acme.tool.search_docs.done', { query })
      : label('acme.tool.search_docs');
  },
  // The hits as links. Tool output is untrusted: textContent, never innerHTML.
  render(el, tool) {
    if (tool.status !== 'done') return;
    const found = hits(tool.result);
    if (!found.length) {
      el.textContent = lovia.t('acme.noHits');
      return;
    }
    const list = document.createElement('ol');
    list.className = 'acme-hits';
    for (const hit of found) {
      const link = document.createElement('a');
      link.href = hit.url;
      link.target = '_blank';
      link.rel = 'noopener';
      link.textContent = hit.title;
      const snippet = document.createElement('p');
      snippet.textContent = hit.snippet;
      const item = document.createElement('li');
      item.append(link, snippet);
      list.append(item);
    }
    el.replaceChildren(list);
  },
  // The same list in the chat's HTML export (a standalone file: no script).
  snapshot(tool) {
    const escape = (/** @type {string} */ s) =>
      s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
    const items = hits(tool.result).map(
      (hit) => `<li><a href="${escape(hit.url)}">${escape(hit.title)}</a> — ${escape(hit.snippet)}</li>`,
    );
    return items.length ? `<ol>${items.join('')}</ol>` : '';
  },
});

// ---- Events --------------------------------------------------------------

const working = document.getElementById('acme-working');
const tokens = document.getElementById('acme-tokens');
/** Tokens spent per chat, on this page. @type {Map<string, number>} */
const spent = new Map();

function showTokens() {
  const n = spent.get(lovia.state.sessionId ?? '') ?? 0;
  if (!tokens) return;
  tokens.hidden = n === 0;
  tokens.textContent = lovia.t('acme.tokens', { n: n.toLocaleString() });
}

lovia.on('run-start', () => {
  if (working) working.hidden = false;
});

lovia.on('run-end', ({ sessionId, status, usage }) => {
  if (working) working.hidden = true;
  if (status === 'done' && sessionId && usage?.total_tokens) {
    spent.set(sessionId, (spent.get(sessionId) ?? 0) + usage.total_tokens);
  }
  showTokens();
});

lovia.on('session', showTokens);

// ---- Actions -------------------------------------------------------------

document.getElementById('acme-summarize')?.addEventListener('click', () => {
  lovia.send(lovia.t('acme.summarizePrompt'));
});

document.getElementById('acme-new-ticket')?.addEventListener('click', () => {
  lovia.newChat();
  lovia.setDraft(lovia.t('acme.ticketTemplate'));
});

// ---- A view --------------------------------------------------------------

// The sidebar's "Docs index" link (`?view=docs`) opens this in place of the
// chat. Each page links to `?view=docs/<slug>`, which arrives as ctx.path.
// Drawn with Preact + htm, no build step: index.html's import map resolves
// the bare imports above to the copies in vendor/.

/** @typedef {{ slug: string, title: string, snippet: string }} Doc */

/** @param {{ docs: Doc[], path: string }} props */
function Docs({ docs, path }) {
  const [query, setQuery] = useState('');
  const page = docs.find((d) => d.slug === path);
  if (page) {
    const ask = () => {
      lovia.newChat(); // shows the chat, closing the view
      lovia.send(`${page.title}: ${page.snippet} Tell me more.`);
    };
    return html`
      <div class="acme-docs">
        <a href="?view=docs">${lovia.t('acme.allDocs')}</a>
        <h2>${page.title}</h2>
        <p>${page.snippet}</p>
        <button type="button" class="acme-chip" onClick=${ask}>${lovia.t('acme.askAbout')}</button>
      </div>
    `;
  }
  const q = query.trim().toLowerCase();
  const shown = docs.filter((d) => `${d.title} ${d.snippet}`.toLowerCase().includes(q));
  return html`
    <div class="acme-docs">
      <input
        type="search"
        class="acme-filter"
        placeholder=${lovia.t('acme.filter')}
        value=${query}
        onInput=${(e) => setQuery(e.currentTarget.value)}
      />
      <ul>
        ${shown.map(
          (d) => html`<li key=${d.slug}><a href=${`?view=docs/${encodeURIComponent(d.slug)}`}>${d.title}</a></li>`,
        )}
      </ul>
      ${shown.length === 0 && html`<p>${lovia.t('acme.noHits')}</p>`}
    </div>
  `;
}

lovia.registerView('docs', {
  title: lovia.t('acme.docs'),
  // Fetching here, not in the component, keeps lovia's handling of a render
  // that fails: the view shows a notice.
  async render(el, ctx) {
    const res = await lovia.fetch('/acme/docs');
    /** @type {Doc[]} */
    const docs = await res.json();
    mount(html`<${Docs} docs=${docs} path=${ctx.path} />`, el);
    return () => mount(null, el);
  },
});
