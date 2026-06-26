"""Unit tests for the run-supervisor :class:`EventHub` fan-out."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from lovia import events  # noqa: E402
from lovia.web.supervisor import EventHub, _Overflow  # noqa: E402


def _ev(n: int) -> events.Event:
    return events.TextDelta(delta=str(n))


async def test_fanout_and_monotonic_seq() -> None:
    hub = EventHub()
    a = hub.subscribe()
    b = hub.subscribe()
    assert (hub.publish(_ev(1)), hub.publish(_ev(2))) == (1, 2)

    got_a = [await a.__anext__(), await a.__anext__()]
    got_b = [await b.__anext__(), await b.__anext__()]
    # Both subscribers see the same events with the same monotonic seq.
    assert [s for s, _ in got_a] == [1, 2]
    assert [s for s, _ in got_b] == [1, 2]
    assert [e.delta for _, e in got_a] == ["1", "2"]


async def test_close_delivers_queued_tail_then_stops() -> None:
    # Regression: close() must not drop still-queued events (a fast terminal
    # publish + close would otherwise lose the final done/error).
    hub = EventHub()
    sub = hub.subscribe()
    hub.publish(_ev(1))
    hub.close()  # immediately after publish, before the subscriber consumed it

    _seq, ev = await sub.__anext__()
    assert isinstance(ev, events.TextDelta) and ev.delta == "1"
    with pytest.raises(StopAsyncIteration):
        await sub.__anext__()


async def test_subscribe_after_close_sees_only_close() -> None:
    hub = EventHub()
    hub.close()
    sub = hub.subscribe()
    with pytest.raises(StopAsyncIteration):
        await sub.__anext__()


async def test_overflow_drops_the_slow_subscriber() -> None:
    hub = EventHub(queue_maxsize=2)
    sub = hub.subscribe()
    for i in range(4):  # two fit, the third overflows → drop + sentinel
        hub.publish(_ev(i))
    with pytest.raises(_Overflow):
        await sub.__anext__()
    assert sub not in hub._subs  # dropped from the hub; the run is untouched
