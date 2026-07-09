"""When the runner gives an endpoint the chance to report its own window.

The runner asks whenever the *policy* has no window of its own; the adapter then
decides whether that costs a request. Crucially the runner must not skip the
question because a bundled table has an answer — the endpoint outranks the table,
and a deployment that caps a familiar model would otherwise be budgeted at the
table's number.
"""

from __future__ import annotations

import httpx
import pytest

from lovia import Agent, Compaction, Runner
from lovia.context import NoopContextPolicy
from lovia.providers.openai_chat import OpenAIChatProvider
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


async def test_asks_even_when_a_table_already_has_an_answer() -> None:
    """The endpoint outranks the table, so it must still get a say."""
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


async def test_the_table_says_nothing_about_a_foreign_host() -> None:
    """``gpt-4.1`` on api.openai.com is 1M. On a vLLM box it is whatever that
    box was started with, and the bundled table must not pretend otherwise."""
    provider = OpenAIChatProvider(model="gpt-4.1", base_url="http://vllm:8000/v1")
    assert provider.context_window() is None

    official = OpenAIChatProvider(
        model="gpt-4.1", api_key="x", base_url="https://api.openai.com/v1"
    )
    assert official.context_window() == 1_047_576


async def test_a_self_reported_window_overrides_the_bundled_table() -> None:
    """The endpoint outranks the table, so the runner must still ask it.

    Here the table does know ``deepseek-v4-pro`` on this host, but a deployment
    that publishes a smaller served window has the final say.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200, json={"data": [{"id": "deepseek-v4-pro", "max_model_len": 65_536}]}
            )
        return httpx.Response(200, content=_SSE)

    provider = OpenAIChatProvider(
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.com",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert provider.context_window() == 1_048_565  # the table, on its own

    await _run(provider)

    assert provider.context_window() == 65_536
