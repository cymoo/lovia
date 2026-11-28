"""Keep awake: the hold follows ``wanted``, and Settings saves the switch."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lovia.web import awake
from lovia.web.default import default_app


class _Recorder:
    """Stands in for the operating system's hold."""

    def __init__(self) -> None:
        self.calls: list[bool] = []

    def set(self, on: bool) -> None:
        self.calls.append(on)


async def test_the_hold_follows_wanted_and_ends_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hold = _Recorder()
    monkeypatch.setattr(awake, "supported", lambda: True)
    monkeypatch.setattr(awake, "_Hold", lambda: hold)
    answers = iter([False, True, True, False])
    task = asyncio.create_task(awake.keep_awake(lambda: next(answers), interval=0))
    while len(hold.calls) < 4:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Whatever it held when the server stopped, it lets go.
    assert hold.calls[:4] == [False, True, True, False]
    assert hold.calls[-1] is False


async def test_it_does_nothing_where_sleep_cannot_be_held_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(awake, "supported", lambda: False)
    monkeypatch.setattr(awake, "_Hold", lambda: pytest.fail("no hold to take"))
    await awake.keep_awake(lambda: True)


async def test_a_failing_hold_never_stops_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Broken:
        def set(self, on: bool) -> None:
            raise OSError("no caffeinate")

    monkeypatch.setattr(awake, "supported", lambda: True)
    monkeypatch.setattr(awake, "_Hold", Broken)
    await awake.keep_awake(lambda: True, interval=0)  # returns, raises nothing


@pytest.mark.skipif(sys.platform != "darwin", reason="caffeinate is macOS's")
def test_macos_holds_with_a_caffeinate_child_and_takes_a_lost_one_again() -> None:
    hold = awake._Hold()
    try:
        hold.set(True)
        first = hold._child
        assert first is not None and first.poll() is None
        hold.set(True)
        assert hold._child is first  # held already: no second child
        first.kill()
        first.wait()
        hold.set(True)
        assert hold._child is not first and hold._child.poll() is None
    finally:
        hold.set(False)
    assert hold._child is None


def test_settings_saves_the_switch_and_says_whether_it_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(awake, "supported", lambda: False)
    app = default_app(home=tmp_path / "home", workspace=tmp_path)
    with TestClient(app, base_url="http://localhost") as c:
        assert c.get("/api/config").json()["keep_awake"] == {
            "on": False,
            "supported": False,
        }
        res = c.put("/api/config/keep-awake", json={"on": True})
        assert res.status_code == 200, res.text
        assert res.json()["config"]["keep_awake"]["on"] is True
    assert '"keep_awake": true' in (tmp_path / "home" / "config.json").read_text()
