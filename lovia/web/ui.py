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
    empty_title: str = "Wake up, Neo.",
    empty_description: str | Sequence[str] | None = None,
) -> APIRouter:
    """Router that serves the bundled single-page chat UI."""
    router = APIRouter()

    @router.get("/", include_in_schema=False)
    async def index(request: Request) -> Any:
        description = empty_description
        if description is None:
            description = "The Matrix has you."
        return _TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "title": title,
                "empty_title": empty_title,
                "empty_description": description,
                "app_config": {
                    "empty_title": empty_title,
                    "empty_description": description,
                },
            },
        )

    return router
