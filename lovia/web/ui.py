"""The bundled chat UI router — the part a custom front-end would replace.

Serves the single-page app at ``GET /``. Keep this separate from
:mod:`lovia.web.api` so ``create_app(agent, ui=False)`` (or mounting only
``build_api_router``) yields a pure JSON + SSE server.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

try:
    from fastapi import APIRouter, Request
    from fastapi.templating import Jinja2Templates
except ImportError as exc:  # pragma: no cover - depends on optional env
    from ._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def build_ui_router(
    *,
    title: str = "lovia",
    empty_title: str = "Where shall we begin?",
    empty_description: str | Sequence[str] | None = None,
    empty_examples: Sequence[str] | None = None,
) -> APIRouter:
    """Router that serves the bundled single-page chat UI.

    ``empty_examples`` are clickable starter prompts on the blank chat state —
    clicking one fills the composer (it doesn't send).
    """
    router = APIRouter()

    @router.get("/", include_in_schema=False)
    async def index(request: Request) -> Any:
        description = empty_description
        if description is None:
            description = "A good question is already half the answer."
        examples = list(empty_examples) if empty_examples else []
        return _TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "title": title,
                "empty_title": empty_title,
                "empty_description": description,
                "empty_examples": examples,
                "app_config": {
                    "empty_title": empty_title,
                    "empty_description": description,
                    "empty_examples": examples,
                },
            },
        )

    return router
