"""The web API's wire contract: error codes and SSE payloads.

Three guards: the HTTP API docs tables (en + zh) list exactly what the code
emits, ``event_to_sse`` output conforms to its registered payload type, and
errors carry their codes end to end.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any, get_args

import httpx
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent, events  # noqa: E402
from lovia.exceptions import (  # noqa: E402
    BudgetExceeded,
    ContextOverflowError,
    MaxTurnsExceeded,
    ProviderError,
    UserError,
)
from lovia.messages import ToolCall, Usage  # noqa: E402
from lovia.parts import ImagePart, TextPart  # noqa: E402
from lovia.plugins import TodoItem  # noqa: E402
from lovia.runtime.result import RunResult  # noqa: E402
from lovia.web import create_app  # noqa: E402
from lovia.web.errors import ErrorCode, RunErrorCode, WebError, run_error_code  # noqa: E402
from lovia.web.sse import (  # noqa: E402
    CHAT_EVENTS,
    LIFECYCLE_EVENTS,
    ContextCompactedData,
    event_to_sse,
)
from lovia.web.store import ChatStore  # noqa: E402

from ..scripted_provider import ScriptedProvider  # noqa: E402

DOCS = Path(__file__).parents[2] / "docs"

# Each contract table, found by its header's first cell.
HEADERS = {
    "en": {
        "http": "Error code",
        "run": "Run error code",
        "chat": "SSE event",
        "lifecycle": "Lifecycle event",
    },
    "zh": {
        "http": "错误码",
        "run": "运行错误码",
        "chat": "SSE 事件",
        "lifecycle": "生命周期事件",
    },
}


def _tables(path: Path) -> dict[str, list[list[str]]]:
    """Markdown tables keyed by their header's first cell."""
    lines = path.read_text(encoding="utf-8").splitlines()
    tables: dict[str, list[list[str]]] = {}
    i = 0
    while i < len(lines) - 1:
        if lines[i].startswith("|") and re.match(r"^\|\s*-", lines[i + 1]):
            header = _cells(lines[i])[0]
            i += 2
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            tables[header] = rows
        else:
            i += 1
    return tables


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _names(rows: list[list[str]]) -> list[str]:
    return [re.fullmatch(r"`(\w+)`", row[0]).group(1) for row in rows]  # type: ignore[union-attr]


def _documented_payloads(rows: list[list[str]]) -> dict[str, tuple[set[str], set[str]]]:
    """``{event: (required fields, optional fields)}`` from a payload table."""
    out = {}
    for name, row in zip(_names(rows), rows):
        m = re.match(r"`\{([^}]*)\}`", row[1])
        assert m, f"{name}: payload cell must start with a `{{fields}}` span"
        fields = [f.strip() for f in m.group(1).split(",") if f.strip()]
        out[name] = (
            {f for f in fields if not f.endswith("?")},
            {f[:-1] for f in fields if f.endswith("?")},
        )
    return out


def _declared_payloads(
    registry: dict[str, type],
) -> dict[str, tuple[set[str], set[str]]]:
    return {
        name: (set(td.__required_keys__), set(td.__optional_keys__))  # type: ignore[attr-defined]
        for name, td in registry.items()
    }


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_docs_tables_match_the_code(lang: str) -> None:
    tables = _tables(DOCS / lang / "http-api.md")
    headers = HEADERS[lang]
    assert _names(tables[headers["http"]]) == list(get_args(ErrorCode))
    assert _names(tables[headers["run"]]) == list(get_args(RunErrorCode))
    assert _documented_payloads(tables[headers["chat"]]) == _declared_payloads(
        CHAT_EVENTS
    )
    assert _documented_payloads(tables[headers["lifecycle"]]) == _declared_payloads(
        LIFECYCLE_EVENTS
    )


def test_context_compacted_payload_tracks_the_notice() -> None:
    # The event forwards the notice whole; its type must name every field.
    notice_fields = {f.name for f in dataclasses.fields(events.CompactionNotice)}
    assert set(ContextCompactedData.__required_keys__) == {"session_id"} | notice_fields


