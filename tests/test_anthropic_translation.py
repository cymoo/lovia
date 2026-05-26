from __future__ import annotations

from lovia.messages import ChatMessage, ToolCall
from lovia.providers.anthropic import (
    _openai_tool_to_anthropic,
    _parse_anthropic_response,
    _to_anthropic_messages,
)


def test_message_translation_extracts_system() -> None:
    msgs = [
        ChatMessage(role="system", content="be terse"),
        ChatMessage(role="user", content="hi"),
        ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[ToolCall(id="c1", name="add", arguments='{"a":1,"b":2}')],
        ),
        ChatMessage(role="tool", content="3", tool_call_id="c1"),
        ChatMessage(role="user", content="thanks"),
    ]
    system, out = _to_anthropic_messages(msgs)
    assert system == "be terse"
    assert out[0] == {"role": "user", "content": "hi"}
    # Assistant message becomes a content list with a tool_use block.
    assert out[1]["role"] == "assistant"
    blocks = out[1]["content"]
    assert blocks[0]["type"] == "tool_use"
    assert blocks[0]["input"] == {"a": 1, "b": 2}
    # Tool result becomes a user message with a tool_result block.
    assert out[2]["role"] == "user"
    assert out[2]["content"][0]["type"] == "tool_result"
    assert out[2]["content"][0]["tool_use_id"] == "c1"


def test_tool_translation() -> None:
    schema = {
        "type": "function",
        "function": {
            "name": "add",
            "description": "sum",
            "parameters": {"type": "object", "properties": {"a": {"type": "integer"}}},
        },
    }
    out = _openai_tool_to_anthropic(schema)
    assert out["name"] == "add"
    assert out["input_schema"]["properties"]["a"]["type"] == "integer"


def test_response_parsing() -> None:
    data = {
        "content": [
            {"type": "text", "text": "Sure, "},
            {"type": "tool_use", "id": "tu1", "name": "add", "input": {"a": 1, "b": 2}},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "stop_reason": "tool_use",
    }
    msg = _parse_anthropic_response(data)
    assert msg.content == "Sure, "
    assert msg.tool_calls[0].name == "add"
    assert msg.finish_reason == "tool_calls"
    assert msg.usage.input_tokens == 10
