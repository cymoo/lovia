"""Custom provider: implement the ``Provider`` protocol yourself.

A provider is just an object with ``name`` / ``model`` / ``supports_json_schema``
and an async ``stream`` that yields typed deltas. No subclassing, no network —
this example runs fully offline with a toy "echo" model so you can see exactly
what an adapter has to produce.

Run::

    python examples/10_custom_provider.py
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from lovia import Agent, Runner
from lovia.providers.base import ModelSettings
from lovia.transcript import (
    FinishDelta,
    ModelDelta,
    TextDelta,
    TranscriptEntry,
    UsageDelta,
    entries_to_messages,
)
from lovia.messages import Usage


class EchoProvider:
    """A trivial provider that streams back an upper-cased echo of the input.

    Swap the body of ``stream`` for real HTTP calls to wire up any backend the
    built-ins don't cover (a local model, an internal gateway, a mock for
    tests). The runner only relies on this four-member surface.
    """

    supports_json_schema = False

    def __init__(self, model: str = "echo-1") -> None:
        self._model = model

    @property
    def name(self) -> str:
        return "echo"

    @property
    def model(self) -> str | None:
        return self._model

    async def stream(
        self,
        entries: list[TranscriptEntry],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        settings: ModelSettings | None = None,
    ) -> AsyncIterator[ModelDelta]:
        # Find the last user message and echo it, streamed token-by-token so a
        # consumer sees real deltas.
        messages = entries_to_messages(entries)
        last_user = next((m.text for m in reversed(messages) if m.role == "user"), "")
        reply = f"echo: {last_user.upper()}"
        for word in reply.split(" "):
            yield TextDelta(text=word + " ")
        yield UsageDelta(usage=Usage(input_tokens=len(messages), output_tokens=1))
        yield FinishDelta(reason="stop")


async def main() -> None:
    # Pass the provider instance straight to ``model=`` — no "vendor:name"
    # string needed when you already hold an adapter.
    agent = Agent(name="parrot", instructions="(ignored by echo)", model=EchoProvider())

    result = await Runner.run(agent, "hello custom providers")
    print(result.output)  # -> echo: HELLO CUSTOM PROVIDERS


if __name__ == "__main__":
    asyncio.run(main())
