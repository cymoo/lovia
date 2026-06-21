"""Unit tests for ``lovia.runtime.utils`` logging/label helpers."""

from __future__ import annotations

from lovia import Agent
from lovia.messages import Message
from lovia.runtime.utils import (
    agent_model_label,
    input_preview,
    supports_json_schema,
    truncate_repr,
)


# ------------------------------------------------------------ truncate_repr


def test_truncate_repr_passes_through_short_string() -> None:
    assert truncate_repr("hello") == "hello"


def test_truncate_repr_uses_repr_for_non_strings() -> None:
    assert truncate_repr(123) == "123"
    assert truncate_repr(["a", "b"]) == "['a', 'b']"


def test_truncate_repr_clips_long_values() -> None:
    out = truncate_repr("x" * 250, max_len=200)
    assert out.startswith("x" * 200)
    assert out.endswith("<+50 chars>")


def test_truncate_repr_survives_a_raising_repr() -> None:
    class Boom:
        def __repr__(self) -> str:
            raise RuntimeError("nope")

    assert truncate_repr(Boom()) == "<unrepr>"


# --------------------------------------------------------- agent_model_label


def test_agent_model_label_string_model() -> None:
    assert agent_model_label(Agent(name="a", model="openai:gpt-5.4")) == "openai:gpt-5.4"


def test_agent_model_label_list_of_providers() -> None:
    class _WithModel:
        model = "gpt-x"

    class _WithName:
        name = "scripted"

    agent = Agent(name="a", model=[_WithModel(), _WithName()])  # type: ignore[list-item]
    assert agent_model_label(agent) == "gpt-x,scripted"


def test_agent_model_label_single_provider_prefers_model_then_name() -> None:
    class _WithModel:
        model = "claude"

    class _WithName:
        name = "namey"

    assert agent_model_label(Agent(name="a", model=_WithModel())) == "claude"  # type: ignore[arg-type]
    assert agent_model_label(Agent(name="a", model=_WithName())) == "namey"  # type: ignore[arg-type]


# ------------------------------------------------------------- input_preview


def test_input_preview_string() -> None:
    assert input_preview("hi there") == "hi there"


def test_input_preview_skips_system_messages() -> None:
    msgs = [
        Message(role="system", content="you are a bot"),
        Message(role="user", content="actual question"),
    ]
    assert input_preview(msgs) == "actual question"


def test_input_preview_all_system_returns_empty() -> None:
    assert input_preview([Message(role="system", content="x")]) == ""


# -------------------------------------------------------- supports_json_schema


def test_supports_json_schema_requires_all_providers() -> None:
    class _Yes:
        supports_json_schema = True

    class _No:
        supports_json_schema = False

    assert supports_json_schema([_Yes(), _Yes()]) is True  # type: ignore[list-item]
    assert supports_json_schema([_Yes(), _No()]) is False  # type: ignore[list-item]


def test_supports_json_schema_empty_chain_is_false() -> None:
    assert supports_json_schema([]) is False
