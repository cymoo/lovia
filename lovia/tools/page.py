"""Read a web page as Markdown the model can act on.

``read_page`` fetches one URL and returns a :class:`Page`: the final URL, the
title, and the body as Markdown. Markdown rather than plain text because
headings, lists, links and images are exactly the structure a model needs —
``[docs](/guide)`` tells it where to go next, where flattened text tells it
nothing. Set ``images=True`` and the page's images come back as a deduplicated
list too, including the ones inline Markdown cannot show (``srcset``,
``<picture><source>``, ``og:image``).

The model-facing surface is three arguments — ``url``, ``images``, ``offset``.
Budgets (timeout, byte cap, character cap, cache TTL) are operator decisions,
so they live on the :func:`page_reader` factory instead of costing tokens in
the schema on every call.

Swapping the backend is how you get JavaScript rendering: implement
:class:`PageReader` over Jina Reader, Firecrawl, Playwright or anything else
and hand it to :func:`page_reader`. The bundled :class:`HttpReader` makes one
plain HTTP request and parses with the standard library — no extra dependency,
no browser, and therefore no client-side rendering::

    class JinaReader:  # ~15 lines; see docs/en/built-in-tools.md
        async def read(self, url: str, *, images: bool = False) -> Page: ...

    agent = Agent(name="x", tools=[page_reader(JinaReader())])
"""

from __future__ import annotations

import dataclasses
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Annotated, Any, Protocol
from urllib.parse import urljoin, urlparse

from pydantic import Field

from .base import Tool, default_result_renderer, tool
from .http import (
    MAX_RESPONSE_BYTES,
    RawResponse,
    compact_json,
    decode_body,
    default_user_agent,
    fetch_raw,
    render_payload,
    validate_url,
)

__all__ = [
    "HttpReader",
    "Page",
    "PageImage",
    "PageReader",
    "html_to_markdown",
    "page_from_response",
    "page_reader",
    "read_page",
]


@dataclass
class PageImage:
    """One image the page references."""

    url: str
    """Absolute URL, resolved against the page's base."""
    alt: str = ""


@dataclass
class Page:
    """A fetched page, converted for a model to read."""

    url: str
    """Final URL after redirects — not necessarily the one requested."""
    status_code: int
    media_type: str
    title: str | None = None
    text: str = ""
    """The body as Markdown (or compact JSON / plain text for other types)."""
    images: list[PageImage] = field(default_factory=list)
    """Every image found, deduplicated — empty unless ``images=True``."""
    size_capped: bool = False
    """The download hit the byte cap: the tail of the page was never fetched,
    so no ``offset`` will ever reach it."""
    next_offset: int | None = None
    """Pass back as ``offset`` to continue reading; ``None`` means the end."""


class PageReader(Protocol):
    """Minimum surface for a page-reading backend.

    Implementations must be safe to call concurrently. Returning the full
    body is expected — clipping to a character budget is the tool's job, not
    the backend's.
    """

    async def read(self, url: str, *, images: bool = False) -> Page: ...


_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

# Elements whose text is markup machinery, not content.
_SKIPPED_ELEMENTS = frozenset(
    {"script", "style", "noscript", "template", "svg", "canvas", "iframe"}
)
# Elements that end the current line of prose.
_BLOCK_ELEMENTS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "div",
        "dd",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "header",
        "main",
        "nav",
        "p",
        "section",
        "table",
    }
)
_HEADINGS = {f"h{level}": level for level in range(1, 7)}
_LIST_INDENT = "  "

# A page referencing more images than this is pathological; the ceiling keeps
# a runaway document from ballooning the result. The model-facing render caps
# far lower (_RENDERED_IMAGES) but reports the true total.
_MAX_IMAGES = 1_000
_RENDERED_IMAGES = 50
_MAX_ALT_CHARS = 200

# An error page's HTML template is never worth a full character budget.
_ERROR_PAGE_CHARS = 500
# When clipping, back up to a line break within this much of the limit so the
# cut lands between lines instead of mid-word.
_CLIP_BACKTRACK = 200

