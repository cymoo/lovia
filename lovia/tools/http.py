"""HTTP fetch tool — bounded, content-type-aware one-shot requests.

The response body is streamed with a hard byte cap (huge downloads stop
early), converted to model-friendly text by content type — JSON is
re-serialized compactly, HTML is reduced to its visible text via the
standard-library parser, other text passes through, binary returns
metadata only — and finally clipped to ``max_chars`` with an explicit
truncation notice.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from typing import Annotated
from urllib.parse import urlparse

import httpx
from pydantic import Field

from ..http_config import resolve_verify
from ..exceptions import ToolError
from .base import clip_text, tool

__all__ = ["http_fetch"]

# Hard cap on bytes read from the network, independent of max_chars.
_MAX_RESPONSE_BYTES = 1_000_000

_SKIPPED_HTML_ELEMENTS = frozenset(
    {"script", "style", "noscript", "template", "head", "svg"}
)
_BLOCK_HTML_ELEMENTS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "main",
        "br",
        "hr",
        "li",
        "ul",
        "ol",
        "table",
        "tr",
        "blockquote",
        "pre",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
)


class _TextExtractor(HTMLParser):
    """Collect the visible text of an HTML document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_HTML_ELEMENTS:
            self._skip_depth += 1
        elif tag in _BLOCK_HTML_ELEMENTS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_HTML_ELEMENTS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in _BLOCK_HTML_ELEMENTS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._chunks.append(data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self._chunks).splitlines())
        out: list[str] = []
        blank = False
        for line in lines:
            if line:
                out.append(line)
                blank = False
            elif not blank and out:
                out.append("")
                blank = True
        return "\n".join(out).strip()


def html_to_text(html: str) -> str:
    """Reduce an HTML document to its visible text (best effort)."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup — fall back to what was collected
        pass
    return parser.text()


def _render_body(content_type: str, body: bytes, charset: str) -> str:
    text = body.decode(charset, errors="replace")
    if "json" in content_type:
        try:
            return json.dumps(
                json.loads(text), ensure_ascii=False, separators=(",", ":")
            )
        except ValueError:
            return text
    if "html" in content_type:
        extracted = html_to_text(text)
        return extracted or text
    if content_type.startswith("text/") or content_type.endswith("+xml"):
        return text
    if not content_type or _looks_textual(body):
        return text
    return f"(binary content not shown: {content_type or 'unknown type'}, {len(body)} bytes)"


def _looks_textual(body: bytes) -> bool:
    return b"\0" not in body[:1024]


@tool(
    name="http_fetch",
    description=(
        "Fetch a URL over HTTP(S) and return the response as readable text.\n"
        "- JSON responses are compacted; HTML is reduced to its visible "
        "text; binary responses return metadata only.\n"
        "- The body is capped (1MB download limit, max_chars in the result); "
        "a truncation notice tells you when content was cut.\n"
        "- Use method/headers/body for REST API calls (body is sent as "
        "JSON).\n"
        "- One shot per call: no sessions, cookies, or JavaScript rendering."
    ),
)
async def http_fetch(
    url: Annotated[str, "Absolute http:// or https:// URL."],
    method: Annotated[str, "HTTP method (GET, POST, ...)."] = "GET",
    headers: Annotated[dict[str, str] | None, "Optional request headers."] = None,
    body: Annotated[object | None, "Optional JSON body for POST/PUT/PATCH."] = None,
    timeout: Annotated[
        float, Field(default=30.0, ge=1, le=120, description="Timeout in seconds.")
    ] = 30.0,
    max_chars: Annotated[
        int,
        Field(
            default=20_000, ge=100, le=200_000, description="Max characters returned."
        ),
    ] = 20_000,
) -> str:
    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ToolError(
            f"Unsupported URL scheme: {scheme or '(none)'!r}.",
            hint="Only http:// and https:// URLs can be fetched.",
        )

    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, verify=resolve_verify()
    ) as client:
        async with client.stream(
            method.upper(), url, headers=headers, json=body
        ) as resp:
            chunks: list[bytes] = []
            received = 0
            byte_capped = False
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                received += len(chunk)
                if received >= _MAX_RESPONSE_BYTES:
                    byte_capped = True
                    break
            raw = b"".join(chunks)[:_MAX_RESPONSE_BYTES]
            status = resp.status_code
            content_type = resp.headers.get("content-type", "")
            charset = resp.charset_encoding or "utf-8"

    media_type = content_type.split(";")[0].strip().lower()
    rendered = _render_body(media_type, raw, charset)
    rendered, char_capped = clip_text(rendered, max_chars, hint="")

    size_note = f"{len(rendered)} chars"
    if byte_capped:
        size_note += f" (download capped at {_MAX_RESPONSE_BYTES // 1_000_000}MB)"
    elif char_capped:
        size_note += " (truncated)"
    header = f"HTTP {status} · {media_type or 'unknown'} · {size_note}"
    return f"{header}\n\n{rendered}" if rendered else header
