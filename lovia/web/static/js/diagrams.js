// Mermaid diagram rendering + a fullscreen zoom/pan viewer.
//
// Diagrams are always drawn with mermaid's *light* theme onto a fixed light
// "paper" surface (see `.mermaid-diagram` / `.mermaid-sheet` in styles.css),
// even when the app is in dark mode. mermaid's dark theme needs per-diagram
// colour micromanagement to stay legible; a light surface sidesteps all of it
// and reads like an embedded figure. `mermaid` is a global from the CDN
// <script> in index.html (like marked / hljs / DOMPurify).

import { icon } from './icons.js';

const FONT =
  '"Plus Jakarta Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif';
const EXPAND_ICON = icon('maximize-2', { size: 14 });
const CLOSE_ICON = icon('x', { size: 16 });

const _cache = new Map(); // diagram source -> rendered SVG
const _inflight = new Set(); // sources currently being rendered
let _seq = 0;
let _ready = false;

function ensureMermaid() {
  if (typeof mermaid === 'undefined') return false;
  if (!_ready) {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict', // diagram text is untrusted (no scripts / click handlers)
      theme: 'default',
      fontFamily: FONT,
    });
    _ready = true;
  }
  return true;
}

// figure > <svg> + expand button. The svg is swapped in place (not via the
// figure's innerHTML) so the button and its listeners survive a re-render.
function makeFigure(src) {
  const fig = document.createElement('figure');
  fig.className = 'mermaid-diagram';
  fig.dataset.mermaidSrc = src;
  const expand = document.createElement('button');
  expand.type = 'button';
  expand.className = 'mermaid-expand';
  expand.title = 'Expand (zoom & pan)';
  expand.setAttribute('aria-label', 'Expand diagram');
  expand.innerHTML = EXPAND_ICON;
  expand.addEventListener('click', (e) => {
    e.stopPropagation();
    openLightbox(fig);
  });
  fig.addEventListener('click', () => openLightbox(fig));
  fig.appendChild(expand);
  return fig;
}

function setFigureSvg(fig, svg) {
  fig.querySelector('svg')?.remove();
  fig.insertAdjacentHTML('afterbegin', svg); // svg sits before the expand button
}

function swapDiagram(pre, svg) {
  if (!pre || !pre.isConnected) return;
  const fig = makeFigure(pre.querySelector('code')?.textContent?.trim() || '');
  setFigureSvg(fig, svg);
  pre.replaceWith(fig);
}

// Replace every ```mermaid block inside `container` with a rendered diagram.
// Safe to call repeatedly (e.g. on each streaming flush): the cache makes a
// known diagram swap in synchronously, and an incomplete block is left as-is.
export function renderMermaid(container) {
  if (!ensureMermaid()) return;
  container.querySelectorAll('pre > code.language-mermaid').forEach((code) => {
    const pre = code.parentElement;
    const src = (code.textContent || '').trim();
    if (!src) return;
    const cached = _cache.get(src);
    if (cached) {
      swapDiagram(pre, cached); // synchronous: no flicker for a known diagram
      return;
    }
    if (_inflight.has(src)) return; // this exact source is already rendering
    _inflight.add(src);
    // Validate first: while streaming, the fenced block may not be closed yet.
    mermaid
      .parse(src, { suppressErrors: true })
      .then((ok) => {
        if (!ok) return; // incomplete or invalid — retry on a later flush
        return mermaid.render(`lovia-mmd-${++_seq}`, src).then(({ svg }) => {
          _cache.set(src, svg);
          // A newer streaming flush may have replaced the original <pre>, so
          // swap whichever live block currently holds this source.
          container.querySelectorAll('pre > code.language-mermaid').forEach((c) => {
            if ((c.textContent || '').trim() === src) swapDiagram(c.parentElement, svg);
          });
        });
      })
      .catch(() => {}) // unparseable diagram: leave the raw code block visible
      .finally(() => _inflight.delete(src));
  });
}

// ---- Fullscreen viewer (wheel-zoom, drag-pan, buttons) ------------------
// Large/complex diagrams render tiny in the chat column; this is where you
// actually read them.
function naturalSize(svg) {
  const vb = svg.getAttribute('viewBox');
  if (vb) {
    const p = vb.split(/[\s,]+/).map(Number);
    if (p.length === 4 && p[2] > 0 && p[3] > 0) return { w: p[2], h: p[3] };
  }
  const r = svg.getBoundingClientRect();
  return { w: r.width || 640, h: r.height || 480 };
}