def _every_chat_event() -> list[events.Event]:
    agent = Agent(name="a", model=ScriptedProvider([]))
    call = ToolCall(id="c1", name="t", arguments="{}")
    return [
        events.TextDelta(delta="x"),
        events.ReasoningDelta(delta="x"),
        events.OutputDiscarded(),
        events.MessageCompleted(entries=[]),
        events.UserMessageInjected(content="x", turn=1),
        events.ToolCallStarted(call=call),
        events.ToolCallCompleted(call=call, result="ok", output="ok"),
        events.ToolCallCompleted(
            call=call,
            result=[TextPart("x"), ImagePart(data="aGk=", mime_type="image/png")],
            output="x",
        ),
        events.ToolCallCompleted(
            call=call, result=[TodoItem(content="x", status="pending")], output=""
        ),
        events.ApprovalRequired(call=call),
        events.HandoffOccurred(from_agent=agent, to_agent=agent),
        events.TurnStarted(agent=agent, turn=1),
        events.ContextCompacted(
            session_id="s",
            entries_before=[],
            entries_after=[],
            notice=events.CompactionNotice(reason="r", reactive=False),
        ),
        events.ToolCallFailed(error=RuntimeError("x"), call=call),
        events.RunFailed(
            error=ProviderError("x", status_code=429, retryable=True, hint="wait")
        ),
        events.RunCompleted(
            result=RunResult(
                output="x", entries=[], final_agent=agent, usage=Usage(), turns=1
            )
        ),
    ]


def test_every_chat_event_conforms_to_its_payload_type() -> None:
    emitted = set()
    for ev in _every_chat_event():
        payload = event_to_sse(ev)
        assert payload is not None, type(ev).__name__
        name = payload["event"]
        td: Any = CHAT_EVENTS[name]
        keys = set(json.loads(payload["data"]))
        assert (
            td.__required_keys__ <= keys <= td.__required_keys__ | td.__optional_keys__
        ), name
        emitted.add(name)
    # `session` and `snapshot` are framed by the supervisor, not event_to_sse.
    assert emitted == set(CHAT_EVENTS) - {"session", "snapshot"}


def _data(ev: events.Event) -> dict[str, Any]:
    payload = event_to_sse(ev)
    assert payload is not None
    return json.loads(payload["data"])


def test_error_event_carries_code_and_provider_facts() -> None:
    data = _data(
        events.RunFailed(
            error=ProviderError(
                "slow down", status_code=429, retryable=True, hint="wait"
            )
        )
    )
    assert data == {
        "type": "ProviderError",
        "message": "slow down",
        "code": "rate_limited",
        "status_code": 429,
        "retryable": True,
        "hint": "wait",
    }
    # A tool failure is tool-scoped whatever raised it.
    call = ToolCall(id="c1", name="t", arguments="{}")
    tool = _data(
        events.ToolCallFailed(error=ProviderError("x", status_code=429), call=call)
    )
    assert tool["code"] == "tool_error"


def _transport_error(cause: BaseException) -> ProviderError:
    try:
        raise ProviderError("stream failed") from cause
    except ProviderError as exc:
        return exc


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (ProviderError("x", status_code=401), "provider_auth"),
        (ProviderError("x", status_code=403), "provider_auth"),
        (ProviderError("x", status_code=429), "rate_limited"),
        (ProviderError("x", status_code=529), "overloaded"),
        (ProviderError("x", status_code=504), "timeout"),
        (ProviderError("x", status_code=500), "provider_error"),
        (_transport_error(httpx.ReadTimeout("t")), "timeout"),
        (_transport_error(httpx.ConnectError("c")), "network"),
        (ProviderError("x"), "provider_error"),
        (ContextOverflowError("x"), "context_overflow"),
        (BudgetExceeded("x"), "budget_exceeded"),
        (MaxTurnsExceeded("x"), "max_turns"),
        (RuntimeError("x"), "internal"),
    ],
)
def test_run_error_code(exc: BaseException, code: str) -> None:
    assert run_error_code(exc) == code


def test_web_error_lifts_a_lovia_hint_out_of_the_message() -> None:
    err = WebError.from_exc(400, "invalid_request", UserError("bad", hint="fix it"))
    assert err.detail == {"code": "invalid_request", "message": "bad", "hint": "fix it"}
    plain = WebError.from_exc(400, "invalid_request", ValueError("bad"))
    assert plain.detail == {"code": "invalid_request", "message": "bad"}


def test_route_errors_answer_with_the_coded_body() -> None:
    app = create_app(
        Agent(name="bot", model=ScriptedProvider([])),
        store=ChatStore.in_memory(),
        generate_titles=False,
    )
    r = TestClient(app).get("/api/agents/nope")
    assert r.status_code == 404
    assert r.json() == {
        "detail": {"code": "agent_not_found", "message": "unknown agent 'nope'"}
    }
