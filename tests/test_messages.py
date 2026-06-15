"""Tests for the message model: content parts, Usage, Message helpers."""

from __future__ import annotations

from typing import Any

import pytest

from lovia import FilePart, ImagePart, TextPart, user
from lovia.parts import normalize_content, text_of
from lovia.messages import AssistantTurn, Message, Usage
from lovia.providers.openai_chat import message_to_openai

# ---------- TextPart / ImagePart / FilePart ----------


def test_text_part_serializes_to_openai_parts() -> None:
    msg = user([TextPart("hello"), ImagePart(url="https://x/y.png")])
    payload = message_to_openai(msg)
    assert payload["content"] == [
        {"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
    ]


def test_image_part_base64_serializes_with_data_url() -> None:
    msg = user(ImagePart(data="ZmFrZQ==", mime_type="image/png"))
    payload = message_to_openai(msg)
    assert payload["content"][0]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


def test_image_part_requires_exactly_one_source() -> None:
    with pytest.raises(ValueError):
        ImagePart()
    with pytest.raises(ValueError):
        ImagePart(url="x", data="y", mime_type="image/png")


def test_image_part_base64_requires_mime_type() -> None:
    with pytest.raises(ValueError):
        ImagePart(data="ZmFrZQ==")


def test_file_part_from_path_infers_mime_type_and_filename(tmp_path: Any) -> None:
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"pdf")

    part = FilePart.from_path(path)

    assert part.data == "cGRm"
    assert part.mime_type == "application/pdf"
    assert part.filename == "doc.pdf"


def test_file_part_requires_exactly_one_source() -> None:
    with pytest.raises(ValueError):
        FilePart()
    with pytest.raises(ValueError):
        FilePart(url="https://x/doc.pdf", data="cGRm", mime_type="application/pdf")


def test_file_part_base64_requires_mime_type() -> None:
    with pytest.raises(ValueError):
        FilePart(data="cGRm")


def test_file_part_rejects_invalid_base64_data() -> None:
    with pytest.raises(ValueError, match="valid base64"):
        FilePart(data="not base64", mime_type="application/pdf")


# ---------- normalize_content / text_of ----------


def test_normalize_content_returns_str_for_str() -> None:
    assert normalize_content("hello") == "hello"


def test_normalize_content_wraps_single_part_in_list() -> None:
    part = TextPart("hi")
    assert normalize_content(part) == [part]


def test_normalize_content_returns_none_for_none() -> None:
    assert normalize_content(None) is None


def test_text_of_includes_image_placeholder() -> None:
    content = [
        TextPart("alpha "),
        ImagePart(url="x"),
        FilePart.from_url("https://x/doc.pdf", filename="doc.pdf"),
        TextPart("beta"),
    ]
    out = text_of(content)
    assert "alpha" in out and "beta" in out and "[image]" in out
    assert "[file:doc.pdf]" in out


def test_text_of_returns_empty_for_none() -> None:
    assert text_of(None) == ""


# ---------- Message.text helper ----------


def test_message_text_returns_str_when_content_is_str() -> None:
    msg = Message(role="user", content="hi")
    assert msg.text == "hi"


def test_message_text_concatenates_text_parts() -> None:
    msg = Message(
        role="user",
        content=[TextPart("ping "), ImagePart(url="x"), TextPart("pong")],
    )
    assert "ping" in msg.text and "pong" in msg.text


def test_message_text_empty_for_none_content() -> None:
    msg = Message(role="assistant", content=None)
    assert msg.text == ""


# ---------- Usage ----------


def test_usage_add_accumulates_all_counters() -> None:
    u = Usage(
        input_tokens=1, output_tokens=2, cache_read_tokens=3, cache_write_tokens=4
    )
    u.add(
        Usage(
            input_tokens=10,
            output_tokens=20,
            cache_read_tokens=30,
            cache_write_tokens=40,
        )
    )
    assert u.input_tokens == 11
    assert u.output_tokens == 22
    assert u.cache_read_tokens == 33
    assert u.cache_write_tokens == 44


def test_usage_total_is_sum_of_input_and_output() -> None:
    u = Usage(input_tokens=5, output_tokens=7)
    assert u.total_tokens == 12


# ---------- user() helper accepts various shapes ----------


def test_user_helper_accepts_single_part() -> None:
    msg = user(TextPart("hi"))
    assert isinstance(msg.content, list)
    assert msg.content[0].text == "hi"


def test_assistant_turn_to_message_is_chat_compatible_view() -> None:
    am = AssistantTurn(content="answer")
    chat = am.to_message()
    assert chat.content == "answer"
    assert not hasattr(chat, "reasoning_content")
