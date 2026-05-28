"""Full web stack: persistent sessions + sandbox + audit + auto-titles.

This is the recommended production wiring. Combines:

* :func:`lovia.web.serve` — FastAPI + bundled chat UI with sidebar,
  workspace file panel, and audit feed.
* SQLite-backed transcripts and chat metadata (``db_path``) — sessions
  survive restarts and show up in the left sidebar.
* :class:`LocalSandboxProvider` — every chat gets its own workspace,
  reused across turns and visible in the right-hand "Files" panel.
* :class:`AuditStream` — every shell command's verdict streams into the
  "Audit" panel so you can see what the model is doing in real time.
* LLM-generated chat titles — after the first turn the agent itself
  picks a concise 3-6 word title for the sidebar.

Run::

    OPENAI_API_KEY=sk-... python examples/25_web_sandbox.py
    # open http://127.0.0.1:8000
"""

from __future__ import annotations

import os
import tempfile

from dotenv import load_dotenv

from lovia import (
    Agent,
    AuditStream,
    LocalSandboxProvider,
    attach_sandbox,
)
from lovia.web import serve

load_dotenv()
MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-4o-mini")

INSTRUCTIONS = (
    "You are a focused coding agent operating inside a per-session sandbox "
    "at /workspace via the write_file / read_file / apply_patch / run tools. "
    "Iterate in small steps and show your work.\n\n"
    "Dependency hygiene: the sandbox redirects HOME / TMPDIR and prepends "
    "`.venv/bin` to PATH. Before installing Python packages, bootstrap a "
    "project-local venv:\n"
    "    python -m venv .venv && .venv/bin/pip install <pkg>\n"
    "From the next command onward `python` and `pip` will resolve to the "
    "venv automatically. A bare `pip install …` will be flagged by the "
    "audit policy."
)


def main() -> None:
    audit_stream = AuditStream()

    with tempfile.TemporaryDirectory(prefix="lovia-web-") as base:
        provider = LocalSandboxProvider(root_base=base)

        base_agent = Agent(name="coder", instructions=INSTRUCTIONS, model=MODEL)
        agent = attach_sandbox(base_agent, provider, audit_stream=audit_stream)

        # Persist transcripts + chat metadata to ./lovia.db so the sidebar
        # survives restarts. Drop ``db_path`` for in-memory only.
        serve(
            agent,
            db_path="lovia.db",
            sandbox_provider=provider,
            audit_stream=audit_stream,
            host="127.0.0.1",
            port=8000,
        )


if __name__ == "__main__":
    main()