_SRCSET_SPLIT_RE = re.compile(r"\s*,\s*")
_INTERIOR_SPACE_RE = re.compile(r"[ \t]+")


class _MarkdownExtractor(HTMLParser):
    """Convert an HTML document to Markdown, collecting title and images.

    Whitespace is normalized as text arrives rather than afterwards, so the
    only indentation left in the output is the list nesting this class emits
    itself — HTML source indentation never reaches the result.
    """

    def __init__(self, base_url: str = "", *, collect_images: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self._base = base_url
        self._collect_images = collect_images
        self._out: list[str] = []
        self._skip_depth = 0
        self._pre_depth = 0
        self._in_title = False
        self._title: list[str] = []
        # (start index in _out, closing renderer) for inline wrappers.
        self._wraps: list[tuple[int, str, str]] = []
        # [ordered, next number] per open list, innermost last.
        self._lists: list[list[Any]] = []
        self._images: dict[str, PageImage] = {}
        self._row_cells = 0
        self._row_is_header = False

    # ---- output primitives ----

    def _emit(self, chunk: str) -> None:
        self._out.append(chunk)

    def _at_line_start(self) -> bool:
        return not self._out or self._out[-1].endswith("\n")

    def _break(self) -> None:
        if not self._at_line_start():
            self._emit("\n")

    def _blank_line(self) -> None:
        self._break()
        self._emit("\n")

    # ---- parser callbacks ----

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_depth:
            if tag in _SKIPPED_ELEMENTS:
                self._skip_depth += 1
            return
        if tag in _SKIPPED_ELEMENTS:
            self._skip_depth += 1
            return

        values = {name: (value or "") for name, value in attrs}
        if tag == "base":
            # <base> lives in <head>, which the parser always reaches before
            # any body content, so resolving URLs inline stays correct.
            if href := values.get("href"):
                self._base = urljoin(self._base, href)
            return
        if tag == "meta":
            self._handle_meta(values)
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "img":
            self._handle_img(values)
            return
        if tag == "source":
            self._record_image(self._pick_srcset(values.get("srcset", "")), "")
            return
        if tag == "br":
            self._break()
            return
        if tag == "hr":
            self._blank_line()
            self._emit("---\n")
            return
        if tag == "pre":
            self._pre_depth += 1
            if self._pre_depth == 1:
                self._blank_line()
                self._emit("```\n")
            return
        if level := _HEADINGS.get(tag):
            self._blank_line()
            self._emit("#" * level + " ")
            return
        if tag in ("ul", "ol"):
            self._blank_line()
            self._lists.append([tag == "ol", 1])
            return
        if tag == "li":
            self._start_list_item()
            return
        if tag == "a":
            self._wraps.append((len(self._out), values.get("href", ""), "a"))
            return
        if tag == "code" and not self._pre_depth:
            self._wraps.append((len(self._out), "", "code"))
            return
        if tag == "tr":
            self._break()
            self._row_cells = 0
            self._row_is_header = False
            return
        if tag in ("td", "th"):
            self._emit("| " if self._at_line_start() else " | ")
            self._row_cells += 1
            self._row_is_header = self._row_is_header or tag == "th"
            return
        if tag in _BLOCK_ELEMENTS:
            self._blank_line()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_ELEMENTS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
            return
        if tag == "pre":
            if self._pre_depth == 1:
                self._break()
                self._emit("```\n")
            self._pre_depth = max(0, self._pre_depth - 1)
            return
        if tag in ("a", "code"):
            self._close_wrap(tag)
            return
        if tag in ("ul", "ol"):
            if self._lists:
                self._lists.pop()
            self._blank_line()
            return
        if tag == "li":
            self._break()
            return
        if tag == "tr":
            self._close_table_row()
            return
        if tag in _HEADINGS or tag in _BLOCK_ELEMENTS:
            self._blank_line()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title.append(data)
            return
        if self._pre_depth:
            self._emit(data)
            return
        if not data.strip():
            # Whitespace between tags still separates words.
            if not self._at_line_start() and not self._out[-1].endswith(" "):
                self._emit(" ")
            return
        lead = " " if data[:1].isspace() and not self._at_line_start() else ""
        trail = " " if data[-1:].isspace() else ""
        self._emit(f"{lead}{' '.join(data.split())}{trail}")

    # ---- element helpers ----

    def _start_list_item(self) -> None:
        self._break()
        if not self._lists:  # <li> outside any list — treat it as a bullet
            self._emit("- ")
            return
        ordered, number = self._lists[-1]
        self._emit(_LIST_INDENT * (len(self._lists) - 1))
        if ordered:
            self._emit(f"{number}. ")
            self._lists[-1][1] = number + 1
        else:
            self._emit("- ")

    def _close_table_row(self) -> None:
        if not self._row_cells:
            return
        self._emit(" |\n")
        if self._row_is_header:
            # Markdown needs the delimiter row or the table reads as prose.
            self._emit("|" + " --- |" * self._row_cells + "\n")
        self._row_cells = 0

    def _close_wrap(self, tag: str) -> None:
        for index in range(len(self._wraps) - 1, -1, -1):
            if self._wraps[index][2] == tag:
                start, href, _ = self._wraps.pop(index)
                break
        else:
            return  # stray close tag
        inner = " ".join("".join(self._out[start:]).split())
        del self._out[start:]
        if not inner:
            return
        if tag == "code":
            self._emit(f"`{inner}`")
        elif href and (url := self._absolute(href)):
            self._emit(f"[{inner}]({url})")
        else:
            self._emit(inner)

    def _handle_meta(self, values: dict[str, str]) -> None:
        prop = (values.get("property") or values.get("name") or "").lower()
        if prop in ("og:image", "og:image:url", "twitter:image"):
            self._record_image(values.get("content", ""), "")

    def _handle_img(self, values: dict[str, str]) -> None:
        src = values.get("src") or self._pick_srcset(values.get("srcset", ""))
        alt = " ".join(values.get("alt", "").split())[:_MAX_ALT_CHARS]
        url = self._record_image(src, alt)
        if url:
            self._emit(f"![{alt}]({url})")

    def _pick_srcset(self, srcset: str) -> str:
        """The largest candidate in a ``srcset``, by width or pixel density."""
        best, best_score = "", -1.0
        for candidate in _SRCSET_SPLIT_RE.split(srcset.strip()):
            if not candidate:
                continue
            parts = candidate.split()
            url, descriptor = parts[0], parts[1] if len(parts) > 1 else ""
            try:
                score = float(descriptor[:-1]) if descriptor[-1:] in ("w", "x") else 1.0
            except ValueError:
                score = 1.0
            if score > best_score:
                best, best_score = url, score
        return best

    def _record_image(self, src: str, alt: str) -> str:
        """Absolutize and remember ``src``; returns the URL, or "" if unusable."""
        url = self._absolute(src)
        if not url:
            return ""
        if self._collect_images and len(self._images) < _MAX_IMAGES:
            self._images.setdefault(url, PageImage(url=url, alt=alt))
        return url

    def _absolute(self, url: str) -> str:
        """Resolve ``url`` against the base, dropping what a model cannot use."""
        url = url.strip()
        # A base64 data: URI can be 100KB inside a single attribute, and the
        # model can do nothing with it either way.
        if not url or url.startswith(("data:", "javascript:", "#")):
            return ""
        try:
            resolved = urljoin(self._base, url)
        except ValueError:
            return ""
        return resolved if urlparse(resolved).scheme in ("http", "https") else ""

    # ---- results ----

    @property
    def title(self) -> str | None:
        title = " ".join("".join(self._title).split())
        return title or None

    @property
    def images(self) -> list[PageImage]:
        return list(self._images.values())

    @property
    def text(self) -> str:
        """The collected Markdown, with blank-line runs and spaces collapsed."""
        lines: list[str] = []
        in_fence = False
        for line in "".join(self._out).splitlines():
            if line.strip().startswith("```"):
                in_fence = not in_fence
                lines.append("```")
                continue
            if in_fence:
                lines.append(line.rstrip())
                continue
            indent = len(line) - len(line.lstrip(" "))
            body = _INTERIOR_SPACE_RE.sub(" ", line[indent:]).strip()
            if body:
                lines.append(" " * indent + body)
            elif lines and lines[-1]:
                lines.append("")
        return "\n".join(lines).strip()


def html_to_markdown(html: str, *, base_url: str = "") -> str:
    """Convert an HTML document to Markdown (best effort).

    ``base_url`` resolves relative links and images; without it, relative URLs
    are dropped rather than emitted in a form nothing can fetch.
    """
    return _parse(html, base_url=base_url, collect_images=False).text


def _parse(html: str, *, base_url: str, collect_images: bool) -> _MarkdownExtractor:
    parser = _MarkdownExtractor(base_url, collect_images=collect_images)
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup — keep whatever was collected
        pass
    return parser


def _looks_like_html(body: bytes) -> bool:
    head = body[:512].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html")) or b"<html" in head


def page_from_response(raw: RawResponse, *, collect_images: bool = False) -> Page:
    """Build a :class:`Page` from a raw response, by content type."""
    page = Page(
        url=raw.url,
        status_code=raw.status_code,
        media_type=raw.media_type,
        size_capped=raw.size_capped,
    )
    if "html" in raw.media_type or (not raw.media_type and _looks_like_html(raw.body)):
        # Sniff <meta charset>: a headerless GBK page is mojibake without it.
        html = decode_body(raw.body, raw.charset, sniff_meta=True)
        doc = _parse(html, base_url=raw.url, collect_images=collect_images)
        page.title = doc.title
        # An extraction that came back empty (framework shell, exotic markup)
        # is less useful than the markup itself.
        page.text = doc.text or html
        page.images = doc.images
        return page
    if "json" in raw.media_type:
        page.text = compact_json(decode_body(raw.body, raw.charset))
        return page
    page.text = render_payload(raw)
    return page


class HttpReader:
    """The bundled backend: one plain HTTP request, standard-library parsing.

    No browser and no JavaScript, so a page that renders client-side comes
    back nearly empty — swap in a :class:`PageReader` that drives a real
    browser when that matters.

    Responses are cached by URL for ``cache_ttl`` seconds. That is mostly
    correctness rather than speed: continuing a long page with ``offset``
    would otherwise re-download it and could splice together two different
    versions. The cache holds at most ``cache_size`` entries so a long-running
    agent cannot accumulate page bodies indefinitely.
    """

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_bytes: int = MAX_RESPONSE_BYTES,
        user_agent: str | None = None,
        cache_ttl: float = 300.0,
        cache_size: int = 32,
    ) -> None:
        self._timeout = timeout
        self._max_bytes = max_bytes
        # Resolved per request, not here: the module-level ``read_page`` builds
        # an HttpReader while ``lovia/__init__`` is still executing, so
        # ``lovia.__version__`` does not exist yet.
        self._user_agent = user_agent
        self._cache_ttl = cache_ttl
        self._cache_size = max(1, cache_size)
        self._cache: OrderedDict[str, tuple[float, RawResponse]] = OrderedDict()

    async def read(self, url: str, *, images: bool = False) -> Page:
        return page_from_response(await self._fetch(url), collect_images=images)

    async def _fetch(self, url: str) -> RawResponse:
        if (cached := self._cache_get(url)) is not None:
            return cached
        raw = await fetch_raw(
            url,
            headers={"Accept": _ACCEPT},
            timeout=self._timeout,
            max_bytes=self._max_bytes,
            user_agent=self._user_agent or default_user_agent(),
        )
        self._cache_put(url, raw)
        return raw

    def _cache_get(self, url: str) -> RawResponse | None:
        if self._cache_ttl <= 0:
            return None
        entry = self._cache.get(url)
        if entry is None:
            return None
        stored_at, raw = entry
        if time.monotonic() - stored_at > self._cache_ttl:
            del self._cache[url]
            return None
        self._cache.move_to_end(url)
        return raw

    def _cache_put(self, url: str, raw: RawResponse) -> None:
        if self._cache_ttl <= 0:
            return
        self._cache[url] = (time.monotonic(), raw)
        self._cache.move_to_end(url)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)


