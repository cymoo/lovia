"""The HTTP transport ``web_fetch`` builds on.

:func:`fetch_raw` performs one capped GET — the body is streamed with a hard
byte cap, so huge downloads stop early — :func:`decode_body` turns bytes into
text by declared (or sniffed) charset, and :func:`render_payload` turns a
non-HTML body into the text the model sees: compact JSON, text as-is, binary
as metadata only.

No SSRF filtering is applied: a fetch reaches whatever the host can reach,
including private/internal addresses (and redirects may lead there too).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from ..http_config import resolve_verify
from ..exceptions import ToolError

__all__ = [
    "MAX_RESPONSE_BYTES",
    "RawResponse",
    "compact_json",
    "decode_body",
    "default_user_agent",
    "display_media_type",
    "fetch_raw",
    "looks_textual",
    "render_payload",
    "validate_url",
]

# Hard cap on bytes read from the network, independent of max_chars.
MAX_RESPONSE_BYTES = 1_000_000

_BINARY_TYPE_PREFIXES = ("image/", "video/", "audio/", "font/")

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
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    max_bytes: int = MAX_RESPONSE_BYTES,
    user_agent: str | None = None,
) -> RawResponse:
    """Perform one redirect-following GET, reading at most ``max_bytes``."""
    validate_url(url)
    request_headers = dict(headers or {})
    if user_agent:
        request_headers["User-Agent"] = user_agent

    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, verify=resolve_verify()
    ) as client:
        async with client.stream("GET", url, headers=request_headers) as resp:
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


def render_payload(raw: RawResponse) -> str:
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


# RFC 6838 type/subtype names. The status line sits outside the fence, yet the
# media type comes from the server's Content-Type header — so anything that
# isn't a well-formed type is shown as "unknown" rather than echoed.
_MEDIA_TYPE = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}",
    re.IGNORECASE,
)


def display_media_type(media_type: str) -> str:
    """``media_type`` when it is a well-formed MIME type, else ``"unknown"``."""
    return media_type if _MEDIA_TYPE.fullmatch(media_type) else "unknown"
