"""Tests for ``lovia.tools.page`` — the web_fetch tool and its HTML conversion."""

from __future__ import annotations

from typing import Any, Callable

import httpx
import pytest

from lovia.exceptions import ToolError
from lovia.run_context import RunContext
from lovia.tools.base import Tool
from lovia.tools.page import (
    HttpReader,
    Page,
    PageImage,
    html_to_markdown,
    page_from_response,
    web_fetch,
)
from lovia.tools.base import run_tool
from lovia.tools.page import _render_page


def _ctx() -> RunContext:
    return RunContext(deps=None, entries=[], agent=None)  # type: ignore[arg-type]


@pytest.fixture
def page_tool() -> Tool:
    """A tool with a cache-free reader, so mocked responses are never stale."""
    return web_fetch(HttpReader(cache_ttl=0))


def _mock_http(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    real_client = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr("lovia.tools._http.httpx.AsyncClient", factory)


def _html_page(monkeypatch: pytest.MonkeyPatch, html: str, **kwargs: Any) -> None:
    _mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            kwargs.pop("status", 200),
            text=html,
            headers={"content-type": "text/html; charset=utf-8"},
            **kwargs,
        ),
    )


# ------------------------------------------------------- html_to_markdown


def test_markdown_keeps_the_structure_plain_text_would_destroy() -> None:
    html = (
        "<h2>Guide</h2><p>See <a href='/docs'>the docs</a>.</p>"
        "<ul><li>one</li><li>two</li></ul>"
        "<ol><li>first</li><li>second</li></ol>"
    )
    out = html_to_markdown(html, base_url="https://example.com/x/")
    assert "## Guide" in out
    assert "[the docs](https://example.com/docs)" in out
    assert "- one" in out and "- two" in out
    assert "1. first" in out and "2. second" in out


def test_markdown_drops_markup_machinery() -> None:
    html = (
        "<head><style>p{color:red}</style><script>evil()</script></head>"
        "<body><noscript>enable js</noscript><svg><text>vector</text></svg>"
        "<p>real content</p></body>"
    )
    out = html_to_markdown(html)
    assert out == "real content"


def test_markdown_preserves_code_verbatim() -> None:
    html = "<pre><code>def f():\n    return 1</code></pre><p>and <code>x=1</code></p>"
    out = html_to_markdown(html)
    assert "```\ndef f():\n    return 1\n```" in out
    # Inline code is backticked, and must not be fenced inside a <pre>.
    assert "`x=1`" in out


def test_markdown_builds_real_tables() -> None:
    html = "<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>"
    assert html_to_markdown(html) == "| a | b |\n| --- | --- |\n| 1 | 2 |"
    # No header cells means no delimiter row — the rows still read as a table.
    assert html_to_markdown("<table><tr><td>x</td></tr></table>") == "| x |"


def test_markdown_indents_nested_lists() -> None:
    out = html_to_markdown("<ul><li>outer<ul><li>inner</li></ul></li></ul>")
    assert "- outer" in out
    assert "  - inner" in out


def test_markdown_drops_bullets_that_never_got_content() -> None:
    # A <li> holding only a nested list (very common in a table of contents)
    # would otherwise leave a lone "-" on its own line.
    assert html_to_markdown("<ul><li><ul><li>x</li></ul></li></ul>") == "- x"
    assert html_to_markdown("<ul><li>a</li><li></li></ul>") == "- a"
    # But content that merely looks like a marker is content: deciding at emit
    # time, not by matching the finished text, is what keeps these apart.
    assert html_to_markdown("<p>-</p><p>next</p>") == "-\n\nnext"
    assert html_to_markdown("<ul><li>-</li><li>b</li></ul>") == "- -\n- b"
    # An image is content even though it contributes no words.
    assert (
        html_to_markdown(
            "<ul><li><img src='i.png' alt='I'></li></ul>", base_url="https://e.com/"
        )
        == "- ![I](https://e.com/i.png)"
    )


