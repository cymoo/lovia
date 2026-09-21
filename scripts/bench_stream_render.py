"""Benchmark the web UI's streaming markdown render (issue #110).

Two modes, both driving the real page in headless Chromium so DOM and layout
cost are included. Needs ``playwright`` with Chromium (``pip install playwright
&& playwright install chromium``) and a running ``lovia web``.

``render`` (default) — per-flush cost of rendering a reply of N KB the way
``flushRender`` does (markdown → sanitize → DOM replace → hljs), for a mixed
document (prose, lists, fences, tables) and for the worst-case shapes a block
incremental render cannot split (one huge fence / table / blank-line-free
paragraph). No messages are sent; any model config will do::

    python3 scripts/bench_stream_render.py --url http://127.0.0.1:8123

``live`` — send one message and watch the reply stream in: renders, time to
first visible text, rAF long frames. The server must be on the stub model
(see .claude/skills/verify/SKILL.md); ``--emit-script`` prints a stub script
for a mixed reply of the given size::

    python3 scripts/bench_stream_render.py --emit-script 48 > /tmp/reply.json
    python3 .claude/skills/verify/stub_model.py --stall 8 --script "$(cat /tmp/reply.json)" &
    python3 scripts/bench_stream_render.py --live --url http://127.0.0.1:8123

Output is markdown, ready to paste into the issue.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request

# ---- documents ---------------------------------------------------------------


def mixed(kb: int) -> str:
    """Prose / list / fence / table sections, ~1 KB each."""
    section = (
        "\n## Section {i}\n\nSome *prose* with **bold**, `code`, and a [link](https://x.y/{i}).\n\n"
        "- item one\n- item two\n- item three\n\n"
        "```python\ndef fn_{i}(x):\n    return x * {i}\n```\n\n"
        "| a | b |\n|---|---|\n| {i} | {i2} |\n"
    )
    out = ""
    i = 0
    while len(out) < kb * 1024:
        out += section.format(i=i, i2=i * 2)
        i += 1
    return out


def repeat_to(kb: int, line: str) -> str:
    out, i = "", 0
    while len(out) < kb * 1024:
        out += line.format(i=i)
        i += 1
    return out[: kb * 1024]


# One block that never closes while streaming: every flush re-processes all of it.
SHAPES = {
    "one fenced code block": lambda kb: (
        "```python\n"
        + repeat_to(kb, "def fn_{i}(x):\n    return x * {i}  # c\n")
        + "\n"
    ),
    "one table": lambda kb: (
        "| a | b | c |\n|---|---|---|\n" + repeat_to(kb, "| r{i} | v{i} | w{i} |\n")
    ),
    "one paragraph, no blank lines": lambda kb: repeat_to(kb, "第{i}句话，没有空行。"),
}

# ---- in-page probes ------------------------------------------------------------

# Times one full flush the way chat.js does it (renderMarkdownInto + highlightIn
# from util.js), `reps` times, with the stages timed separately once per rep.
# hljs cache is warmed first for the mixed doc (steady-state streaming re-renders
# unchanged blocks); the worst-case shapes append a char per rep so every rep is
# a cache miss, like a block that is still growing.
RENDER_PROBE = """
async ({ hash, text, reps, grow }) => {
  const util = await import(`/static/${hash}/js/util.js`);
  const body = document.createElement('div');
  body.className = 'body';
  document.querySelector('#transcript').appendChild(body);
  const t = { parse: [], purify: [], dom: [], hljs: [], flush: [] };
  util.renderMarkdownInto(body, text); util.highlightIn(body);
  for (let r = 0; r < reps; r++) {
    const src = grow ? text + ' '.repeat(r + 1) : text;
    let t0 = performance.now(); const html = marked.parse(src); t.parse.push(performance.now() - t0);
    t0 = performance.now(); const clean = DOMPurify.sanitize(html); t.purify.push(performance.now() - t0);
    t0 = performance.now();
    const tmpl = document.createElement('template'); tmpl.innerHTML = clean;
    body.replaceChildren(tmpl.content); body.offsetHeight;
    t.dom.push(performance.now() - t0);
    t0 = performance.now(); util.highlightIn(body); body.offsetHeight; t.hljs.push(performance.now() - t0);
    // The whole flush, on text the staged hljs pass above has not cached.
    const src2 = grow ? src + '.' : src;
    t0 = performance.now(); util.renderMarkdownInto(body, src2); util.highlightIn(body); body.offsetHeight;
    t.flush.push(performance.now() - t0);
  }
  body.remove();
  return { blocks: marked.lexer(text).filter((k) => k.type !== 'space').length, t };
}
"""

LIVE_PROBE = """
() => {
  const p = (window.__bench = { renders: 0, firstText: null, frames: 0, longFrames: 0, maxGap: 0, longTaskMs: 0 });
  const t0 = performance.now();
  let last = t0;
  const tick = (now) => {
    const gap = now - last; last = now; p.frames++;
    if (gap > 50) p.longFrames++;
    if (gap > p.maxGap) p.maxGap = gap;
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  new PerformanceObserver((l) => { for (const e of l.getEntries()) p.longTaskMs += e.duration; })
    .observe({ entryTypes: ['longtask'] });
  new MutationObserver((muts) => {
    for (const m of muts) {
      if (!m.target.classList?.contains('body')) continue;
      p.renders++;
      if (p.firstText === null && m.target.textContent.length) p.firstText = performance.now() - t0;
      break;
    }
  }).observe(document.getElementById('transcript'), { childList: true, subtree: true });
}
"""

# ---- runners --------------------------------------------------------------------


def pct(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def static_hash(url: str) -> str:
    html = urllib.request.urlopen(url, timeout=10).read().decode()
    m = re.search(r"/static/([0-9a-f]+)/js/main\.js", html)
    if not m:
        sys.exit(
            "could not find the static asset hash in the page — is this a lovia web server?"
        )
    return m.group(1)


def bench_render(page, hash_: str, reps: int) -> None:
    def row(label: str, text: str, grow: bool) -> str:
        r = page.evaluate(
            RENDER_PROBE, {"hash": hash_, "text": text, "reps": reps, "grow": grow}
        )
        t = r["t"]
        cells = [f"{pct(t[k], 0.5):.1f}" for k in ("parse", "purify", "dom", "hljs")]
        f = t["flush"]
        return (
            f"| {label} | {len(text) // 1024} KB | {r['blocks']} | "
            + " | ".join(cells)
            + f" | **{pct(f, 0.5):.1f}** / {pct(f, 0.95):.1f} / {max(f):.1f} |"
        )

    print(
        f"Full flush (current renderer), ms; stages are p50, flush is p50 / p95 / max over {reps} reps.\n"
    )
    print("| document | size | blocks | parse | purify | DOM+layout | hljs | flush |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for kb in (1, 4, 12, 24, 48):
        print(row("mixed (hljs cache warm)", mixed(kb), grow=False))
    for name, gen in SHAPES.items():
        for kb in (12, 48):
            print(row(f"{name} (growing, hljs miss)", gen(kb), grow=True))


def bench_live(page, window_s: float) -> None:
    page.evaluate(LIVE_PROBE)
    page.fill("#prompt", "go")
    page.keyboard.press("Enter")
    t0 = time.time()
    done = None
    while time.time() - t0 < window_s:
        if done is None and page.query_selector("#followups .followup"):
            done = time.time() - t0
        time.sleep(0.1)
    p = page.evaluate("() => window.__bench")
    n = page.evaluate(
        "() => document.querySelector('#transcript .turn.assistant .body')?.textContent.length ?? 0"
    )
    print("| metric | value |\n| --- | ---: |")
    print(f"| reply length | {n} chars |")
    print(
        f"| stream finished | {'%.1f s' % done if done else f'not within {window_s:.0f} s'} |"
    )
    print(f"| renders | {p['renders']} |")
    print(
        f"| time to first visible text | {p['firstText'] and '%.0f ms' % p['firstText']} |"
    )
    print(f"| rAF frames / long (>50 ms) | {p['frames']} / {p['longFrames']} |")
    print(f"| max frame gap | {p['maxGap']:.0f} ms |")
    print(f"| long-task time | {p['longTaskMs']:.0f} ms |")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default="http://127.0.0.1:8123")
    ap.add_argument(
        "--live",
        action="store_true",
        help="stream one reply through the UI (stub model)",
    )
    ap.add_argument(
        "--reps", type=int, default=9, help="render mode: repetitions per row"
    )
    ap.add_argument(
        "--window", type=float, default=30, help="live mode: seconds to watch"
    )
    ap.add_argument(
        "--emit-script",
        type=int,
        metavar="KB",
        help="print a stub-model script for a mixed reply",
    )
    args = ap.parse_args()

    if args.emit_script:
        print(json.dumps([{"text": mixed(args.emit_script)}]))
        return

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_selector("#prompt")
        if args.live:
            bench_live(page, args.window)
        else:
            bench_render(page, static_hash(args.url), args.reps)
        browser.close()


if __name__ == "__main__":
    main()
