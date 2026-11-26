"""PDF to text for ``web_fetch``, through the optional ``pypdf``.

Text only: layout, tables and images are lost, and a multi-column page may
come back in reading order or not. A scanned PDF has no text layer at all
and says so — reading it needs OCR, or a model that takes PDFs itself.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

PDF_MAGIC = b"%PDF-"
INSTALL_HINT = "pip install 'lovia[pdf]'"


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
    try:
        reader = PdfReader(io.BytesIO(body))
        if reader.is_encrypted and not reader.decrypt(""):
            return PdfText("(PDF not read: it is password-protected)")
        pages = [page.extract_text() or "" for page in reader.pages]
        title = reader.metadata.title if reader.metadata else None
    except Exception as exc:  # noqa: BLE001 — a bad file is an answer, not a crash
        # pypdf names a missing ``cryptography`` (AES) in its own message.
        return PdfText(f"(PDF not read: {type(exc).__name__}: {exc})")
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    if not text:
        return PdfText(
            f"(PDF of {len(pages)} page(s) with no text layer — likely scanned; "
            "reading it needs OCR or a model that takes PDFs)"
        )
    return PdfText(text, title=str(title).strip() or None if title else None)
