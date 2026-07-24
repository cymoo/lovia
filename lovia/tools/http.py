"""HTTP transport plus the ``http_request`` tool.

``http_request`` is an honest HTTP client for the model: it sends the method,
headers and body it is given and reports the status, the response headers and
the body as text. It deliberately does **not** interpret HTML — reading a web
page is :mod:`lovia.tools.page`'s job (``read_page``).

The body is streamed with a hard byte cap (huge downloads stop early), decoded
by declared charset, and clipped to ``max_chars``. JSON is re-serialized
compactly; binary returns metadata only.

This module also holds the transport helpers ``read_page`` builds on:
:func:`fetch_raw` performs one capped request, :func:`decode_body` turns bytes
into text, and :func:`compact_json` shrinks JSON payloads.

No SSRF filtering is applied: the tool reaches whatever the host can reach,
including private/internal addresses (and redirects may lead there too). When
the model is exposed to untrusted input, gate the tool — either wholesale with
``dataclasses.replace(http_request, needs_approval=True)`` or writes-only with
``needs_approval=writes_need_approval`` — or isolate the network instead.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Annotated, Any
from urllib.parse import urlparse

import httpx
from pydantic import Field

from ..http_config import resolve_verify
from ..exceptions import ToolError
from ..run_context import RunContext
from .base import clip_text, tool

__all__ = [
    "MAX_RESPONSE_BYTES",
    "RawResponse",
    "compact_json",
    "decode_body",
    "default_user_agent",
    "fetch_raw",
    "http_request",
    "looks_textual",
    "validate_url",
    "writes_need_approval",
]

# Hard cap on bytes read from the network, independent of max_chars.
MAX_RESPONSE_BYTES = 1_000_000

_BINARY_TYPE_PREFIXES = ("image/", "video/", "audio/", "font/")

# Response headers worth their tokens minus the one that leaks credentials:
# ``set-cookie`` would put session tokens straight into the transcript.
_HIDDEN_RESPONSE_HEADERS = frozenset({"set-cookie"})
_MAX_HEADER_CHARS = 600

_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# ``<meta charset=x>`` / ``<meta http-equiv=content-type content="...charset=x">``
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w.:+-]+)""", re.IGNORECASE
)
_META_SNIFF_BYTES = 4096


@dataclass
class RawResponse:
    """One capped HTTP response, before any content interpretation."""

    url: str
    """Final URL after redirects — not necessarily the one requested."""
    status_code: int
    media_type: str
    """Content type with parameters stripped, lowercased (may be empty)."""
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    charset: str | None = None
    """Charset declared in the Content-Type header, if any."""
    size_capped: bool = False
    """The download hit the byte cap; the tail was never fetched."""


def default_user_agent() -> str:
    """The User-Agent lovia's fetching tools send.

    Sites routinely 403 an unidentified client, so this leads with the Mozilla
    token every server-side sniffer expects and then says who we actually are.
    The version is imported lazily: ``lovia/__init__`` imports this module.
    """
    global _user_agent
    if _user_agent is None:
        from .. import __version__

        _user_agent = (
            f"Mozilla/5.0 (compatible; lovia/{__version__}; "
            "+https://github.com/cymoo/lovia)"
        )
    return _user_agent


_user_agent: str | None = None


def validate_url(url: str) -> None:
    """Reject anything the tool cannot fetch, with a hint the model can act on."""
    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ToolError(
            f"Unsupported URL scheme: {scheme or '(none)'!r}.",
            hint="Only http:// and https:// URLs can be fetched.",
        )


async def fetch_raw(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    json_body: object | None = None,
    timeout: float = 30.0,
    max_bytes: int = MAX_RESPONSE_BYTES,
    user_agent: str | None = None,
) -> RawResponse:
    """Perform one redirect-following request, reading at most ``max_bytes``."""
    validate_url(url)
    request_headers = {"User-Agent": user_agent} if user_agent else {}
    # Caller-supplied headers win, so a model can override the default UA.
    request_headers.update(headers or {})

    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, verify=resolve_verify()
    ) as client:
        async with client.stream(
            method.upper(), url, headers=request_headers, json=json_body
        ) as resp:
            chunks: list[bytes] = []
            received = 0
            size_capped = False
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                received += len(chunk)
                if received >= max_bytes:
                    size_capped = True
                    break
            return RawResponse(
                url=str(resp.url),
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "")
                .split(";")[0]
                .strip()
                .lower(),
                headers=dict(resp.headers),
                body=b"".join(chunks)[:max_bytes],
                # Unlike ``resp.encoding`` this is None when the header carries
                # no charset, which is what lets decode_body fall back to
                # sniffing the document itself.
                charset=resp.charset_encoding,
                size_capped=size_capped,
            )