def _clip(text: str, offset: int, limit: int) -> tuple[str, int | None]:
    """Return ``text`` from ``offset`` within ``limit`` chars, plus the next offset."""
    body = text[offset:] if offset else text
    if len(body) <= limit:
        return body, None
    # Prefer cutting between lines so the model never resumes mid-word.
    cut = body.rfind("\n", max(0, limit - _CLIP_BACKTRACK), limit)
    if cut <= 0:
        cut = limit
    return body[:cut], offset + cut


def _render_page(result: Any, ctx: Any) -> str:
    """The text both the model and the web UI see for a ``read_page`` result."""
    if not isinstance(result, Page):
        return result if isinstance(result, str) else default_result_renderer(result)

    lines: list[str] = []
    if result.title:
        lines.append(f"Title: {result.title}")
    lines.append(f"URL: {result.url}")
    status = f"HTTP {result.status_code} · {result.media_type or 'unknown'}"
    if result.size_capped:
        status += (
            f" · download capped at {MAX_RESPONSE_BYTES / 1e6:.1f}MB "
            "(the rest of the page was never fetched)"
        )
    lines.append(status)

    if result.text:
        lines += ["", result.text]
    if result.next_offset is not None:
        lines += ["", f"[... truncated. Continue with offset={result.next_offset}.]"]
    if result.images:
        shown = result.images[:_RENDERED_IMAGES]
        total = len(result.images)
        count = f"{len(shown)} of {total}" if total > len(shown) else str(total)
        lines += ["", f"Images ({count}):"]
        lines += [
            f"{i}. {image.url}" + (f" — {image.alt}" if image.alt else "")
            for i, image in enumerate(shown, 1)
        ]
    return "\n".join(lines)


