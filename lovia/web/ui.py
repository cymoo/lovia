"""The bundled chat UI router — the part a custom front-end would replace.

Serves the single-page app at ``GET /``. Keep this separate from
:mod:`lovia.web.api` so ``create_app(agent, ui=False)`` (or mounting only
``build_api_router``) yields a pure JSON + SSE server.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import jinja2
    from fastapi import APIRouter, Request
    from fastapi.templating import Jinja2Templates
except ImportError as exc:  # pragma: no cover - depends on optional env
    from ._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ..exceptions import UserError

SURFACE_NOTE = (
    "Replies render as GitHub-flavored Markdown in the chat UI, including "
    "```mermaid diagrams and inline images — ![alt](url) displays the image "
    "itself, so embed images directly instead of saying they cannot be shown. "
    "A workspace file works the same way — reference it by its "
    "workspace-relative path: ![chart](uploads/chart.png) shows the image, "
    "[report](report.md) links to the file."
)
r"""What the bundled chat UI renders — tell the model, or it assumes plain text.

An untold model answers image requests with "I cannot display images here".
The ``lovia web`` default agent carries this note automatically; agents served
via ``--app`` or :func:`~lovia.web.serve` opt in::

    from lovia.web import SURFACE_NOTE

    agent = Agent(name="x", instructions="Be helpful.\n\n" + SURFACE_NOTE)

Defined next to the UI it describes so the two change together.
"""

_TEMPLATE_DIR = Path(__file__).parent / "templates"


@dataclass(frozen=True)
class ChatUI:
    """Options for the bundled chat UI: ``create_app(agent, ui=ChatUI(...))``.

    ``empty_title`` and ``empty_description`` (a string or a list of short
    lines) fill the blank chat state; ``empty_examples`` are clickable starter
    prompts on it — clicking one fills the composer, it doesn't send.

    ``login_url`` is where the page sends a signed-out user: an API call that
    a custom ``auth=`` dependency answers with 401 navigates there. A
    ``{next}`` in it becomes the current page's URL-encoded path, for the
    login flow to return to. Unset, the page only says the user is signed
    out. The token check (``token=``) keeps its own prompt either way.

    ``templates`` is a directory of your own Jinja templates. An
    ``index.html`` there replaces the page; start it with
    ``{% extends "lovia/index.html" %}`` and fill only the blocks you need —
    ``head``, ``sidebar_footer``, ``body_end``.
    """

    empty_title: str = "Where shall we begin?"
    empty_description: str | Sequence[str] = (
        "A good question is already half the answer."
    )
    empty_examples: Sequence[str] = ()
    login_url: str | None = None
    templates: str | Path | None = None


def _templates(directory: str | Path | None) -> Jinja2Templates:
    if directory is None:
        return Jinja2Templates(directory=_TEMPLATE_DIR)
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise UserError(
            f"ChatUI templates directory not found: {directory}",
            hint="pass an existing directory holding your index.html",
        )
    builtin = jinja2.FileSystemLoader(_TEMPLATE_DIR)
    # Yours first; the bundled page stays reachable as "lovia/index.html" so
    # an override can extend it without extending itself.
    loader = jinja2.ChoiceLoader(
        [
            jinja2.FileSystemLoader(root),
            jinja2.PrefixLoader({"lovia": builtin}),
            builtin,
        ]
    )
    env = jinja2.Environment(loader=loader, autoescape=jinja2.select_autoescape())
    return Jinja2Templates(env=env)


def build_ui_router(ui: ChatUI, *, title: str) -> APIRouter:
    """Router that serves the bundled single-page chat UI."""
    router = APIRouter()
    templates = _templates(ui.templates)
    # A bare string is one example, not an iterable of characters.
    examples = (
        [ui.empty_examples]
        if isinstance(ui.empty_examples, str)
        else list(ui.empty_examples)
    )

    @router.get("/", include_in_schema=False)
    async def index(request: Request) -> Any:
        response = templates.TemplateResponse(
            request,
            "index.html",
            {
                "title": title,
                "empty_title": ui.empty_title,
                "empty_description": ui.empty_description,
                "empty_examples": examples,
                "app_config": {
                    "empty_title": ui.empty_title,
                    "empty_description": ui.empty_description,
                    "empty_examples": examples,
                    # The mount prefix (a proxy's root_path, or app.mount's):
                    # the client prepends it to every /api call.
                    "base_path": request.scope.get("root_path", "").rstrip("/"),
                    "login_url": ui.login_url,
                },
            },
        )
        # The shell must always revalidate so a new build's version-stamped
        # asset URLs (see `_asset_token` in app.py) are picked up right after an
        # upgrade, instead of a heuristically-cached shell pointing at the old,
        # now-404 prefix.
        response.headers["Cache-Control"] = "no-cache"
        return response

    return router
