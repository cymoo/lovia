"""The runner asks the endpoint for a window only when nothing else knows it.

Discovery costs a network request, so the gate matters as much as the probe:
a policy that already has a window, an adapter whose table answers, or a policy
that never needs one at all must all cost nothing.
"""

from __future__ import annotations

import pytest

from lovia import Agent, Compaction, Runner
from lovia.context import NoopContextPolicy
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

    def context_window(self, model: str) -> int | None:
        return self._window

    async def discover_context_window(self) -> int | None:
        self.probes += 1
        return self._discovered


async def _run(provider: _Probeable, **agent_kw) -> None:
    agent = Agent(name="a", instructions="i", model=provider, **agent_kw)
    await Runner().run(agent, "hello")


async def test_probes_when_nothing_local_knows_the_window() -> None:
    provider = _Probeable([text("hi")], window=None, discovered=32_768)
    await _run(provider)
    assert provider.probes == 1


async def test_skips_the_probe_when_the_adapter_table_answers() -> None:
    provider = _Probeable([text("hi")], window=200_000)
    await _run(provider)
    assert provider.probes == 0


async def test_skips_the_probe_when_the_policy_is_configured() -> None:
    provider = _Probeable([text("hi")], window=None)
    await _run(provider, context_policy=Compaction(context_window=128_000))
    assert provider.probes == 0


async def test_skips_the_probe_for_a_policy_that_needs_no_window() -> None:
    """``NoopContextPolicy`` has no ``context_window`` field at all."""
    provider = _Probeable([text("hi")], window=None)
    await _run(provider, context_policy=NoopContextPolicy())
    assert provider.probes == 0


async def test_a_provider_without_discovery_is_left_alone() -> None:
    provider = ScriptedProvider([text("hi")])
    await _run(provider)  # must not raise


async def test_a_failed_probe_does_not_fail_the_run() -> None:
    provider = _Probeable([text("hi")], window=None, discovered=None)
    await _run(provider)
    assert provider.probes == 1


@pytest.mark.parametrize("window", [None, 4096])
async def test_probe_runs_once_per_run(window: int | None) -> None:
    provider = _Probeable([text("hi"), text("again")], window=None, discovered=window)
    agent = Agent(name="a", instructions="i", model=provider)
    runner = Runner()
    await runner.run(agent, "one")
    assert provider.probes == 1
