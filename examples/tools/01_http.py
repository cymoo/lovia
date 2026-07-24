"""Fetching the web — two tools, because they are two different jobs.

``read_page`` reads a page for its content: HTML becomes Markdown, so headings,
links and images survive. ``http_request`` is a plain HTTP client for REST
endpoints: it returns status, headers and the body untouched.

Run::

    python examples/tools/01_http.py
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

from lovia import Agent, Runner, events, model_from_env
from lovia.tools import http_request, read_page

load_dotenv()
MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset


async def main() -> None:
    agent = Agent(
        name="Fetcher",
        instructions=(
            "Use read_page for web pages and http_request for JSON APIs. "
            "Summarise what you find."
        ),
        model=MODEL,
        tools=[read_page, http_request],
    )
    handle = Runner.stream(
        agent,
        "Read https://example.com and list every image it references, then "
        "fetch https://httpbin.org/json and tell me what's inside.",
    )
    async for ev in handle:
        if isinstance(ev, events.TextDelta):
            print(ev.delta, end="", flush=True)
        elif isinstance(ev, events.ToolCallStarted):
            print(f"\n[tool] {ev.call.name}", flush=True)
    result = await handle.result()
    print(f"\n\n[done, turns={result.turns}]")


if __name__ == "__main__":
    asyncio.run(main())
