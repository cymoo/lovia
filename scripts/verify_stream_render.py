"""Browser verification of the incremental streaming render (issue #110).

Drives the real UI in headless Chromium against a ``lovia web`` on the stub
model and checks the layers the parse-layer corpus can't reach:

* **equivalence** — mid-stream, the live body (committed blocks + tail) equals
  a from-scratch render of the same text, DOM for DOM;
* **node identity** — nodes of committed blocks survive later flushes;
* **selection** — a selection inside a committed block survives, and flushes
  keep landing while it is held;
* **mermaid** — a diagram rendered early is swapped in once and stays;
* **lifecycle** — stop and reload mid-stream both end in a plain full render;
* **fallback** — a reply with a raw HTML block renders in full and stays
  equivalent.

Setup (see .claude/skills/verify/SKILL.md for the server side)::

    python3 scripts/verify_stream_render.py --emit-reply main > /tmp/main.json
    python3 .claude/skills/verify/stub_model.py --stall 15 --script "$(cat /tmp/main.json)" &
    python3 scripts/verify_stream_render.py --url http://127.0.0.1:8123

    # then again with the fallback reply and --fallback
    python3 scripts/verify_stream_render.py --emit-reply fallback > /tmp/fb.json
    ... restart the stub with it ...
    python3 scripts/verify_stream_render.py --fallback --url http://127.0.0.1:8123
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request

MERMAID = "```mermaid\ngraph TD\n  A[start] --> B[end]\n```\n\n"


def section(i: int) -> str:
    return (
        f"## Section {i}\n\nProse with **bold**, `code`, and a [link](https://x.y/{i}).\n\n"
        f"- item one\n- item two\n\n```python\ndef fn_{i}(x):\n    return x * {i}\n```\n\n"
        f"| a | b |\n|---|---|\n| {i} | {i * 2} |\n\n"
    )


def reply_main() -> str:
    return "Intro paragraph.\n\n" + MERMAID + "".join(section(i) for i in range(14))


def reply_fallback() -> str:
    return (
        "".join(section(i) for i in range(3))
        + "<details><summary>raw</summary>\n\nhidden\n\n</details>\n\n"
        + "".join(section(i) for i in range(3, 8))
    )


# ---- in-page helpers -------------------------------------------------------------

# Renders body.dataset.raw from scratch the way flushRender would (streaming or
# forced) and compares it to the live body, ignoring only the tail marker.
# Runs in one task, so the live DOM and dataset.raw are the same flush.
EQUIV = """
async ({ hash, forced }) => {
  const util = await import(`/static/${hash}/js/util.js`);
  const { highlightCode } = await import(`/static/${hash}/js/chat.js`);
  const { store } = await import(`/static/${hash}/js/store.js`);
  const bodies = document.querySelectorAll('#transcript .turn.assistant .body');
  const body = bodies[bodies.length - 1];
  if (!body) return { ok: false, why: 'no body' };
  const ref = document.createElement('div');
  util.renderMarkdownInto(ref, body.dataset.raw, { agent: store.agent });
  highlightCode(ref, { skip: forced ? null : ref.lastElementChild });
  // Mermaid swaps are async and cached: strip diagrams on both sides, keep
  // their count.
  const strip = (el) => {
    const c = el.cloneNode(true);
    const figs = c.querySelectorAll('figure.mermaid-diagram, pre > code.language-mermaid');
    figs.forEach((f) => (f.closest('figure, pre')).replaceWith(document.createComment('mmd')));
    return { html: c.innerHTML.replace(/<!--tail-->/g, ''), diagrams: figs.length };
  };
  const a = strip(body), b = strip(ref);
  return { ok: a.html === b.html && a.diagrams === b.diagrams, live: a.html.length, ref: b.html.length,
           hasMarker: body.innerHTML.includes('<!--tail-->'), chars: body.textContent.length,
           diff: a.html === b.html ? null : firstDiff(a.html, b.html) };
  function firstDiff(x, y) { let i = 0; while (i < x.length && x[i] === y[i]) i++; return { at: i, live: x.slice(i - 60, i + 80), ref: y.slice(i - 60, i + 80) }; }
}
"""

BUSY = (
    "() => document.getElementById('transcript')?.getAttribute('aria-busy') === 'true'"
)
BODY = "document.querySelector('#transcript .turn.assistant:last-of-type .body')"


def static_hash(url: str) -> str:
    html = urllib.request.urlopen(url, timeout=10).read().decode()
    m = re.search(r"/static/([0-9a-f]+)/js/main\.js", html)
    if not m:
        sys.exit("not a lovia web page")
    return m.group(1)


class Check:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def __call__(self, cond: bool, label: str) -> None:
        print(("  ok   " if cond else "  FAIL ") + label)
        if not cond:
            self.failures.append(label)


def wait_settled(page, timeout: float = 90, *, started: bool = False) -> None:
    """Wait for aria-busy to go true → false. `started`: it is known to be
    true already (a cancel may settle before the first poll)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        b = page.evaluate(BUSY)
        started = started or b
        if started and not b:
            return
        time.sleep(0.1)
    raise TimeoutError("stream did not settle")