function openLightbox(fig) {
  const svgEl = fig.querySelector('svg');
  if (!svgEl) return;

  const { w: natW, h: natH } = naturalSize(svgEl);
  const clone = svgEl.cloneNode(true);
  clone.removeAttribute('style'); // drop mermaid's max-width so it can scale freely
  clone.style.width = `${natW}px`;
  clone.style.height = `${natH}px`;
  clone.style.display = 'block';

  const sheet = document.createElement('div'); // light "paper" the diagram sits on
  sheet.className = 'mermaid-sheet';
  sheet.style.transformOrigin = '0 0';
  sheet.appendChild(clone);

  const overlay = document.createElement('div');
  overlay.className = 'mermaid-lightbox';
  const stage = document.createElement('div');
  stage.className = 'mermaid-lightbox-stage';
  stage.appendChild(sheet);
  overlay.appendChild(stage);

  let scale = 1, tx = 0, ty = 0;
  const apply = () => { sheet.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`; };
  const zoomAt = (factor, cx, cy) => {
    const ns = Math.min(8, Math.max(0.1, scale * factor));
    tx = cx - (cx - tx) * (ns / scale); // keep the point under the cursor fixed
    ty = cy - (cy - ty) * (ns / scale);
    scale = ns;
    apply();
  };
  const center = () => {
    const r = stage.getBoundingClientRect();
    return [r.width / 2, r.height / 2];
  };
  const fit = () => {
    const r = stage.getBoundingClientRect();
    const sw = sheet.offsetWidth || natW;
    const sh = sheet.offsetHeight || natH;
    scale = Math.min((r.width - 48) / sw, (r.height - 48) / sh, 6) || 1;
    if (scale <= 0) scale = 1;
    tx = (r.width - sw * scale) / 2;
    ty = (r.height - sh * scale) / 2;
    apply();
  };

  const bar = document.createElement('div');
  bar.className = 'mermaid-lightbox-bar';
  const mk = (label, title, fn) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'mermaid-lightbox-btn';
    b.title = title;
    b.setAttribute('aria-label', title);
    b.innerHTML = label;
    b.addEventListener('click', (e) => { e.stopPropagation(); fn(); });
    return b;
  };
  bar.appendChild(mk(icon('minus', { size: 16 }), 'Zoom out', () => { const [x, y] = center(); zoomAt(1 / 1.25, x, y); }));
  bar.appendChild(mk('Fit', 'Reset / fit', fit));
  bar.appendChild(mk(icon('plus', { size: 16 }), 'Zoom in', () => { const [x, y] = center(); zoomAt(1.25, x, y); }));
  bar.appendChild(mk(CLOSE_ICON, 'Close', close));
  overlay.appendChild(bar);

  stage.addEventListener('wheel', (e) => {
    e.preventDefault();
    const r = stage.getBoundingClientRect();
    zoomAt(e.deltaY < 0 ? 1.12 : 1 / 1.12, e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });

  let dragging = false, ox = 0, oy = 0;
  stage.addEventListener('pointerdown', (e) => {
    dragging = true; ox = e.clientX - tx; oy = e.clientY - ty;
    stage.setPointerCapture(e.pointerId); stage.classList.add('grabbing');
  });
  stage.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    tx = e.clientX - ox; ty = e.clientY - oy; apply();
  });
  const endDrag = () => { dragging = false; stage.classList.remove('grabbing'); };
  stage.addEventListener('pointerup', endDrag);
  stage.addEventListener('pointercancel', endDrag);

  function onKey(e) {
    if (e.key === 'Escape') close();
    else if (e.key === '+' || e.key === '=') { const [x, y] = center(); zoomAt(1.25, x, y); }
    else if (e.key === '-' || e.key === '_') { const [x, y] = center(); zoomAt(1 / 1.25, x, y); }
    else if (e.key === '0') fit();
  }
  function close() {
    document.removeEventListener('keydown', onKey);
    overlay.remove();
  }
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  document.addEventListener('keydown', onKey);

  document.body.appendChild(overlay);
  requestAnimationFrame(fit); // size known only once the stage is laid out
}
