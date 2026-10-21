"""Browser check of the nested sandbox (static/js/sandbox.js) in real engines.

A test page with no CSP imports the shipped ``sandbox.js`` and ``html.js``
and builds an ``html`` component's frame from them, then runs one escape
attempt inside the untrusted page. A second origin logs every request that
reaches it. Per engine it checks that:

* the nested frame leaks nothing, for each escape (``location``, meta
  refresh, link click, form, parent/top navigation, ``window.open``, a
  nested frame, ``<img>``, ``fetch``);
* today's guard is what does it: the same page in a single frame (no
  proxy) does leak by navigation — otherwise the check proves nothing;
* the page's messages reach the parent through the proxy, a ``data:``
  image still loads, a click passes the focus + activation gate and a
  timer doesn't, and a deployer's cross-origin iframe on the page loads.

Needs Playwright (``pip install playwright && playwright install``); no
lovia server. Exits non-zero on any failure::

    python3 scripts/verify_sandbox_frames.py                 # chromium webkit firefox
    python3 scripts/verify_sandbox_frames.py firefox
"""

from __future__ import annotations

import json
import sys
import threading
from functools import partial
from http.server import (
    BaseHTTPRequestHandler,
    SimpleHTTPRequestHandler,
    ThreadingHTTPServer,
)
from pathlib import Path

from playwright.sync_api import sync_playwright

JS = Path(__file__).resolve().parent.parent / "lovia" / "web" / "static" / "js"
PAGE_PORT, LEAK_PORT = 8791, 8792
LEAK = f"http://127.0.0.1:{LEAK_PORT}"
PIXEL = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"

ESCAPES = {
    "location": f"location.href='{LEAK}/location'",
    "meta-refresh": "document.head.insertAdjacentHTML('beforeend',"
    f"'<meta http-equiv=refresh content=\"0;url={LEAK}/meta\">')",
    "link-click": f"const a=document.createElement('a');a.href='{LEAK}/link';"
    "document.body.append(a);HTMLElement.prototype.click.call(a)",
    "form": f"const f=document.createElement('form');f.action='{LEAK}/form';"
    "document.body.append(f);f.submit()",
    "parent-nav": f"try{{parent.location.href='{LEAK}/parent'}}catch(e){{}}",
    "top-nav": f"try{{top.location.href='{LEAK}/top'}}catch(e){{}}",
    "window-open": f"window.open('{LEAK}/open')",
    "nested-frame": "const i=document.createElement('iframe');"
    f"i.src='{LEAK}/frame';document.body.append(i)",
    "img": f"new Image().src='{LEAK}/img'",
    "fetch": f"fetch('{LEAK}/fetch').catch(()=>{{}})",
}
NAVIGATIONS = ("location", "meta-refresh", "link-click")

# The untrusted page: reports its image, offers a button that posts on
# click, posts once from a timer, and tries the escape.
UNTRUSTED = (
    f"<img id=px src='{PIXEL}'><button id=b style='width:200px;height:60px'>send</button><script>"
    "px.onload=()=>parent.postMessage({kind:'img-ok'},'*');"
    "b.onclick=()=>parent.postMessage({kind:'click'},'*');"
    "setTimeout(()=>parent.postMessage({kind:'timer'},'*'),300);"
    "setTimeout(()=>{%s},700)</script>"
)

TEST_PAGE = """<!doctype html><meta charset="utf-8"><body style="margin:0">
<div id="slot"></div>
<iframe id="dep" src="%(leak)s/deployer" style="width:120px;height:40px"></iframe>
<script type="module">
  import { buildSrcdoc } from '/js/html.js';
  import { sandboxFrame } from '/js/sandbox.js';
  const q = new URLSearchParams(location.search);
  const doc = buildSrcdoc(%(page)s.replace('%%s', %(escapes)s[q.get('escape')]), { theme: 'light', tokens: {} });
  let frame;
  if (q.get('shape') === 'nested') {
    frame = sandboxFrame(doc, { title: 'page', className: '' });
  } else {
    frame = document.createElement('iframe');
    frame.setAttribute('sandbox', 'allow-scripts');
    frame.srcdoc = doc;
  }
  frame.style.cssText = 'width:320px;height:160px;border:0';
  window.log = [];
  addEventListener('message', (e) => {
    if (e.source !== frame.contentWindow || !e.data?.kind) return;
    log.push({ kind: e.data.kind, active: !!navigator.userActivation?.isActive,
               focus: document.activeElement === frame });
  });
  document.getElementById('slot').append(frame);
</script>"""

