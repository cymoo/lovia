"""Tests for ``lovia.tools.page`` — the read_page tool and its HTML conversion."""

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
    page_reader,
    read_page,
)
from lovia.tools.page import _render_page


def _ctx() -> RunContext:
    return RunContext(context=None, entries=[], agent=None)  # type: ignore[arg-type]


@pytest.fixture
def page_tool() -> Tool:
    """A tool with a fresh, cache-free reader.

    The exported ``read_page`` deliberately shares one process-wide response
    cache, so tests must not route through it or they see each other's pages.
    """
    return page_reader(HttpReader(cache_ttl=0))


def _mock_http(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    real_client = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr("lovia.tools.http.httpx.AsyncClient", factory)


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


# ------------------------------------------------------------- read_page


@pytest.mark.asyncio
async def test_read_page_reports_title_and_final_url(
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
async def test_read_page_resolves_relative_urls_against_the_final_url(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    _html_page(monkeypatch, "<a href='sibling'>s</a><img src='/i.png' alt='i'>")
    page = await page_tool.invoke({"url": "https://example.com/dir/doc"}, _ctx())
    assert "[s](https://example.com/dir/sibling)" in page.text
    assert "![i](https://example.com/i.png)" in page.text


@pytest.mark.asyncio
async def test_read_page_honors_base_href(
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
async def test_read_page_images_are_opt_in(
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
async def test_read_page_collects_images_markdown_cannot_show(
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
async def test_read_page_continues_from_next_offset(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    body = "".join(f"<p>line {i:04d}</p>" for i in range(400))
    _html_page(monkeypatch, body)
    tool = page_reader(max_chars=500)

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
async def test_read_page_offset_past_the_end_is_empty_not_an_error(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    _html_page(monkeypatch, "<p>short</p>")
    page = await page_tool.invoke(
        {"url": "https://example.com/", "offset": 10_000}, _ctx()
    )
    assert page.text == ""
    assert page.next_offset is None


@pytest.mark.asyncio
async def test_read_page_caps_error_pages_hard(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    # A 500's HTML template is never worth a full character budget.
    _html_page(monkeypatch, "<p>" + "sorry " * 500 + "</p>", status=500)
    page = await page_tool.invoke({"url": "https://example.com/boom"}, _ctx())
    assert page.status_code == 500
    assert len(page.text) <= 500


@pytest.mark.asyncio
async def test_read_page_surfaces_a_capped_download(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    _html_page(monkeypatch, "<p>" + "x" * 5_000 + "</p>")
    tool = page_reader(HttpReader(max_bytes=1_000, cache_ttl=0))
    page = await tool.invoke({"url": "https://example.com/huge"}, _ctx())
    # size_capped says the tail was never fetched, so no offset will reach it —
    # a different situation from "clipped, ask for more".
    assert page.size_capped is True
    assert "download capped" in _render_page(page, None)


@pytest.mark.asyncio
async def test_read_page_sniffs_meta_charset(
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
async def test_read_page_handles_non_html_content(
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


@pytest.mark.asyncio
async def test_read_page_falls_back_to_markup_when_extraction_is_empty(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    # An app shell has no text at all; the markup is more useful than nothing.
    _html_page(monkeypatch, "<html><body><div id='root'></div></body></html>")
    page = await page_tool.invoke({"url": "https://example.com/spa"}, _ctx())
    assert "id='root'" in page.text or 'id="root"' in page.text


@pytest.mark.asyncio
async def test_read_page_rejects_non_http_schemes(page_tool: Tool) -> None:
    with pytest.raises(ToolError, match="scheme"):
        await page_tool.invoke({"url": "file:///etc/passwd"}, _ctx())


@pytest.mark.asyncio
async def test_read_page_retries_a_dropped_connection(
    monkeypatch: pytest.MonkeyPatch, page_tool: Tool
) -> None:
    """Retrying is safe now that this tool only ever issues GETs."""
    from lovia.tools import run_tool

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
async def test_page_reader_accepts_a_custom_backend() -> None:
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

    tool = page_reader(Stub())
    page = await tool.invoke({"url": "https://example.com/spa", "images": True}, _ctx())
    assert seen == {"url": "https://example.com/spa", "images": True}
    assert page.title == "Rendered"
    assert page.images[0].url == "https://x/i.png"


@pytest.mark.asyncio
async def test_page_reader_does_not_mutate_the_backends_page() -> None:
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

    tool = page_reader(Reusing(), max_chars=10)
    clipped = await tool.invoke({"url": "https://x/"}, _ctx())
    assert len(clipped.text) == 10
    assert clipped.next_offset == 10
    assert len(original.text) == 100
    assert original.next_offset is None


@pytest.mark.asyncio
async def test_exported_read_page_works_without_assembly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # tools=[read_page] must just work: no factory call, no backend to pick.
    _html_page(monkeypatch, "<h1>Ready</h1>")
    page = await read_page.invoke({"url": "https://singleton.example/ready"}, _ctx())
    assert read_page.name == "read_page"
    assert page.text == "# Ready"


def test_page_reader_names_the_tool_and_keeps_the_schema_small() -> None:
    tool = page_reader()
    assert tool.name == "read_page"
    # Timeouts and byte caps are operator decisions, not model decisions: the
    # model pays tokens for this schema on every single call.
    assert set(tool.parameters["properties"]) == {"url", "images", "offset"}
    assert page_reader(name="browse").name == "browse"


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
    assert out.splitlines()[:3] == [
        "Title: Doc",
        "URL: https://example.com/doc",
        "HTTP 200 · text/html",
    ]
    assert "body text" in out


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
