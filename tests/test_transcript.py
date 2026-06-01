"""Round-trip tests for the TranscriptEntry type family and ModelDelta streaming types."""

from __future__ import annotations

import pytest

from lovia import (
    FinishDelta,
    FilePart,
    ImagePart,
    InputEntry,
    AssistantTextEntry,
    ReasoningDelta,
    ReasoningEntry,
    TextPart,
    TextDelta,
    ToolCallDelta,
    ToolCallEntry,
    ToolResultEntry,
    Usage,
    UsageDelta,
    entry_from_dict,
    entry_to_dict,
)


@pytest.mark.parametrize(
    "entry",
    [
        InputEntry(role="system", content="be helpful"),
        InputEntry(role="user", content="hello"),
        InputEntry(
            role="user",
            content=[
                TextPart(text="caption:"),
                ImagePart(url="https://x/i.png"),
                FilePart.from_url("https://x/doc.pdf", filename="doc.pdf"),
            ],
        ),
        AssistantTextEntry(content="hi there"),
        AssistantTextEntry(content="with id", id="msg_123"),
        ReasoningEntry(content="thinking..."),
        ReasoningEntry(content="opaque", id="r_42"),
        ToolCallEntry(call_id="c1", name="add", arguments='{"a":1,"b":2}'),
        ToolResultEntry(call_id="c1", output="3"),
        ToolResultEntry(call_id="c2", output="boom", is_error=True),
    ],
)
def test_entry_roundtrip(entry: object) -> None:
    """Every TranscriptEntry kind survives a dict round-trip unchanged."""
    payload = entry_to_dict(entry)  # type: ignore[arg-type]
    restored = entry_from_dict(payload)
    assert restored == entry


def test_input_entry_image_part_roundtrip() -> None:
    """Image parts inside InputEntry keep their fields after a round trip."""
    entry = InputEntry(
        role="user",
        content=[ImagePart(data="aGVsbG8=", mime_type="image/png", detail="high")],
    )
    restored = entry_from_dict(entry_to_dict(entry))
    assert isinstance(restored, InputEntry)
    assert isinstance(restored.content, list)
    part = restored.content[0]
    assert isinstance(part, ImagePart)
    assert part.data == "aGVsbG8="
    assert part.mime_type == "image/png"
    assert part.detail == "high"


def test_input_entry_file_part_roundtrip() -> None:
    entry = InputEntry(
        role="user",
        content=[
            FilePart(
                data="cGRm",
                mime_type="application/pdf",
                filename="doc.pdf",
            )
        ],
    )
    restored = entry_from_dict(entry_to_dict(entry))
    assert isinstance(restored, InputEntry)
    assert isinstance(restored.content, list)
    part = restored.content[0]
    assert isinstance(part, FilePart)
    assert part.data == "cGRm"
    assert part.mime_type == "application/pdf"
    assert part.filename == "doc.pdf"


def test_entry_from_dict_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="Unknown entry type"):
        entry_from_dict({"type": "nope"})


def test_entry_from_dict_rejects_unknown_field() -> None:
    """Extra fields surface as TypeError rather than being silently dropped."""
    with pytest.raises(TypeError):
        entry_from_dict({"type": "assistant_text", "content": "hi", "mystery_field": 1})


def test_entry_to_dict_preserves_type_discriminator() -> None:
    """The ``type`` discriminator is what makes the union round-trip — keep it."""
    payload = entry_to_dict(AssistantTextEntry(content="x"))
    assert payload["type"] == "assistant_text"


def test_delta_types_construct() -> None:
    """Delta dataclasses exist and accept their documented fields."""
    assert TextDelta(text="hi").text == "hi"
    assert ReasoningDelta(text="why").text == "why"
    td = ToolCallDelta(index=0, call_id="c1", name="add", arguments='{"a":')
    assert td.index == 0 and td.call_id == "c1" and td.name == "add"
    assert UsageDelta(usage=Usage(input_tokens=10)).usage.input_tokens == 10
    assert FinishDelta(reason="stop").reason == "stop"