def test_markdown_escapes_text_interpolated_into_syntax() -> None:
    # A bracket in link text or alt text does not merely look wrong: it ends
    # the construct early and swallows the rest.
    base = "https://e.com/"
    assert (
        html_to_markdown("<a href='/x'>see [1] here</a>", base_url=base)
        == r"[see \[1\] here](https://e.com/x)"
    )
    assert (
        html_to_markdown("<img src='i.png' alt='Figure [2]'>", base_url=base)
        == r"![Figure \[2\]](https://e.com/i.png)"
    )
    # A pipe would otherwise add a phantom column.
    assert html_to_markdown("<table><tr><td>a|b</td></tr></table>") == r"| a\|b |"
    # Prose is left alone — "[1]" with no destination after it is not a link,
    # and escaping every page would just cost tokens.
    assert html_to_markdown("<p>see [1] in prose</p>") == "see [1] in prose"
    # Nor is a link that will be dropped anyway worth escaping: Wikipedia
    # footnotes are [9] behind a fragment href, and there are hundreds a page.
    assert (
        html_to_markdown('<sup><a href="#cite-9">[9]</a></sup> text', base_url=base)
        == "[9] text"
    )


def test_markdown_escaping_does_not_break_nested_constructs() -> None:
    # The escape happens as text arrives, so Markdown this converter emitted
    # itself — here an image inside a link — passes through untouched.
    assert (
        html_to_markdown(
            "<a href='/p'><img src='i.png' alt='I'></a>", base_url="https://e.com/"
        )
        == "[![I](https://e.com/i.png)](https://e.com/p)"
    )


def test_markdown_url_with_parentheses_stays_one_link() -> None:
    # Wikipedia-style disambiguation URLs are everywhere.
    assert (
        html_to_markdown("<a href='/wiki/Foo_(bar)'>Foo</a>", base_url="https://e.com/")
        == "[Foo](<https://e.com/wiki/Foo_(bar)>)"
    )


def test_markdown_code_span_fence_grows_past_its_content() -> None:
    assert html_to_markdown("<p><code>a `b` c</code></p>") == "``a `b` c``"
    assert html_to_markdown("<p><code>`x`</code></p>") == "`` `x` ``"


def test_markdown_code_inside_link_is_not_escaped() -> None:
    # A code span binds tighter than link brackets, so its content needs no
    # escaping — a backslash there would show up literally in the rendering.
    assert (
        html_to_markdown(
            "<a href='/d'><code>a[0]</code> docs</a>", base_url="https://e.com/"
        )
        == "[`a[0]` docs](https://e.com/d)"
    )


def test_markdown_survives_malformed_markup() -> None:
    assert html_to_markdown("<p>ok<div") == "ok"
    assert html_to_markdown("plain text") == "plain text"
    # Unclosed <li>, and stray closing tags with nothing open.
    assert html_to_markdown("<ul><li>a<li>b</ul>") == "- a\n- b"
    assert "keep" in html_to_markdown("keep</a></code></ul>")


def test_markdown_normalizes_source_indentation() -> None:
    # HTML source indentation must never survive as Markdown indentation:
    # four leading spaces would turn a paragraph into a code block.
    html = "<body>\n    <p>\n        indented in source\n    </p>\n</body>"
    assert html_to_markdown(html) == "indented in source"


def test_markdown_drops_urls_a_model_cannot_use() -> None:
    html = (
        "<a href='mailto:x@y.z'>mail</a> <a href='javascript:x()'>js</a> "
        "<a href='#top'>anchor</a>"
    )
    out = html_to_markdown(html, base_url="https://example.com")
    # The link text survives; the unusable target does not become a link.
    assert "mail" in out and "js" in out and "anchor" in out
    assert "mailto:" not in out and "javascript:" not in out and "](#" not in out


