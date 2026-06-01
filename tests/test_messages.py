"""Tests for the message model: content blocks, Usage, ChatMessage helpers."""

from __future__ import annotations

from typing import Any

import pytest

from lovia import FileBlock, ImageBlock, TextBlock, user
from lovia.content import normalize_content, text_of
from lovia.messages import AssistantMessage, ChatMessage, Usage
from lovia.providers.openai_chat import message_to_openai


# ---------- TextBlock / ImageBlock / FileBlock ----------


def test_text_block_serializes_to_openai_parts() -> None:
    msg = user([TextBlock("hello"), ImageBlock(url="https://x/y.png")])
    payload = message_to_openai(msg)
    assert payload["content"] == [
        {"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
    ]


def test_image_block_base64_serializes_with_data_url() -> None:
    msg = user(ImageBlock(data="ZmFrZQ==", mime_type="image/png"))
    payload = message_to_openai(msg)
    assert payload["content"][0]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


def test_image_block_requires_exactly_one_source() -> None:
    with pytest.raises(ValueError):
        ImageBlock()
    with pytest.raises(ValueError):
        ImageBlock(url="x", data="y", mime_type="image/png")


def test_image_block_base64_requires_mime_type() -> None:
    with pytest.raises(ValueError):
        ImageBlock(data="ZmFrZQ==")


def test_file_block_from_path_infers_mime_type_and_filename(tmp_path: Any) -> None:
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"pdf")

    block = FileBlock.from_path(path)

    assert block.data == "cGRm"
    assert block.mime_type == "application/pdf"
    assert block.filename == "doc.pdf"


def test_file_block_requires_exactly_one_source() -> None:
    with pytest.raises(ValueError):
        FileBlock()
    with pytest.raises(ValueError):
        FileBlock(url="https://x/doc.pdf", data="cGRm", mime_type="application/pdf")


def test_file_block_base64_requires_mime_type() -> None:
    with pytest.raises(ValueError):
        FileBlock(data="cGRm")


def test_file_block_rejects_invalid_base64_data() -> None:
    with pytest.raises(ValueError, match="valid base64"):
        FileBlock(data="not base64", mime_type="application/pdf")


# ---------- normalize_content / text_of ----------


def test_normalize_content_returns_str_for_str() -> None:
    assert normalize_content("hello") == "hello"


def test_normalize_content_wraps_single_block_in_list() -> None:
    block = TextBlock("hi")
    assert normalize_content(block) == [block]


def test_normalize_content_returns_none_for_none() -> None:
    assert normalize_content(None) is None


def test_text_of_includes_image_placeholder() -> None:
    content = [
        TextBlock("alpha "),
        ImageBlock(url="x"),
        FileBlock.from_url("https://x/doc.pdf", filename="doc.pdf"),
        TextBlock("beta"),
    ]
    out = text_of(content)
    assert "alpha" in out and "beta" in out and "[image]" in out
    assert "[file:doc.pdf]" in out


def test_text_of_returns_empty_for_none() -> None:
    assert text_of(None) == ""


# ---------- ChatMessage.text helper ----------


def test_chat_message_text_returns_str_when_content_is_str() -> None:
    msg = ChatMessage(role="user", content="hi")
    assert msg.text == "hi"


def test_chat_message_text_concatenates_text_blocks() -> None:
    msg = ChatMessage(
        role="user",
        content=[TextBlock("ping "), ImageBlock(url="x"), TextBlock("pong")],
    )
    assert "ping" in msg.text and "pong" in msg.text


def test_chat_message_text_empty_for_none_content() -> None:
    msg = ChatMessage(role="assistant", content=None)
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


def test_user_helper_accepts_single_block() -> None:
    msg = user(TextBlock("hi"))
    assert isinstance(msg.content, list)
    assert msg.content[0].text == "hi"


def test_assistant_message_to_chat_message_is_chat_compatible_view() -> None:
    am = AssistantMessage(content="answer")
    chat = am.to_chat_message()
    assert chat.content == "answer"
    assert not hasattr(chat, "reasoning_content")
