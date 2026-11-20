"""When the runner gives an endpoint the chance to report its own window.

The runner asks whenever the *policy* has no window of its own; the adapter then
decides whether that costs a request.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from lovia import Agent, Compaction, Runner
from lovia.context import NoopContextPolicy
from lovia.providers.openai_chat import OpenAIChatProvider
from lovia.runtime import loop as loop_mod
from lovia.testing import ScriptedProvider, text


class _Probeable(ScriptedProvider):
    """A scripted provider that records whether the runner probed it."""

    def __init__(
        self, *args, window: int | None = None, discovered: int | None = None, **kw
    ):
        super().__init__(*args, **kw)
        self.model = "scripted-model"
        self._window = window
        self._discovered = discovered
        self.probes = 0

    def context_window(self) -> int | None:
        return self._discovered if self._discovered is not None else self._window

    async def discover_context_window(self) -> int | None:
        self.probes += 1
        return self.context_window()


async def _run(provider, **agent_kw) -> None:
    agent = Agent(name="a", instructions="i", model=provider, **agent_kw)
    await Runner().run(agent, "hello")


async def test_asks_the_endpoint_when_the_policy_has_no_window() -> None:
    provider = _Probeable([text("hi")], window=None, discovered=32_768)
    await _run(provider)
    assert provider.probes == 1


async def test_asks_even_when_the_provider_already_names_a_window() -> None:
    """Whether a probe is worth a request is the adapter's call, not ours."""
    provider = _Probeable([text("hi")], window=200_000)
    await _run(provider)
    assert provider.probes == 1


async def test_does_not_ask_when_the_policy_is_configured() -> None:
    provider = _Probeable([text("hi")], window=None)
    await _run(provider, context_policy=Compaction(context_window=128_000))
    assert provider.probes == 0


async def test_does_not_ask_for_a_policy_that_needs_no_window() -> None:
    """``NoopContextPolicy`` has no ``context_window`` field at all."""
    provider = _Probeable([text("hi")], window=None)
    await _run(provider, context_policy=NoopContextPolicy())
    assert provider.probes == 0


async def test_unknown_window_is_logged_once_per_model(caplog) -> None:
    """The 'context.window: unknown' hint fires once per process, not per run."""
    provider = _Probeable([text("hi"), text("hi")], window=None)
    provider.model = "windowless-model"
    loop_mod._logged_once.clear()
    with caplog.at_level(logging.INFO, logger="lovia.runtime.loop"):
        await _run(provider)
        await _run(provider)
    hints = [r for r in caplog.records if "context.window: unknown" in r.getMessage()]
    assert len(hints) == 1
    assert "windowless-model" in hints[0].getMessage()


async def test_a_provider_without_discovery_is_left_alone() -> None:
    await _run(ScriptedProvider([text("hi")]))  # must not raise


async def test_a_failed_probe_does_not_fail_the_run() -> None:
    provider = _Probeable([text("hi")], window=None, discovered=None)
    await _run(provider)
    assert provider.probes == 1


@pytest.mark.parametrize("window", [None, 4096])
async def test_the_probe_runs_once_per_run(window: int | None) -> None:
    provider = _Probeable([text("hi"), text("again")], window=None, discovered=window)
    await _run(provider)
    assert provider.probes == 1


_SSE = (
    b'event: chat.completion.chunk\ndata: {"choices":[{"delta":{"content":"hi"},'
    b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
)


async def test_a_name_alone_yields_no_window() -> None:
    """``gpt-4.1`` on a vLLM box is whatever that box was started with."""
    provider = OpenAIChatProvider(model="gpt-4.1", base_url="http://vllm:8000/v1")
    assert provider.context_window() is None


async def test_the_run_learns_the_window_deepseek_publishes() -> None:
    """DeepSeek's ``GET /models`` carries ``context_window`` per model."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "deepseek-v4-pro",
                            "object": "model",
                            "owned_by": "deepseek",
                            "context_window": 1_048_576,
                        }
                    ],
                },
            )
        return httpx.Response(200, content=_SSE)

    provider = OpenAIChatProvider(
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.com",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert provider.context_window() is None

    await _run(provider)

    assert provider.context_window() == 1_048_576
