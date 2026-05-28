"""Multi-turn coding sessions with :func:`attach_sandbox` + a Provider.

This is the recommended production wiring:

* :class:`LocalSandboxProvider` pools sandboxes keyed by ``session_id``.
* :func:`attach_sandbox` returns an :class:`Agent` clone with sandbox
  tools, lazy lifecycle and the default audit policy.
* The :class:`Session` keeps message history so subsequent ``Runner.run``
  calls reuse the same workspace.

Run::

    OPENAI_API_KEY=sk-... python examples/23_sandbox_session.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from dotenv import load_dotenv

from lovia import (
    Agent,
    AuditStream,
    LocalSandboxProvider,
    Runner,
    attach_sandbox,
)
from lovia.stores import InMemorySession

load_dotenv()
MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-4o-mini")


async def main() -> None:
    audit_stream = AuditStream()

    with tempfile.TemporaryDirectory(prefix="lovia-sess-") as base:
        async with LocalSandboxProvider(root_base=base) as provider:
            base_agent = Agent(
                name="coder",
                instructions=(
                    "You are a focused coding agent. Use the sandbox tools "
                    "(write_file, read_file, apply_patch, run) under "
                    "/workspace. Iterate in small steps.\n\n"
                    "Dependency hygiene: the sandbox redirects HOME/TMPDIR "
                    "and prepends `.venv/bin` to PATH. If you need Python "
                    "packages, bootstrap a project-local venv FIRST:\n"
                    "    python -m venv .venv && .venv/bin/pip install <pkg>\n"
                    "From the next command onwards `python` and `pip` will "
                    "resolve to the venv automatically. A bare "
                    "`pip install …` will be flagged by the audit policy."
                ),
                model=MODEL,
            )
            agent = attach_sandbox(base_agent, provider, audit_stream=audit_stream)

            session = InMemorySession()
            session_id = "demo-session"

            print("--- turn 1: scaffold + bootstrap deps ---")
            r1 = await Runner.run(
                agent,
                "Create `app.py` with `def total(values): return sum(values)`. "
                "Then bootstrap a venv, install pytest, write a test, and "
                "run it.",
                session=session,
                session_id=session_id,
            )
            print(r1.output)

            print("\n--- turn 2: extend (sandbox + venv reused) ---")
            r2 = await Runner.run(
                agent,
                "Add `def average(values): ...` to app.py, write a test, "
                "run pytest again. The venv from turn 1 is still there.",
                session=session,
                session_id=session_id,
            )
            print(r2.output)

            # Inspect what audit saw across both runs.
            print("\n--- audit history ---")
            for rec in audit_stream.history():
                marker = {"pass": " ", "warn": "!", "block": "✗"}[rec.verdict]
                print(f"  [{marker}] {rec.command[:80]}")

            # Inspect the surviving workspace.
            sb = await provider.get(session_id)
            if sb is not None:
                print("\n--- final files (visible) ---")
                for entry in await sb.ls("."):
                    print(f"  {entry.name} ({entry.size}B)")
                print("--- bookkeeping (hidden) ---")
                for entry in await sb.ls(".", include_hidden=True):
                    if entry.name.startswith("."):
                        print(f"  {entry.name}")


if __name__ == "__main__":
    asyncio.run(main())
