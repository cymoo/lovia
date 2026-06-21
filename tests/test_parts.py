"""Unit tests for structured content parts (``lovia.parts``).

Covers the validation invariants, ``from_*`` constructors, and the
``normalize_content`` / ``text_of`` helpers that providers and the
summarizer rely on.
"""

from __future__ import annotations

import base64

import pytest

from lovia.parts import (
    ContentPart,
    FilePart,
    ImagePart,
    TextPart,
    normalize_content,
    text_of,
)


# --------------------------------------------------------------- TextPart ---


def test_text_part_defaults() -> None:
    p = TextPart(text="hello")
    assert p.text == "hello"
    assert p.type == "text"


# -------------------------------------------------------------- ImagePart ---


def test_image_part_url_only_ok() -> None:
    p = ImagePart(url="https://example.com/cat.png")
    assert p.url is not None and p.data is None
    assert p.type == "image"


def test_image_part_data_with_mime_ok() -> None:
    p = ImagePart(data=base64.b64encode(b"x").decode(), mime_type="image/png")
    assert p.data is not None and p.url is None


def test_image_part_requires_exactly_one_source() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ImagePart()  # neither
    with pytest.raises(ValueError, match="exactly one"):
        ImagePart(url="u", data="d", mime_type="image/png")  # both


def test_image_part_data_requires_mime() -> None:
    with pytest.raises(ValueError, match="mime_type"):
        ImagePart(data=base64.b64encode(b"x").decode())


def test_image_part_from_path_infers_mime(tmp_path) -> None:
    img = tmp_path / "pic.PNG"  # upper-case suffix exercises .lower()
    img.write_bytes(b"\x89PNG\r\n")
    p = ImagePart.from_path(img)
    assert p.mime_type == "image/png"
    assert base64.b64decode(p.data) == b"\x89PNG\r\n"  # type: ignore[arg-type]


def test_image_part_from_path_unknown_suffix_raises(tmp_path) -> None:
    f = tmp_path / "pic.svg"
    f.write_bytes(b"<svg/>")
    with pytest.raises(ValueError, match="infer mime_type"):
        ImagePart.from_path(f)


# --------------------------------------------------------------- FilePart ---


def test_file_part_requires_exactly_one_source() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        FilePart()
    with pytest.raises(ValueError, match="exactly one"):
        FilePart(url="u", data="ZA==", mime_type="text/plain")


def test_file_part_data_requires_mime() -> None:
    with pytest.raises(ValueError, match="mime_type"):
        FilePart(data="ZA==")


def test_file_part_rejects_invalid_base64() -> None:
    # "not base64!!" contains non-alphabet chars -> validated and rejected.
    with pytest.raises(ValueError, match="valid base64"):
        FilePart(data="not base64!!", mime_type="text/plain")


def test_file_part_from_bytes_and_from_base64_roundtrip() -> None:
    a = FilePart.from_bytes(b"hello", mime_type="text/plain", filename="a.txt")
    assert base64.b64decode(a.data) == b"hello"  # type: ignore[arg-type]
    assert a.filename == "a.txt"
    b = FilePart.from_base64(a.data, mime_type="text/plain")  # type: ignore[arg-type]
    assert b.data == a.data


def test_file_part_from_url_keeps_reference() -> None:
    p = FilePart.from_url("https://example.com/doc.pdf", mime_type="application/pdf")
    assert p.url == "https://example.com/doc.pdf"
    assert p.data is None


def test_file_part_from_path_guesses_mime_and_filename(tmp_path) -> None:
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4")
    p = FilePart.from_path(f)
    assert p.mime_type == "application/pdf"
    assert p.filename == "report.pdf"


# ------------------------------------------------------- normalize_content ---


def test_normalize_content_passthrough_str_and_none() -> None:
    assert normalize_content(None) is None
    assert normalize_content("hi") == "hi"


def test_normalize_content_wraps_single_part() -> None:
    p = TextPart(text="x")
    assert normalize_content(p) == [p]


def test_normalize_content_copies_list() -> None:
    src: list[ContentPart] = [TextPart(text="x")]
    out = normalize_content(src)
    assert out == src
    assert out is not src  # a fresh list, not the caller's


# ---------------------------------------------------------------- text_of ---


def test_text_of_none_and_str() -> None:
    assert text_of(None) == ""
    assert text_of("plain") == "plain"


def test_text_of_mixed_parts() -> None:
    content: list[ContentPart] = [
        TextPart(text="see "),
        ImagePart(url="u"),
        FilePart(url="u", filename="a.txt"),
        FilePart(url="u"),  # no filename
        TextPart(text=" end"),
    ]
    assert text_of(content) == "see [image][file:a.txt][file] end"
