// @ts-check
// Acme's extension for the lovia chat page. Loaded by
// examples/33_web_extensions.py through ChatUI(scripts=["extension.js"]), before
// the page starts; it touches the page only through window.lovia. The strings
// come from ChatUI(messages=...) via lovia.t.

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
lovia.registerView('docs', {
  title: lovia.t('acme.docs'),
  async render(el, ctx) {
    const res = await lovia.fetch('/acme/docs');
    /** @type {{ slug: string, title: string, snippet: string }[]} */
    const docs = await res.json();
    const page = docs.find((d) => d.slug === ctx.path);
    const box = document.createElement('div');
    box.className = 'acme-docs';
    if (page) {
      const h = document.createElement('h2');
      h.textContent = page.title;
      const p = document.createElement('p');
      p.textContent = page.snippet;
      const ask = document.createElement('button');
      ask.type = 'button';
      ask.className = 'acme-chip';
      ask.textContent = lovia.t('acme.askAbout');
      ask.addEventListener('click', () => {
        lovia.newChat(); // shows the chat, closing the view
        lovia.send(`${page.title}: ${page.snippet} Tell me more.`);
      });
      const back = document.createElement('a');
      back.href = '?view=docs';
      back.textContent = lovia.t('acme.allDocs');
      box.append(back, h, p, ask);
    } else {
      const list = document.createElement('ul');
      for (const d of docs) {
        const a = document.createElement('a');
        a.href = `?view=docs/${encodeURIComponent(d.slug)}`;
        a.textContent = d.title;
        const li = document.createElement('li');
        li.append(a);
        list.append(li);
      }
      box.append(list);
    }
    el.replaceChildren(box);
  },
});