def test_markdown_relative_urls_need_a_base() -> None:
    # Without a base, a relative URL would be emitted in a form nothing can
    # fetch — better to keep the text and drop the link.
    assert html_to_markdown("<a href='/g'>g</a>") == "g"


# ------------------------------------------------------------- web_fetch


@pytest.mark.asyncio
async def test_web_fetch_reports_title_and_final_url(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "https://example.com/new"})
        return httpx.Response(
            200,
            text="<html><head><title>  My   Page </title></head><body>hi</body></html>",
            headers={"content-type": "text/html"},
        )

    _mock_http(monkeypatch, handler)
    page = await page_tool.invoke({"url": "https://example.com/old"}, _ctx())
    assert isinstance(page, Page)
    # <title> used to be lost: <head> was skipped wholesale.
    assert page.title == "My Page"
    assert page.url == "https://example.com/new"
    assert page.text == "hi"


@pytest.mark.asyncio
async def test_web_fetch_resolves_relative_urls_against_the_final_url(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    _html_page(monkeypatch, "<a href='sibling'>s</a><img src='/i.png' alt='i'>")
    page = await page_tool.invoke({"url": "https://example.com/dir/doc"}, _ctx())
    assert "[s](https://example.com/dir/sibling)" in page.text
    assert "![i](https://example.com/i.png)" in page.text


@pytest.mark.asyncio
async def test_web_fetch_honors_base_href(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    _html_page(
        monkeypatch,
        "<head><base href='https://cdn.example.com/assets/'></head>"
        "<body><img src='logo.png' alt='L'></body>",
    )
    page = await page_tool.invoke({"url": "https://example.com/p"}, _ctx())
    assert "https://cdn.example.com/assets/logo.png" in page.text


@pytest.mark.asyncio
async def test_web_fetch_images_are_opt_in(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    _html_page(monkeypatch, "<img src='a.png' alt='A'>")
    default = await page_tool.invoke({"url": "https://example.com/"}, _ctx())
    assert default.images == []
    # The inline Markdown still shows it; the structured list is what's opt-in.
    assert "![A](https://example.com/a.png)" in default.text

    asked = await page_tool.invoke(
        {"url": "https://example.com/", "images": True}, _ctx()
    )
    assert asked.images == [PageImage(url="https://example.com/a.png", alt="A")]


@pytest.mark.asyncio
async def test_web_fetch_collects_images_markdown_cannot_show(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    _html_page(
        monkeypatch,
        "<head><meta property='og:image' content='social.png'></head><body>"
        "<img srcset='s-320.png 320w, s-1024.png 1024w' alt='R'>"
        "<picture><source srcset='big.webp 2x'><img src='fb.jpg' alt='P'></picture>"
        "<img src='dup.png' alt='one'><img src='dup.png' alt='again'>"
        "<img src='data:image/png;base64,AAAA' alt='inline'>"
        "</body>",
    )
    page = await page_tool.invoke(
        {"url": "https://example.com/p", "images": True}, _ctx()
    )
    urls = [image.url for image in page.images]
    assert "https://example.com/social.png" in urls  # og:image
    assert "https://example.com/s-1024.png" in urls  # largest srcset candidate
    assert "https://example.com/s-320.png" not in urls
    assert "https://example.com/big.webp" in urls  # <picture><source>
    assert "https://example.com/fb.jpg" in urls
    # Deduplicated by absolute URL, first alt wins.
    assert urls.count("https://example.com/dup.png") == 1
    assert next(i.alt for i in page.images if i.url.endswith("dup.png")) == "one"
    # A base64 data: URI is 100KB of budget the model can do nothing with.
    assert not any(url.startswith("data:") for url in urls)
    assert "data:" not in page.text


@pytest.mark.asyncio
async def test_web_fetch_continues_from_next_offset(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    body = "".join(f"<p>line {i:04d}</p>" for i in range(400))
    _html_page(monkeypatch, body)
    tool = web_fetch(max_chars=500)

    first = await tool.invoke({"url": "https://example.com/long"}, _ctx())
    assert first.next_offset is not None
    assert len(first.text) <= 500
    # The cut lands between lines, so no entry is split mid-word.
    assert first.text.endswith("]") or not first.text.endswith(" ")
    assert "line 0000" in first.text

    second = await tool.invoke(
        {"url": "https://example.com/long", "offset": first.next_offset}, _ctx()
    )
    assert second.text
    assert not second.text.startswith(first.text[-20:])
    assert "line 0000" not in second.text


@pytest.mark.asyncio
async def test_web_fetch_offset_past_the_end_is_empty_not_an_error(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    _html_page(monkeypatch, "<p>short</p>")
    page = await page_tool.invoke(
        {"url": "https://example.com/", "offset": 10_000}, _ctx()
    )
    assert page.text == ""
    assert page.next_offset is None


@pytest.mark.asyncio
async def test_web_fetch_caps_error_pages_hard(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    # A 500's HTML template is never worth a full character budget.
    _html_page(monkeypatch, "<p>" + "sorry " * 500 + "</p>", status=500)
    page = await page_tool.invoke({"url": "https://example.com/boom"}, _ctx())
    assert page.status_code == 500
    assert len(page.text) <= 500


@pytest.mark.asyncio
async def test_web_fetch_surfaces_a_capped_download(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    _html_page(monkeypatch, "<p>" + "x" * 5_000 + "</p>")
    tool = web_fetch(HttpReader(max_bytes=1_000, cache_ttl=0))
    page = await tool.invoke({"url": "https://example.com/huge"}, _ctx())
    # size_capped says the tail was never fetched, so no offset will reach it —
    # a different situation from "clipped, ask for more".
    assert page.size_capped is True
    assert "download capped" in _render_page(page, None)


@pytest.mark.asyncio
async def test_web_fetch_sniffs_meta_charset(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    # A GBK page whose header omits the charset used to arrive as mojibake.
    body = "<html><head><meta charset='gbk'></head><body><p>中文内容</p></body></html>"
    _mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=body.encode("gbk"), headers={"content-type": "text/html"}
        ),
    )
    page = await page_tool.invoke({"url": "https://example.com/"}, _ctx())
    assert "中文内容" in page.text


@pytest.mark.asyncio
async def test_web_fetch_handles_non_html_content(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    responses = iter(
        [
            httpx.Response(200, json={"a": [1, 2]}),
            httpx.Response(
                200,
                content=b"\x00\x01binary",
                headers={"content-type": "application/octet-stream"},
            ),
            httpx.Response(200, content=b"<html><p>sniffed</p></html>"),
            httpx.Response(
                200,
                text="<rss><item>hi</item></rss>",
                headers={"content-type": "application/rss+xml"},
            ),
        ]
    )
    _mock_http(monkeypatch, lambda request: next(responses))

    json_page = await page_tool.invoke({"url": "https://example.com/a"}, _ctx())
    assert json_page.text == '{"a":[1,2]}'
    binary = await page_tool.invoke({"url": "https://example.com/b"}, _ctx())
    assert "binary content not shown" in binary.text
    # No content-type at all: sniff the markup rather than dumping it raw.
    sniffed = await page_tool.invoke({"url": "https://example.com/c"}, _ctx())
    assert sniffed.text == "sniffed"
    # XML is not HTML: it passes through as text.
    feed = await page_tool.invoke({"url": "https://example.com/d"}, _ctx())
    assert feed.text == "<rss><item>hi</item></rss>"


@pytest.mark.asyncio
async def test_web_fetch_falls_back_to_markup_when_extraction_is_empty(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    # An app shell has no text at all; the markup is more useful than nothing.
    _html_page(monkeypatch, "<html><body><div id='root'></div></body></html>")
    page = await page_tool.invoke({"url": "https://example.com/spa"}, _ctx())
    assert "id='root'" in page.text or 'id="root"' in page.text


@pytest.mark.asyncio
async def test_web_fetch_reads_the_main_content_a_page_marks(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    body = "Body text of the article. " * 20
    pages = {
        # Navigation outside <main> is left out; the title still comes from <head>.
        "/one-main": f"<head><title>T</title></head><nav><a href='/x'>Menu</a></nav>"
        f"<main><h1>Topic</h1><p>{body}</p></main><footer>Footer</footer>",
        # No <main>: the one <article> marks it.
        "/one-article": f"<nav>Menu</nav><article><p>{body}</p></article>",
        # Two of them: no landmark to trust, the whole page.
        "/two": f"<nav>Menu</nav><article><p>{body}</p></article><article>B</article>",
        # One inside a <template> is inert: the live one still marks it.
        "/template": f"<nav>Menu</nav><article><p>{body}</p></article>"
        "<template><article>card</article></template>",
        # Short but whole: kept, not swapped for the navigation around it.
        "/short": "<nav>" + "Menu entry " * 30 + "</nav><main>Gone fishing.</main>",
        # A landmark a script fills in later: the whole page — and the note.
        "/shell": "<nav>" + "Menu entry " * 30 + "</nav><main></main><script></script>",
        # Images outside the main content still count for images=true.
        "/images": f"<header><img src='/logo.png' alt='Logo'></header>"
        f"<main><p>{body}</p><img src='/fig.png' alt='Fig'></main>",
    }
    _mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            text=f"<html>{pages[request.url.path]}</html>",
            headers={"content-type": "text/html"},
        ),
    )

    async def read(path: str, **args: Any) -> Any:
        url = f"https://example.com{path}"
        return await page_tool.invoke({"url": url, **args}, _ctx())

    page = await read("/one-main")
    assert page.title == "T" and page.text.startswith("# Topic")
    assert "Menu" not in page.text and "Footer" not in page.text
    assert "Menu" not in (await read("/one-article")).text
    assert "Menu" in (await read("/two")).text
    assert "Menu" not in (await read("/template")).text
    short = await read("/short")
    assert short.text == "Gone fishing." and short.note is None
    shell = await read("/shell")
    assert "Menu entry" in shell.text and shell.note is not None
    page = await read("/images", images=True)
    assert "logo.png" not in page.text  # the Markdown is the main content's
    assert [i.alt for i in page.images] == ["Logo", "Fig"]


@pytest.mark.asyncio
async def test_web_fetch_says_when_a_page_needs_javascript(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    pages = {
        "/app": "<div id='root'>Loading…</div><script src='/app.js'></script>",
        # Text of its own, scripts or not: no such note.
        "/doc": "<p>" + "words " * 100 + "</p><script>x()</script>",
        # Short, and its only script is data a browser never runs: no note.
        "/data": "<p>Closed today.</p><script type='application/ld+json'>{}</script>",
    }
    _mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            text=f"<html><body>{pages[request.url.path]}</body></html>",
            headers={"content-type": "text/html"},
        ),
    )
    page = await page_tool.invoke({"url": "https://example.com/app"}, _ctx())
    assert page.note is not None and "JavaScript" in page.note
    # Outside the fence: it is lovia's word, not the page's.
    rendered = _render_page(page, None)
    assert rendered.index("JavaScript") < rendered.index("--- BEGIN")
    page = await page_tool.invoke({"url": "https://example.com/doc"}, _ctx())
    assert page.note is None
    page = await page_tool.invoke({"url": "https://example.com/data"}, _ctx())
    assert page.note is None


def _pdf(*pages: str) -> bytes:
    """A minimal PDF: one Helvetica text line per page, a correct xref."""
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode(),
    ]
    font = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {4 + 2 * i} 0 R /Resources << /Font << /F1 {font} 0 R >> >> >>".encode()
        )
        objects.append(
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (n, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


@pytest.mark.asyncio
async def test_web_fetch_reads_a_pdf_as_text(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    pytest.importorskip("pypdf")
    responses = iter(
        [
            # By its type, or — served as a download — by its first bytes.
            httpx.Response(
                200,
                content=_pdf("First page", "Second page"),
                headers={"content-type": "application/pdf"},
            ),
            httpx.Response(
                200,
                content=_pdf("Sniffed"),
                headers={"content-type": "application/octet-stream"},
            ),
            httpx.Response(
                200, content=_pdf(""), headers={"content-type": "application/pdf"}
            ),
        ]
    )
    _mock_http(monkeypatch, lambda request: next(responses))
    page = await page_tool.invoke({"url": "https://example.com/a.pdf"}, _ctx())
    assert page.text == "First page\n\nSecond page"
    sniffed = await page_tool.invoke({"url": "https://example.com/dl"}, _ctx())
    assert sniffed.text == "Sniffed"
    scanned = await page_tool.invoke({"url": "https://example.com/scan.pdf"}, _ctx())
    assert "no text layer" in scanned.text


@pytest.mark.asyncio
async def test_a_pdf_gets_its_own_download_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("pypdf")
    pdf = _pdf("x" * 100)
    # Typed as a PDF, or a download that only its first bytes say is one.
    types = {"/a.pdf": "application/pdf", "/dl": "application/octet-stream"}
    _mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=pdf, headers={"content-type": types[request.url.path]}
        ),
    )

    async def read(reader: HttpReader, path: str) -> Any:
        url = f"https://example.com{path}"
        return await web_fetch(reader).invoke({"url": url}, _ctx())

    # Over the page cap, within the PDF cap: read whole, either way.
    for path in types:
        page = await read(HttpReader(max_bytes=100, cache_ttl=0), path)
        assert page.text == "x" * 100
    # Exactly the PDF cap is whole; one byte under it is cut short — said,
    # not parsed: a PDF cut short reads as nothing.
    exact = HttpReader(max_bytes=100, pdf_max_bytes=len(pdf), cache_ttl=0)
    assert (await read(exact, "/a.pdf")).text == "x" * 100
    tight = HttpReader(max_bytes=100, pdf_max_bytes=len(pdf) - 1, cache_ttl=0)
    page = await read(tight, "/a.pdf")
    assert page.size_capped and "larger than the download cap" in page.text


def test_a_pdf_s_text_is_bounded_and_its_title_best_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("pypdf")
    from lovia.tools import _pdf as pdf_module

    monkeypatch.setattr(pdf_module, "MAX_TEXT_CHARS", 10)
    out = pdf_module.pdf_text(_pdf("first page text", "second", "third"))
    assert out.text.startswith("first page text")
    assert "second" not in out.text
    assert "the first 1 of 3 pages" in out.text
    # A malformed /Info entry costs the title, never the text.
    import pypdf

    monkeypatch.setattr(
        pypdf.PdfReader,
        "metadata",
        property(lambda self: (_ for _ in ()).throw(pypdf.errors.PdfReadError("bad"))),
    )
    monkeypatch.setattr(pdf_module, "MAX_TEXT_CHARS", 1_000)
    out = pdf_module.pdf_text(_pdf("still here"))
    assert (out.text, out.title) == ("still here", None)


@pytest.mark.asyncio
async def test_an_offset_read_is_a_slice_of_the_page_read_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parses = {"n": 0}
    real = page_from_response

    def counting(*args: Any, **kwargs: Any) -> Any:
        parses["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr("lovia.tools.page.page_from_response", counting)
    _html_page(monkeypatch, "<p>" + "word " * 2_000 + "</p><img src='/a.png'>")
    tool = web_fetch(HttpReader(), max_chars=1_000)
    first = await tool.invoke({"url": "https://example.com/long"}, _ctx())
    assert first.images == []  # not asked for
    again = await tool.invoke(
        {
            "url": "https://example.com/long",
            "offset": first.next_offset,
            "images": True,
        },
        _ctx(),
    )
    assert parses["n"] == 1
    assert [i.url for i in again.images] == ["https://example.com/a.png"]


def test_a_pdf_without_pypdf_says_how_to_read_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    real_import = builtins.__import__

    def no_pypdf(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "pypdf":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pypdf)
    from lovia.tools._pdf import pdf_text

    assert "lovia[pdf]" in pdf_text(_pdf("x")).text


@pytest.mark.asyncio
async def test_web_fetch_rejects_non_http_schemes(page_tool: Tool) -> None:
    with pytest.raises(ToolError, match="scheme"):
        await page_tool.invoke({"url": "file:///etc/passwd"}, _ctx())


@pytest.mark.asyncio
async def test_web_fetch_retries_a_dropped_connection(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    """Retrying is safe: this tool only ever issues GETs."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("")
        return httpx.Response(
            200, text="<p>ok</p>", headers={"content-type": "text/html"}
        )

    _mock_http(monkeypatch, handler)
    page = await run_tool(page_tool, {"url": "https://example.com/"}, _ctx())
    assert page.text == "ok"
    assert attempts["n"] == 3


# ------------------------------------------------------------ HttpReader


@pytest.mark.asyncio
async def test_reader_sends_an_identifying_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent")
        seen["accept"] = request.headers.get("accept")
        return httpx.Response(
            200, text="<p>x</p>", headers={"content-type": "text/html"}
        )

    _mock_http(monkeypatch, handler)
    await HttpReader(cache_ttl=0).read("https://example.com/")
    # Sites routinely 403 an unidentified client.
    assert "lovia/" in seen["ua"] and seen["ua"].startswith("Mozilla/5.0")
    assert "text/html" in seen["accept"]


@pytest.mark.asyncio
async def test_reader_caches_by_url_so_continuation_cannot_splice_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            text=f"<p>version {calls['n']}</p>",
            headers={"content-type": "text/html"},
        )

    _mock_http(monkeypatch, handler)
    reader = HttpReader(cache_ttl=300)
    first = await reader.read("https://example.com/a")
    again = await reader.read("https://example.com/a")
    assert first.text == again.text == "version 1"
    assert calls["n"] == 1
    # A different URL is a different entry.
    await reader.read("https://example.com/b")
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_reader_cache_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200, text="<p>x</p>", headers={"content-type": "text/html"}
        )

    _mock_http(monkeypatch, handler)
    reader = HttpReader(cache_ttl=0)
    await reader.read("https://example.com/a")
    await reader.read("https://example.com/a")
    assert calls["n"] == 2
    assert not reader._cache


@pytest.mark.asyncio
async def test_reader_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    # An unbounded cache would let a long-running agent hold every page body
    # it ever read; that is a leak, not an optimisation.
    _html_page(monkeypatch, "<p>x</p>")
    reader = HttpReader(cache_size=2)
    for i in range(5):
        await reader.read(f"https://example.com/{i}")
    assert len(reader._cache) == 2
    assert list(reader._cache) == ["https://example.com/3", "https://example.com/4"]


@pytest.mark.asyncio
async def test_reader_cache_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200, text="<p>x</p>", headers={"content-type": "text/html"}
        )

    _mock_http(monkeypatch, handler)
    clock = {"t": 1_000.0}
    monkeypatch.setattr("lovia.tools.page.time.monotonic", lambda: clock["t"])
    reader = HttpReader(cache_ttl=60)
    await reader.read("https://example.com/a")
    clock["t"] += 30
    await reader.read("https://example.com/a")
    assert calls["n"] == 1
    clock["t"] += 61
    await reader.read("https://example.com/a")
    assert calls["n"] == 2


# ----------------------------------------------------- custom backends


@pytest.mark.asyncio
async def test_web_fetch_accepts_a_custom_backend() -> None:
    """How JavaScript rendering gets solved without lovia shipping a browser."""
    seen: dict[str, Any] = {}

    class Stub:
        async def read(self, url: str, *, images: bool = False) -> Page:
            seen.update(url=url, images=images)
            return Page(
                url=url,
                status_code=200,
                media_type="text/html",
                title="Rendered",
                text="from a real browser",
                images=[PageImage(url="https://x/i.png", alt="i")],
            )

    tool = web_fetch(Stub())
    page = await tool.invoke({"url": "https://example.com/spa", "images": True}, _ctx())
    assert seen == {"url": "https://example.com/spa", "images": True}
    assert page.title == "Rendered"
    assert page.images[0].url == "https://x/i.png"


@pytest.mark.asyncio
async def test_web_fetch_does_not_mutate_the_backends_page() -> None:
    # A backend may cache and reuse the Page it returns; clipping must not
    # scribble on it.
    original = Page(
        url="https://x/",
        status_code=200,
        media_type="text/html",
        text="abcdefghij" * 10,
    )

    class Reusing:
        async def read(self, url: str, *, images: bool = False) -> Page:
            return original

    tool = web_fetch(Reusing(), max_chars=10)
    clipped = await tool.invoke({"url": "https://x/"}, _ctx())
    assert len(clipped.text) == 10
    assert clipped.next_offset == 10
    assert len(original.text) == 100
    assert original.next_offset is None


@pytest.mark.asyncio
async def test_web_fetch_works_without_a_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # tools=[web_fetch()] must just work: no backend to pick.
    _html_page(monkeypatch, "<h1>Ready</h1>")
    page = await web_fetch().invoke({"url": "https://example.com/ready"}, _ctx())
    assert page.text == "# Ready"


def test_web_fetch_names_the_tool_and_keeps_the_schema_small() -> None:
    tool = web_fetch()
    assert tool.name == "web_fetch"
    # Timeouts and byte caps are operator decisions, not model decisions: the
    # model pays tokens for this schema on every single call.
    assert set(tool.parameters["properties"]) == {"url", "images", "offset"}
    assert web_fetch(name="browse").name == "browse"


# --------------------------------------------------------------- render


def test_render_page_leads_with_what_orients_the_model() -> None:
    page = Page(
        url="https://example.com/doc",
        status_code=200,
        media_type="text/html",
        title="Doc",
        text="body text",
    )
    out = _render_page(page, None)
    # The page's own words — title included — sit inside the fence, so the
    # title opens the fenced block rather than the whole result.
    assert out.splitlines()[:5] == [
        "URL: https://example.com/doc",
        "HTTP 200 · text/html",
        "",
        "--- BEGIN EXTERNAL CONTENT (reference material) ---",
        "Title: Doc",
    ]
    assert out.endswith("body text\n--- END EXTERNAL CONTENT ---")


def test_render_page_spells_out_how_to_continue() -> None:
    page = Page(
        url="https://x/",
        status_code=200,
        media_type="text/html",
        text="clipped",
        next_offset=20_000,
    )
    # The model should not have to do arithmetic to resume.
    assert "offset=20000" in _render_page(page, None)


def test_render_page_reports_the_true_image_total() -> None:
    many = [PageImage(url=f"https://x/{i}.png", alt=f"a{i}") for i in range(60)]
    out = _render_page(
        Page(url="https://x/", status_code=200, media_type="text/html", images=many),
        None,
    )
    # Capped for the model, but the count is honest — no silent truncation.
    assert "Images (50 of 60):" in out
    assert "50. https://x/49.png — a49" in out
    assert "https://x/50.png" not in out

    few = many[:3]
    assert "Images (3):" in _render_page(
        Page(url="https://x/", status_code=200, media_type="text/html", images=few),
        None,
    )


def test_render_page_falls_back_for_unexpected_shapes() -> None:
    # Only the Page shape is ours to format; the runner never routes error
    # strings through renderers, so anything else came from a direct caller.
    assert _render_page("plain string", None) == "plain string"
    assert _render_page([1, 2], None) == "[1, 2]"