def page_reader(
    impl: PageReader | None = None,
    *,
    name: str = "read_page",
    max_chars: int = 20_000,
) -> Tool:
    """Build a ``read_page`` :class:`Tool`, optionally over a custom backend.

    ``impl`` defaults to :class:`HttpReader`; pass your own to get JavaScript
    rendering or a hosted extraction service. ``max_chars`` bounds the text
    handed to the model per call — the rest stays reachable via ``offset``.
    """
    reader: PageReader = impl if impl is not None else HttpReader()

    # Reading crosses the open internet, where a dropped connection is a
    # normal event. Retrying is safe here in a way it never was for the old
    # combined tool: this issues GETs only.
    @tool(
        name=name,
        result_renderer=_render_page,
        retries=2,
        description=(
            "Read a web page and return its content as Markdown.\n"
            "- Headings, lists, links and images survive the conversion, so "
            "[text](url) in the result holds real URLs you can read next.\n"
            "- images=true also returns a deduplicated list of every image "
            "the page references, including ones inline Markdown cannot show "
            "(srcset, <picture>, og:image).\n"
            "- Long pages are cut short; the notice tells you which offset to "
            "continue from.\n"
            "- No JavaScript is executed, so a page that builds its content "
            "in the browser may come back nearly empty.\n"
            "- For REST APIs and non-HTML endpoints, use http_request."
        ),
    )
    async def _read(
        url: Annotated[str, "Absolute http:// or https:// URL."],
        images: Annotated[bool, "Also list every image the page references."] = False,
        offset: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Resume reading at this character offset.",
            ),
        ] = 0,
    ) -> Page:
        validate_url(url)
        page = await reader.read(url, images=images)
        # An error page's boilerplate is never worth a full budget.
        limit = (
            max_chars
            if 200 <= page.status_code < 300
            else min(max_chars, _ERROR_PAGE_CHARS)
        )
        text, next_offset = _clip(page.text, offset, limit)
        # replace() rather than mutate: a custom reader may hand back a page
        # it caches and reuses.
        return dataclasses.replace(page, text=text, next_offset=next_offset)

    return _read


read_page = page_reader()
"""Ready-to-use ``read_page`` tool over the default :class:`HttpReader`."""
