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

    def context_window(self, model: str) -> int | None:
        return self._window


class FakeWorkspace:
    """In-memory WorkspaceSession stub: write_text/read_text only."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    async def write_text(self, path: str, content: str, *, create_only: bool = False):
        self.files[path] = content

    async def read_text(self, path: str, *, start=None, end=None):
        return self.files[path]


class FailingWorkspace:
    """A workspace whose writes always fail."""

    async def write_text(self, path: str, content: str, *, create_only: bool = False):
        raise OSError("disk full")
