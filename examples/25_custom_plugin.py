"""Write your own plugin: one object that bundles a capability.

A plugin is anything with a ``name`` and an async ``setup()`` returning a
:class:`PluginInstance` — tools, extra instructions, per-turn view
injectors, hooks, guardrails, and an ``aclose`` teardown, any subset. This
one packages "the assistant knows the user's profile":

* a tiny JSON file that survives runs — long-lived state is held by the
  plugin, while per-run state is built inside ``setup()``;
* an ``update_profile`` tool the model calls when the user states a
  durable preference;
* a view injector that re-shows the *current* profile at the tail of each
  turn's model view — transient, never persisted, prompt-cache friendly;
* ``aclose`` flushes changes back to disk when the run ends.

For free-form memory with search, reach for the built-in ``Memory`` plugin
(23_memory.py) instead; this pattern fits structured, app-owned state.

Run::

    python examples/25_custom_plugin.py
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from lovia import Agent, PluginInstance, RunContext, Runner, tool
from lovia.transcript import InputEntry, TranscriptEntry

load_dotenv()
MODEL = os.environ.get("LOVIA_MODEL")
if not MODEL:
    raise SystemExit(
        'Set LOVIA_MODEL first (env or .env), e.g. "openai:gpt-5.5" '
        'or "anthropic:claude-4-8-opus"'
    )


@dataclass
class ProfilePlugin:
    """Give the agent a durable, structured user profile."""

    path: Path
    name: str = "profile"

    async def setup(self) -> PluginInstance:
        # Loaded fresh per run; the tool and injector below close over it.
        profile: dict[str, str] = (
            json.loads(self.path.read_text()) if self.path.exists() else {}
        )

        @tool
        async def update_profile(key: str, value: str) -> str:
            """Save a durable fact about the user (e.g. diet=vegetarian)."""
            profile[key] = value
            return f"profile updated: {key}={value}"

        def inject(ctx: RunContext[Any]) -> list[TranscriptEntry] | None:
            if not profile:
                return None
            lines = "\n".join(f"- {k}: {v}" for k, v in sorted(profile.items()))
            return [
                InputEntry(
                    role="user",
                    content=(
                        f"<system-reminder>\nUser profile:\n{lines}\n</system-reminder>"
                    ),
                )
            ]

        async def save() -> None:
            self.path.write_text(json.dumps(profile, indent=2))

        return PluginInstance(
            tools=[update_profile],
            instructions=(
                "Record the user's durable preferences with update_profile. "
                "The current profile is re-shown to you every turn."
            ),
            view_injectors=[inject],
            aclose=save,
        )


async def main() -> None:
    Path("tmp").mkdir(exist_ok=True)
    agent = Agent(
        name="assistant",
        instructions="You are a concise personal assistant.",
        model=MODEL,
        plugins=[ProfilePlugin(Path("tmp/profile.json"))],
    )

    r1 = await Runner.run(agent, "I'm vegetarian and I prefer metric units.")
    print("A:", r1.output)

    # A brand-new run with no shared transcript: the profile file carries over.
    r2 = await Runner.run(agent, "Suggest a quick dinner. Mind my preferences.")
    print("A:", r2.output)

    print("\nprofile on disk:", json.loads(Path("tmp/profile.json").read_text()))


if __name__ == "__main__":
    asyncio.run(main())
