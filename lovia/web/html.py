"""``show_html``: an interactive page in the chat UI.

The ``html`` component's props are ``{"html": str, "title"?: str}``; the
bundled page (``static/js/html.js``) runs the page in a sandboxed iframe with
no network access, and its ``lovia.send(text)`` posts a follow-up on a click.
A tool that builds a page returns ``ui_result("html", props)`` — with
:func:`~lovia.web.put_ui_data` when the page is big. See
``docs/design/web-html-component.md``.

``lovia web``'s default agent has it; add it to your own agent with
``tools=[show_html]``.
"""

from __future__ import annotations

from typing import Annotated

from ..exceptions import ToolError
from ..tools.base import tool
from .components import _check_json_size, ui_result

__all__ = ["MAX_HTML_BYTES", "show_html"]

# A model's output limit (16–32K tokens) is ~64–128 KB of HTML: this rejects
# only pages a model couldn't have finished writing anyway.
MAX_HTML_BYTES = 128 * 1024


@tool
def show_html(
    html: Annotated[
        str, "A complete, self-contained HTML page: inline CSS and JS only."
    ],
    title: Annotated[str | None, "A short heading for the page."] = None,
) -> str:
    """Show the user an interactive HTML page, run in a sandbox inside the chat.

    Use it for what a chart or table can't do: a calculator, a simulation, a
    form that explores options, a custom visualisation. For a plain chart or
    table, use show_chart or show_table instead.

    The page has no network: no CDN scripts, fonts, remote images, or fetch —
    inline everything. No storage, alert(), or form submission either. Style
    it with the chat's CSS variables (var(--lv-ink), var(--lv-surface),
    var(--lv-border), var(--lv-accent), var(--lv-chart-1) … var(--lv-chart-8)) so it
    matches light and dark themes. A button can ask a follow-up as the user:
    lovia.send("Break March down by region"). Never ask for passwords or
    personal data. Don't repeat the page's content in your reply.
    """
    if not html.strip():
        raise ToolError(
            "html is empty", hint="write the whole page, with its script and style"
        )
    _check_json_size(
        {"html": html, "title": title},
        what="page",
        limit=MAX_HTML_BYTES,
        hint="make the page smaller: less inline data, shorter code",
    )
    return ui_result("html", from_args=True)
