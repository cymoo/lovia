"""Request-level prompt-prefix stability, end to end.

The per-layer tests (compaction replay in ``tests/context/test_pipeline.py``,
the Anthropic breakpoint in ``tests/providers/test_anthropic.py``, Notes
pinning in ``tests/plugins/test_memory.py``) each hold one piece. This drives a
real agent — Memory, Todo, Skills, a local workspace, and an exposed plus a
deferred MCP server — through several tool-calling turns across two runs of one
session, records every request the loop sends, and checks the whole thing:
lovia's request (system + tools + history), then the final provider JSON.

The default ``Compaction`` policy stays mounted: the transcript sits far below
its watermark, so the view must equal the transcript (checked below via
"every request's stable part is a prefix of the final transcript").
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

# The deferred/exposed MCP servers are the real stdio stub; CI installs the extra.
pytest.importorskip("mcp")

from lovia import Agent, Runner
from lovia.plugins import Memory, Skills, Todo
from lovia.plugins.mcp import MCP, MCPServerStdio
from lovia.providers.anthropic import AnthropicProvider
from lovia.providers.base import ModelSettings
from lovia.providers.openai_chat import OpenAIChatProvider
from lovia.stores import SQLiteSession
from lovia.testing import ScriptedProvider, batch, call, text
from lovia.transcript import InputEntry, ModelDelta, TranscriptEntry, entry_to_dict
from lovia.workspace import CommandRule, Workspace

_STUB = Path(__file__).parent / "plugins" / "mcp_stub_server.py"
_SESSION = "s1"


@dataclass
class _Request:
    entries: list[TranscriptEntry]
    tools: list[dict[str, Any]] | None
    settings: ModelSettings | None


class _RecordingProvider(ScriptedProvider):
    """``ScriptedProvider`` that keeps the raw request, not the lossy Messages.

    Deep copies, so a later in-place mutation of a sent entry shows up as a
    difference instead of silently rewriting the record.
    """

    def __init__(self, script: list[Any]) -> None:
        super().__init__(script)
        self.requests: list[_Request] = []

    async def stream(
        self,
        entries: list[TranscriptEntry],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        settings: ModelSettings | None = None,
    ) -> AsyncIterator[ModelDelta]:
        self.requests.append(
            _Request(copy.deepcopy(entries), copy.deepcopy(tools), settings)
        )
        async for delta in super().stream(
            entries, tools=tools, response_format=response_format, settings=settings
        ):
            yield delta


def _todos(*statuses: str) -> dict[str, Any]:
    labels = ["Write the notes file", "Check the math"]
    return {
        "todos": [
            {"content": label, "status": status}
            for label, status in zip(labels, statuses)
        ]
    }


_RUN_1 = [
    call("todo_write", _todos("in_progress", "pending"), call_id="t1"),
    call("write_file", {"path": "notes.txt", "content": "hello"}, call_id="w1"),
    call("shell", {"command": "sleep 30", "background": True}, call_id="sh1"),
    batch(
        ("read_file", {"path": "notes.txt"}, "r1"),
        ("remember", {"fact": "user likes green tea"}, "m1"),
    ),
    call("load_skill", {"name": "greet"}, call_id="sk1"),
    call(
        "call_mcp_tool",
        {"tool": "stub__add", "arguments": {"a": 2, "b": 3}},
        call_id="mcp1",
    ),
    call("calc__add", {"a": 4, "b": 5}, call_id="mcp2"),
    text("Notes written, math checked."),
]
_RUN_2 = [
    call("todo_write", _todos("completed", "in_progress"), call_id="t2"),
    call("search_mcp_tools", {"query": "add"}, call_id="mcp3"),
    call("read_file", {"path": "notes.txt"}, call_id="r2"),
    text("All done."),
]


@dataclass
class _Recording:
    requests: list[_Request]
    transcript: list[TranscriptEntry]  # system + the session's full history
    notes: str


async def _drive(root: Path) -> _Recording:
    skills = root / "skills" / "greet"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "---\nname: greet\ndescription: Greet the user warmly.\n---\n# Greet\nSay hi."
    )
    (root / "ws").mkdir()

    def stub(name: str, *, defer: bool) -> MCPServerStdio:
        return MCPServerStdio(
            command=sys.executable, args=[str(_STUB)], name=name, defer=defer
        )

    provider = _RecordingProvider([*_RUN_1, *_RUN_2])
    agent = Agent(
        name="builder",
        instructions="You build things.",
        model=provider,
        settings=ModelSettings(provider_options={"anthropic": {"cache_system": True}}),
        workspace=Workspace.local(
            str(root / "ws"), command_rules=(CommandRule("sleep", "allow"),)
        ),
        plugins=[
            Memory(root / "memory", auto_curate=False),
            Todo(),
            Skills(root / "skills"),
            MCP(stub("calc", defer=False), stub("stub", defer=True)),
        ],
    )
    session = SQLiteSession(root / "session.db")
    for prompt in ("Write the notes and check the math.", "Now wrap it up."):
        await Runner.run(agent, prompt, session=session, session_id=_SESSION)

    assert not provider._script, "every scripted turn was consumed"
    system = provider.requests[0].entries[0]
    history = await session.load(_SESSION)
    notes = (root / "memory" / "MEMORY.md").read_text()
    return _Recording(provider.requests, [system, *history], notes)


@pytest.fixture(scope="module")
def recording(tmp_path_factory: pytest.TempPathFactory) -> _Recording:
    return asyncio.run(_drive(tmp_path_factory.mktemp("prefix")))


def _dump(entry: TranscriptEntry) -> str:
    return json.dumps(entry_to_dict(entry))


def _stable_len(request: _Request, transcript: list[str]) -> int:
    """Length of the request's head that is the transcript verbatim.

    What follows is the transient view-injector tail: entries the transcript
    never contains.
    """
    entries = [_dump(e) for e in request.entries]
    n = 0
    while n < len(entries) and n < len(transcript) and entries[n] == transcript[n]:
        n += 1
    tail = request.entries[n:]
    assert all(
        isinstance(e, InputEntry) and _dump(e) not in transcript for e in tail
    ), f"non-transient entries after the stable head: {tail}"
    return n


# --------------------------------------------------------------------------- #
# Layer 1 — lovia's request (entries + tools)
# --------------------------------------------------------------------------- #
def test_tool_list_is_identical_across_every_request(recording: _Recording) -> None:
    first = json.dumps(recording.requests[0].tools)
    assert recording.requests[0].tools
    for i, request in enumerate(recording.requests[1:], 1):
        assert json.dumps(request.tools) == first, f"tools changed at request {i}"


def test_system_entry_is_identical_across_runs(recording: _Recording) -> None:
    # Run 1 saved a note; Memory pins the session's Notes so the prompt keeps
    # the snapshot it started with (#243).
    assert "green tea" in recording.notes
    first = _dump(recording.requests[0].entries[0])
    assert "green tea" not in first
    for request in recording.requests:
        assert request.entries[0].role == "system"  # type: ignore[union-attr]
        assert _dump(request.entries[0]) == first


def test_each_request_extends_the_previous_one_verbatim(
    recording: _Recording,
) -> None:
    transcript = [_dump(e) for e in recording.transcript]
    stable = [_stable_len(r, transcript) for r in recording.requests]
    tails = [
        e.content
        for r, n in zip(recording.requests, stable)
        for e in r.entries[n:]
        if isinstance(e, InputEntry)
    ]
    # The tail stripping is not vacuous: both injectors fired.
    assert any("Your current todo list" in str(c) for c in tails)
    assert any("Background processes" in str(c) for c in tails)

    for i in range(len(recording.requests) - 1):
        head = [_dump(e) for e in recording.requests[i].entries[: stable[i]]]
        nxt = [_dump(e) for e in recording.requests[i + 1].entries]
        assert stable[i + 1] > stable[i]
        assert nxt[: len(head)] == head, f"request {i + 1} rewrote request {i}"


# --------------------------------------------------------------------------- #
# Layer 2 — provider serialization (offline: _build_payload, no network)
# --------------------------------------------------------------------------- #
def _anthropic() -> AnthropicProvider:
    return AnthropicProvider(model="claude-haiku-4-5", api_key="x")


def _openai() -> OpenAIChatProvider:
    return OpenAIChatProvider(model="gpt-4o-mini", api_key="x")


def _payload(provider: Any, request: _Request, entries: list[Any]) -> Any:
    return provider._build_payload(
        entries=entries,
        tools=request.tools,
        response_format=None,
        settings=request.settings,
        stream=True,
    )


def _head(payload: Any, *, anthropic: bool) -> str:
    """The parts that must never change: tools and the system prompt."""
    system = payload["system"] if anthropic else payload["messages"][0]
    return json.dumps({"tools": payload["tools"], "system": system})


def _messages(payload: Any, *, anthropic: bool) -> list[str]:
    """Per-message JSON, Anthropic ``cache_control`` markers stripped."""
    out = []
    for message in payload["messages"]:
        content = message.get("content")
        if isinstance(content, list):
            message = {
                **message,
                "content": [
                    {k: v for k, v in block.items() if k != "cache_control"}
                    for block in content
                ],
            }
        out.append(json.dumps(message))
    return out[0 if anthropic else 1 :]


@pytest.mark.parametrize("make", [_anthropic, _openai], ids=["anthropic", "openai"])
def test_serialized_requests_share_a_byte_identical_prefix(
    recording: _Recording, make: Any
) -> None:
    provider = make()
    anthropic = isinstance(provider, AnthropicProvider)
    transcript = [_dump(e) for e in recording.transcript]
    requests = recording.requests
    wire = [_payload(provider, r, r.entries) for r in requests]
    # The same request without its transient tail: what N+1 must repeat.
    stable = [
        _payload(provider, r, r.entries[: _stable_len(r, transcript)]) for r in requests
    ]

    first = _head(wire[0], anthropic=anthropic)
    for i, payload in enumerate(wire):
        assert _head(payload, anthropic=anthropic) == first, f"head changed at {i}"
    for i in range(len(requests) - 1):
        head = _messages(stable[i], anthropic=anthropic)
        nxt = _messages(wire[i + 1], anthropic=anthropic)
        assert nxt[: len(head)] == head, f"request {i + 1} rewrote request {i}"


def test_anthropic_breakpoint_prefix_reappears_in_the_next_request(
    recording: _Recording,
) -> None:
    # What the cache actually keys on: the wire bytes through request N's
    # history breakpoint, which must sit before N's transient tail.
    provider = _anthropic()
    wire = [_payload(provider, r, r.entries) for r in recording.requests]
    checked = 0
    for cur, nxt in zip(wire, wire[1:]):
        marks = [
            (m, b)
            for m, message in enumerate(cur["messages"])
            for b, block in enumerate(message["content"])
            if "cache_control" in block
        ]
        if not marks:  # the very first request has no history yet
            continue
        [(m, b)] = marks
        assert _through(cur, m, b) == _through(nxt, m, b)
        checked += 1
    assert checked == len(wire) - 2


def _through(payload: Any, m: int, b: int) -> str:
    """Messages up to block ``b`` of message ``m``, markers stripped."""
    out = []
    for i, message in enumerate(payload["messages"][: m + 1]):
        blocks = message["content"] if i < m else message["content"][: b + 1]
        content = [
            {k: v for k, v in blk.items() if k != "cache_control"} for blk in blocks
        ]
        out.append({**message, "content": content})
    return json.dumps(out)
