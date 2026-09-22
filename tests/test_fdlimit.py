"""Tests for the process-wide open-file limit helpers."""

from __future__ import annotations

import errno
import logging
import os
from typing import Iterator

import pytest

from lovia._fdlimit import out_of_file_descriptors, raise_soft_limit

# Both helpers are documented no-ops without RLIMIT_NOFILE (Windows); there is
# nothing to assert there beyond "did not raise", which every caller covers.
resource = pytest.importorskip("resource")


@pytest.fixture
def restore_limit() -> Iterator[tuple[int, int]]:
    """Hand back this process's (soft, hard) limit and put it back afterwards."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        yield soft, hard
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_raise_soft_limit_lifts_a_low_limit(restore_limit: tuple[int, int]) -> None:
    _, hard = restore_limit
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
    raise_soft_limit(4096)
    assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] == 4096


def test_raise_soft_limit_leaves_a_generous_limit_alone(
    restore_limit: tuple[int, int],
) -> None:
    _, hard = restore_limit
    resource.setrlimit(resource.RLIMIT_NOFILE, (4096, hard))
    raise_soft_limit(1024)
    assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] == 4096


def test_raise_soft_limit_warns_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Patched rather than lowered for real: a refusal is what a hardened host
    # does, and the test must not depend on being able to reproduce one.
    monkeypatch.setattr(
        resource, "getrlimit", lambda _res: (256, resource.RLIM_INFINITY)
    )

    def _refuse(*_args: object) -> None:
        raise OSError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr(resource, "setrlimit", _refuse)
    with caplog.at_level(logging.WARNING, logger="lovia._fdlimit"):
        raise_soft_limit(4096)
    assert "open-file limit is 256, short of 4096" in caplog.text
    assert "operation not permitted" in caplog.text
    assert "ulimit -n 4096" in caplog.text


def test_raise_soft_limit_warns_when_the_hard_cap_pins_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The quiet case: nothing refuses anything, setrlimit would succeed as a
    # no-op, and the process is still stuck at a limit it will run out of.
    monkeypatch.setattr(resource, "getrlimit", lambda _res: (256, 256))
    asked: list[tuple[int, int]] = []
    monkeypatch.setattr(resource, "setrlimit", lambda _res, pair: asked.append(pair))
    with caplog.at_level(logging.WARNING, logger="lovia._fdlimit"):
        raise_soft_limit(4096)
    assert asked == []  # there was nothing to ask for
    assert "open-file limit is 256, short of 4096" in caplog.text
    assert "hard cap (256)" in caplog.text


def test_out_of_file_descriptors_is_none_with_headroom() -> None:
    assert out_of_file_descriptors() is None


def test_out_of_file_descriptors_reports_the_soft_limit(
    restore_limit: tuple[int, int],
) -> None:
    soft, hard = restore_limit
    hogs: list[int] = []
    try:
        # Lowering below the count already open is legal: the open ones stay,
        # new ones fail — which is exactly the state being probed.
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, hard))
        while True:
            hogs.append(os.open(os.devnull, os.O_RDONLY))
    except OSError:
        pass
    try:
        assert out_of_file_descriptors() == 64
    finally:
        # Headroom back before anything else in the process needs a descriptor
        # (pytest's own reporting included), then release the hogs.
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
        for fd in hogs:
            os.close(fd)


def test_out_of_file_descriptors_ignores_unrelated_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _denied(*_args: object, **_kwargs: object) -> int:
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(os, "open", _denied)
    assert out_of_file_descriptors() is None
