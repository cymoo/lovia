"""PDF to text for ``web_fetch``, through the optional ``pypdf``.

Text only: layout, tables and images are lost, and a multi-column page may
come back in reading order or not. A scanned PDF has no text layer at all
and says so — reading it needs OCR, or a model that takes PDFs itself.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

PDF_MAGIC = b"%PDF-"
INSTALL_HINT = "pip install 'lovia[pdf]'"
# Text kept from one PDF. A 10 MB file can inflate to far more, and the model
# reads it 20k characters at a time anyway; a book's worth is plenty.
MAX_TEXT_CHARS = 2_000_000


@dataclass
class PdfText:
    text: str
    title: str | None = None


def pdf_text(body: bytes) -> PdfText:
    """The text of the PDF in ``body``, or a sentence on why there is none."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return PdfText(f"(PDF not read: text extraction needs pypdf — {INSTALL_HINT})")
    pages: list[str] = []
    budget = MAX_TEXT_CHARS
    try:
        reader = PdfReader(io.BytesIO(body))
        if reader.is_encrypted and not reader.decrypt(""):
            return PdfText("(PDF not read: it is password-protected)")
        count = len(reader.pages)
        for page in reader.pages:
            text = (page.extract_text() or "").strip()
            pages.append(text[:budget])
            budget -= len(text)
            if budget <= 0:
                break
    except Exception as exc:  # noqa: BLE001 — a bad file is an answer, not a crash
        # pypdf names a missing ``cryptography`` (AES) in its own message.
        return PdfText(f"(PDF not read: {type(exc).__name__}: {exc})")
    text = "\n\n".join(p for p in pages if p)
    if not text:
        return PdfText(
            f"(PDF of {count} page(s) with no text layer — likely scanned; "
            "reading it needs OCR or a model that takes PDFs)"
        )
    if budget < 0 or len(pages) < count:
        text += f"\n\n(… text cut short at {MAX_TEXT_CHARS:,} characters, page {len(pages)} of {count})"
    return PdfText(text, title=_title(reader))


def _title(reader: Any) -> str | None:
    # Best effort: a malformed /Info entry must not cost the text.
    try:
        title = reader.metadata.title if reader.metadata else None
    except Exception:  # noqa: BLE001 — no title is a fine answer
        return None
    return str(title).strip() or None if title else None