def decode_body(body: bytes, charset: str | None, *, sniff_meta: bool = False) -> str:
    """Decode ``body``, preferring the declared charset then a sniffed one.

    A server may declare a charset no codec implements (``charset=bogus``), so
    every candidate is tried in turn and utf-8 is the floor. With
    ``sniff_meta`` the document's own ``<meta charset>`` is consulted when the
    header is silent — without it a GBK page served headerless is mojibake.
    """
    sniffed = _sniff_meta_charset(body) if sniff_meta else None
    for candidate in (charset, sniffed, "utf-8"):
        if not candidate:
            continue
        try:
            return body.decode(candidate, errors="replace")
        except LookupError:  # unknown codec name — try the next candidate
            continue
    return body.decode("utf-8", errors="replace")


def _sniff_meta_charset(body: bytes) -> str | None:
    match = _META_CHARSET_RE.search(body[:_META_SNIFF_BYTES])
    return match.group(1).decode("ascii", errors="replace") if match else None


def looks_textual(body: bytes) -> bool:
    """Whether ``body`` is plausibly text — NUL bytes mean binary."""
    return b"\0" not in body[:1024]


def compact_json(text: str) -> str:
    """Re-serialize JSON without whitespace, or return ``text`` unchanged."""
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":"))
    except ValueError:
        return text


def writes_need_approval(args: dict[str, Any], ctx: RunContext[Any]) -> bool:
    """Approval predicate: let reads through, gate anything that may write.

    Not the default — approval fails closed, so a ``Runner.run`` caller with no
    approval handler would have every POST denied. Opt in explicitly::

        import dataclasses
        from lovia.tools import http_request, writes_need_approval

        gated = dataclasses.replace(
            http_request, needs_approval=writes_need_approval
        )
    """
    return str(args.get("method", "GET")).upper() not in _IDEMPOTENT_METHODS


def _render_headers(headers: dict[str, str]) -> str:
    shown = [
        f"{name}: {value}"
        for name, value in headers.items()
        if name.lower() not in _HIDDEN_RESPONSE_HEADERS
    ]
    text, _ = clip_text("\n".join(shown), _MAX_HEADER_CHARS, hint="")
    return text


def _render_payload(raw: RawResponse) -> str:
    """Turn a response body into the text the model sees."""
    if "json" in raw.media_type:
        return compact_json(decode_body(raw.body, raw.charset))
    if raw.media_type.startswith("text/") or raw.media_type.endswith("+xml"):
        return decode_body(raw.body, raw.charset)
    # Anything else: sniff, unless the type is declared binary. Showing
    # replacement-character soup to the model helps nobody.
    if not _is_binary_type(raw.media_type) and looks_textual(raw.body):
        return decode_body(raw.body, raw.charset)
    return (
        f"(binary content not shown: {raw.media_type or 'unknown type'}, "
        f"{len(raw.body)} bytes)"
    )


def _is_binary_type(media_type: str) -> bool:
    return (
        media_type.startswith(_BINARY_TYPE_PREFIXES)
        or media_type == "application/octet-stream"
    )


@tool(
    name="http_request",
    description=(
        "Make an HTTP(S) request and return the status, response headers and "
        "body.\n"
        "- Use this for REST APIs and non-HTML endpoints. To read a web page "
        "as text, use read_page instead — this tool returns HTML raw.\n"
        "- JSON responses are compacted; binary responses return metadata "
        "only.\n"
        "- body is sent as a JSON request body for POST/PUT/PATCH.\n"
        "- One shot per call: no sessions, cookies, or JavaScript rendering."
    ),
)
async def http_request(
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
    raw = await fetch_raw(
        url,
        method=method,
        headers=headers,
        json_body=body,
        timeout=timeout,
        user_agent=default_user_agent(),
    )
    rendered, char_capped = clip_text(_render_payload(raw), max_chars, hint="")

    size_note = f"{len(rendered)} chars"
    if raw.size_capped:
        size_note += f" (download capped at {MAX_RESPONSE_BYTES / 1e6:.1f}MB)"
    elif char_capped:
        size_note += " (truncated)"

    lines = [f"HTTP {raw.status_code} · {raw.media_type or 'unknown'} · {size_note}"]
    if raw.url != url:
        lines.append(f"URL: {raw.url}")
    if header_block := _render_headers(raw.headers):
        lines.append("")
        lines.append(header_block)
    if rendered:
        lines.append("")
        lines.append(rendered)
    return "\n".join(lines)
