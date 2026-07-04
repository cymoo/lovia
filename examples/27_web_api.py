"""Serve the JSON + SSE API only, and build your own UI on top of it.

Run::

    pip install -e .[web]
    python examples/27_web_api.py
    # open http://127.0.0.1:8000  (a ~40-line custom page, NOT the bundled UI)

``create_app(agent, ui=False)`` gives a pure JSON + SSE server — no bundled chat
page, no ``/static`` mount. You then own the front-end. This example mounts a
tiny hand-written page at ``/`` that talks to the same endpoints the bundled UI
uses; ``lovia/web/static/js/api.js`` is the full reference client.

Useful endpoints (see ``/api/docs`` for the full schema):

    GET  /api/info            server title, agents, version, capabilities
    GET  /api/agents          list agents
    POST /api/chat            one blocking turn -> {output, session_id, usage}
    POST /api/chat/stream     SSE stream of the turn (text_delta, tool_call, …)
    GET  /api/sessions        list past chats        (DELETE clears all)
    GET  /api/sessions/{id}   full transcript        (DELETE removes one)
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi.responses import HTMLResponse

from lovia import Agent, tool
from lovia.web import create_app

load_dotenv()

MODEL = os.environ.get("LOVIA_MODEL")
if not MODEL:
    raise SystemExit(
        'Set LOVIA_MODEL first (env or .env), e.g. "openai:gpt-5.5" '
        'or "anthropic:claude-4-8-opus"'
    )


@tool
async def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


# A minimal custom front-end: read the server title from /api/info, then stream
# a turn from /api/chat/stream. Real apps would use static/js/api.js instead.
INDEX = """
<!doctype html><meta charset="utf-8"><title>custom ui</title>
<h1 id="title">…</h1>
<input id="msg" placeholder="Say something" size="40" autofocus>
<button onclick="send()">Send</button>
<pre id="out"></pre>
<script>
fetch('/api/info').then(r => r.json()).then(i => title.textContent = i.title);
let sid = null;
async function send() {
  out.textContent = '';
  const res = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: {'content-type': 'application/json', accept: 'text/event-stream'},
    body: JSON.stringify({message: msg.value, session_id: sid}),
  });
  const reader = res.body.getReader(), dec = new TextDecoder();
  let raw = '';
  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    raw += dec.decode(value, {stream: true});
    let i;
    while ((i = raw.indexOf('\\n\\n')) >= 0) {
      const chunk = raw.slice(0, i); raw = raw.slice(i + 2);
      let event = 'message', data = '';
      for (const line of chunk.split('\\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) data += line.slice(5).trim();
      }
      const d = JSON.parse(data || '{}');
      if (event === 'session') sid = d.session_id;
      else if (event === 'text_delta') out.textContent += d.delta;
    }
  }
}
</script>
"""


def main() -> None:
    import uvicorn

    agent = Agent(
        name="lovia",
        instructions="You are a friendly assistant. Keep replies short.",
        model=MODEL,
        tools=[add],
    )

    # ui=False: no bundled chat page, no /static — just the API.
    app = create_app(agent, ui=False)

    # Bring your own UI: serve a custom page from your own route.
    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return INDEX

    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