def start_stream(page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_selector("#prompt")
    page.fill("#prompt", "go")
    page.keyboard.press("Enter")
    page.wait_for_function(f"() => ({BODY})?.textContent.length > 50", timeout=15000)


def scenario_main(page, url: str, hash_: str, check: Check) -> None:
    print("stream 1: equivalence, identity, selection, mermaid")
    start_stream(page, url)
    # Wait until a heading is committed (sits before the tail marker), then
    # anchor identity to it and the selection to the first committed paragraph.
    committed = (
        "(() => { const b = %s; const out = []; if (!b) return out;"
        " for (const c of b.childNodes) { if (c.nodeType === 8 && c.data === 'tail') return out; out.push(c); }"
        " return []; })()" % BODY
    )
    page.wait_for_function(
        f"() => {committed}.some((n) => n.tagName === 'H2')", timeout=20000
    )
    page.evaluate(
        f"() => {{ const cs = {committed}; window.__h = cs.find((n) => n.tagName === 'H2'); window.__p = cs.find((n) => n.tagName === 'P'); document.getSelection().selectAllChildren(window.__p); }}"
    )
    chars0 = page.evaluate(f"() => ({BODY}).textContent.length")
    equiv_checks, equiv_ok = 0, 0
    for _ in range(25):
        r = page.evaluate(EQUIV, {"hash": hash_, "forced": False})
        if r.get("hasMarker"):
            equiv_checks += 1
            equiv_ok += bool(r["ok"])
            if not r["ok"] and equiv_checks - equiv_ok == 1:
                print("  first divergence:", json.dumps(r["diff"])[:400])
        if not page.evaluate(BUSY):
            break
        time.sleep(0.25)
    check(
        equiv_checks >= 10 and equiv_ok == equiv_checks,
        f"mid-stream equivalence on {equiv_ok}/{equiv_checks} incremental flushes",
    )
    ident = page.evaluate(
        f"() => {{ const b = {BODY}; return window.__h.isConnected && b.contains(window.__h) && b.querySelector('h2') === window.__h; }}"
    )
    check(ident, "first heading is the same node after later flushes")
    sel = page.evaluate(
        "() => { const s = document.getSelection(); return !s.isCollapsed && window.__p.contains(s.anchorNode) && window.__p.contains(s.focusNode); }"
    )
    chars1 = page.evaluate(f"() => ({BODY}).textContent.length")
    check(sel, "selection inside a committed paragraph survived")
    check(
        chars1 > chars0 + 500,
        f"flushes kept landing while the selection was held ({chars0} → {chars1} chars)",
    )
    page.evaluate("() => document.getSelection().removeAllRanges()")
    diag = page.evaluate(
        f"() => {{ const b = {BODY}; const f = b.querySelectorAll('figure.mermaid-diagram'); window.__fig = f[0] || null; return f.length; }}"
    )
    check(diag == 1, f"mermaid diagram rendered once mid-stream ({diag})")
    wait_settled(page)
    final = page.evaluate(EQUIV, {"hash": hash_, "forced": True})
    check(
        final["ok"] and not final["hasMarker"],
        "after settle: full render, no marker, equivalent",
    )
    same_fig = page.evaluate(
        f"() => {{ const f = ({BODY}).querySelectorAll('figure.mermaid-diagram'); return f.length === 1 && !!window.__fig; }}"
    )
    check(same_fig, "after settle: exactly one diagram")
    all_hl = page.evaluate(
        f"() => [...({BODY}).querySelectorAll('pre code')].every((c) => c.classList.contains('language-mermaid') || c.dataset.highlighted)"
    )
    check(all_hl, "after settle: every code block highlighted")


def scenario_stop(page, url: str, hash_: str, check: Check) -> None:
    print("stream 2: stop mid-stream")
    start_stream(page, url)
    page.wait_for_function(
        f"() => ({BODY})?.innerHTML.includes('<!--tail-->')", timeout=20000
    )
    page.click("#stop")
    wait_settled(page, started=True)
    r = page.evaluate(EQUIV, {"hash": hash_, "forced": True})
    check(
        r["ok"] and not r["hasMarker"],
        f"after stop: full render, no marker, equivalent ({r['chars']} chars)",
    )


def scenario_reload(page, url: str, hash_: str, check: Check) -> None:
    print("stream 3: reload mid-stream (reconnect)")
    start_stream(page, url)
    page.wait_for_function(
        f"() => ({BODY})?.innerHTML.includes('<!--tail-->') && ({BODY}).textContent.length > 2000",
        timeout=30000,
    )
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#prompt")
    wait_settled(page)
    r = page.evaluate(EQUIV, {"hash": hash_, "forced": True})
    check(
        r["ok"] and not r["hasMarker"],
        f"after reload + settle: full render, no marker, equivalent ({r['chars']} chars)",
    )


def scenario_fallback(page, url: str, hash_: str, check: Check) -> None:
    print("fallback stream: raw HTML block")
    start_stream(page, url)
    page.wait_for_function(f"() => ({BODY})?.querySelector('details')", timeout=30000)
    seen_marker, checks, ok = False, 0, 0
    for _ in range(20):
        r = page.evaluate(EQUIV, {"hash": hash_, "forced": False})
        checks += 1
        ok += bool(r["ok"])
        seen_marker = seen_marker or r["hasMarker"]
        if not page.evaluate(BUSY):
            break
        time.sleep(0.25)
    check(
        not seen_marker,
        "no tail marker once the HTML block appeared (full-render mode)",
    )
    check(ok == checks, f"equivalent on {ok}/{checks} full-render flushes")
    wait_settled(page)
    r = page.evaluate(EQUIV, {"hash": hash_, "forced": True})
    check(r["ok"], "after settle: equivalent")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default="http://127.0.0.1:8123")
    ap.add_argument(
        "--emit-reply",
        choices=["main", "fallback"],
        help="print the stub script for a scenario",
    )
    ap.add_argument(
        "--fallback",
        action="store_true",
        help="run the fallback scenario (stub on the fallback reply)",
    )
    args = ap.parse_args()
    if args.emit_reply:
        print(
            json.dumps(
                [
                    {
                        "text": reply_main()
                        if args.emit_reply == "main"
                        else reply_fallback()
                    }
                ]
            )
        )
        return

    from playwright.sync_api import sync_playwright

    check = Check()
    hash_ = static_hash(args.url)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        if args.fallback:
            scenario_fallback(page, args.url, hash_, check)
        else:
            scenario_main(page, args.url, hash_, check)
            scenario_stop(page, args.url, hash_, check)
            scenario_reload(page, args.url, hash_, check)
        browser.close()
    if check.failures:
        sys.exit(f"{len(check.failures)} check(s) failed")
    print("all checks passed")


if __name__ == "__main__":
    main()
