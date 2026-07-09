"""Shared helpers for the context test suite."""

from __future__ import annotations

from lovia.context import CompactionRequest
from lovia.transcript import InputEntry, ToolCallEntry, ToolResultEntry


def user(s: str) -> InputEntry:
    return InputEntry(role="user", content=s)


def system(s: str) -> InputEntry:
    return InputEntry(role="system", content=s)


def call(call_id: str, name: str = "f") -> ToolCallEntry:
    return ToolCallEntry(call_id=call_id, name=name, arguments="{}")


def out(call_id: str, content: str = "ok") -> ToolResultEntry:
    return ToolResultEntry(call_id=call_id, output=content)


def req(entries, **kw) -> CompactionRequest:
    return CompactionRequest(entries=entries, **kw)


class FakeSummarizer:
    """Records every summarize() call and returns a fixed text."""

    def __init__(self, text: str = "SUMMARY_TEXT") -> None:
        self.text = text
        self.calls: list[list] = []
        self.priors: list[str | None] = []

    async def summarize(self, entries, *, req, prior_summary=None):
        self.calls.append(list(entries))
        self.priors.append(prior_summary)
        return self.text


class FailingSummarizer:
    def __init__(self) -> None:
        self.calls = 0

    async def summarize(self, entries, *, req, prior_summary=None):
        self.calls += 1
        raise RuntimeError("boom")


class FakeProviderWithWindow:
    """A stand-in provider that just answers context_window queries."""

    name = "fake"

    def __init__(self, *, window: int | None = 1000) -> None:
        self.model = "fake-model"
        self._window = window

    def context_window(self) -> int | None:
        return self._window


class FakeResultStore:
    """In-memory ResultStore stub: put/get."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def put(self, key: str, content: str) -> None:
        self.data[key] = content

    async def get(self, key: str) -> str | None:
        return self.data.get(key)


class FailingResultStore:
    """A result store whose puts always fail."""

    async def put(self, key: str, content: str) -> None:
        raise OSError("disk full")

    async def get(self, key: str) -> str | None:
        return None
