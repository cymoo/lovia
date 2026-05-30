"""Round-trip tests for the Item type family and ItemDelta streaming types."""

from __future__ import annotations

import pytest

from lovia import (
    FinishDelta,
    ImageBlock,
    InputMessageItem,
    MessageOutputItem,
    ReasoningDelta,
    ReasoningItem,
    TextBlock,
    TextDelta,
    ToolCallDelta,
    ToolCallItem,
    ToolCallOutputItem,
    Usage,
    UsageDelta,
    item_from_dict,
    item_to_dict,
)


@pytest.mark.parametrize(
    "item",
    [
        InputMessageItem(role="system", content="be helpful"),
        InputMessageItem(role="user", content="hello"),
        InputMessageItem(
            role="user",
            content=[TextBlock(text="caption:"), ImageBlock(url="https://x/i.png")],
        ),
        MessageOutputItem(content="hi there"),
        MessageOutputItem(content="with id", id="msg_123"),
        ReasoningItem(content="thinking..."),
        ReasoningItem(content="opaque", id="r_42"),
        ToolCallItem(call_id="c1", name="add", arguments='{"a":1,"b":2}'),
        ToolCallOutputItem(call_id="c1", output="3"),
        ToolCallOutputItem(call_id="c2", output="boom", is_error=True),
    ],
)
def test_item_roundtrip(item: object) -> None:
    """Every Item kind survives a dict round-trip unchanged."""
    payload = item_to_dict(item)  # type: ignore[arg-type]
    restored = item_from_dict(payload)
    assert restored == item


def test_input_message_image_block_roundtrip() -> None:
    """Image blocks inside InputMessageItem keep their fields after a round trip."""
    item = InputMessageItem(
        role="user",
        content=[ImageBlock(data="aGVsbG8=", mime_type="image/png", detail="high")],
    )
    restored = item_from_dict(item_to_dict(item))
    assert isinstance(restored, InputMessageItem)
    assert isinstance(restored.content, list)
    block = restored.content[0]
    assert isinstance(block, ImageBlock)
    assert block.data == "aGVsbG8="
    assert block.mime_type == "image/png"
    assert block.detail == "high"


def test_item_from_dict_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="Unknown item type"):
        item_from_dict({"type": "nope"})


def test_item_from_dict_rejects_unknown_field() -> None:
    """Extra fields surface as TypeError rather than being silently dropped."""
    with pytest.raises(TypeError):
        item_from_dict({"type": "message_output", "content": "hi", "mystery_field": 1})


def test_item_to_dict_preserves_type_discriminator() -> None:
    """The ``type`` discriminator is what makes the union round-trip — keep it."""
    payload = item_to_dict(MessageOutputItem(content="x"))
    assert payload["type"] == "message_output"


def test_delta_types_construct() -> None:
    """Delta dataclasses exist and accept their documented fields."""
    assert TextDelta(text="hi").text == "hi"
    assert ReasoningDelta(text="why").text == "why"
    td = ToolCallDelta(index=0, call_id="c1", name="add", arguments='{"a":')
    assert td.index == 0 and td.call_id == "c1" and td.name == "add"
    assert UsageDelta(usage=Usage(input_tokens=10)).usage.input_tokens == 10
    assert FinishDelta(reason="stop").reason == "stop"