HITS: list[str] = []


def _embed(value: object) -> str:
    """JSON for an inline <script>, where `</` would end the element."""
    return json.dumps(value).replace("</", "<\\/")


class Logger(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/deployer":
            HITS.append(self.path)
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.end_headers()
        self.wfile.write(b"<p id=ok>deployer frame</p>")

    do_POST = do_GET

    def log_message(self, *a: object) -> None:
        pass


class Pages(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/case"):
            body = TEST_PAGE % {
                "leak": LEAK,
                "page": _embed(UNTRUSTED),
                "escapes": _embed(ESCAPES),
            }
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())
        else:
            self.path = self.path.removeprefix("/js")
            super().do_GET()

    def guess_type(self, path: str) -> str:  # type: ignore[override]
        return (
            "text/javascript" if str(path).endswith(".js") else super().guess_type(path)
        )

    def log_message(self, *a: object) -> None:
        pass


def serve() -> None:
    for port, handler in (
        (PAGE_PORT, partial(Pages, directory=str(JS))),
        (LEAK_PORT, Logger),
    ):
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()


def run_engine(p, engine: str) -> list[str]:  # type: ignore[no-untyped-def]
    failures: list[str] = []
    browser = getattr(p, engine).launch()
    print(f"\n{engine} {browser.version}")
    for shape in ("single", "nested"):
        leaked = []
        for escape in ESCAPES:
            page = browser.new_page()
            HITS.clear()
            page.goto(
                f"http://127.0.0.1:{PAGE_PORT}/case?shape={shape}&escape={escape}"
            )
            page.wait_for_timeout(1300)
            if HITS:
                leaked.append(escape)
            if shape == "nested" and escape == "img":
                # The other checks, on a page whose escape doesn't navigate.
                page.mouse.click(40, 50)
                page.wait_for_timeout(400)
                log = page.evaluate("log")
                kinds = {e["kind"]: e for e in log}
                deployer = page.frame_locator("#dep").locator("#ok").count() == 1
                checks = {
                    "relay": "timer" in kinds,
                    "data: image": "img-ok" in kinds,
                    "click passes the gate": kinds.get("click", {}).get("active")
                    is True
                    and kinds.get("click", {}).get("focus") is True,
                    "timer fails the gate": not (
                        kinds.get("timer", {}).get("active")
                        and kinds.get("timer", {}).get("focus")
                    ),
                    "deployer iframe": deployer,
                }
                for name, ok in checks.items():
                    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
                    if not ok:
                        failures.append(f"{engine}: {name}")
            page.close()
        print(f"  {shape:6} leaked via: {', '.join(leaked) or 'nothing'}")
        if shape == "single" and not set(NAVIGATIONS) & set(leaked):
            failures.append(
                f"{engine}: the single frame didn't leak, so the check proves nothing"
            )
        if shape == "nested" and leaked:
            failures.append(
                f"{engine}: the nested frame leaked via {', '.join(leaked)}"
            )
    browser.close()
    return failures


def main() -> int:
    engines = sys.argv[1:] or ["chromium", "webkit", "firefox"]
    serve()
    failures: list[str] = []
    with sync_playwright() as p:
        for engine in engines:
            failures += run_engine(p, engine)
    print(
        "\n"
        + (
            "\n".join(f"FAIL {f}" for f in failures)
            if failures
            else "all checks passed"
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
