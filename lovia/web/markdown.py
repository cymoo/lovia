"""Safe markdown rendering for the bundled chat UI."""

from __future__ import annotations

try:
    from markdown_it import MarkdownIt
except ImportError as exc:  # pragma: no cover - depends on optional env
    from ._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

_MARKDOWN = MarkdownIt("commonmark", {"html": False, "linkify": False})


def render_markdown(text: str) -> str:
    """Render user/model markdown while escaping raw HTML."""

    return _MARKDOWN.render(text)
