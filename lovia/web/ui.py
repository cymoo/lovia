"""The bundled chat UI router — the part a custom front-end would replace.

Serves the single-page app at ``GET /``. Keep this separate from
:mod:`lovia.web.api` so ``create_app(agent, ui=False)`` (or mounting only
``build_api_router``) yields a pure JSON + SSE server.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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

# Languages the bundled UI ships strings for; ``ChatUI.messages`` adds more.
BUILTIN_LANGS = ("en", "zh")
# A BCP 47-style tag: "ja", "pt-BR", "zh-Hant".
_LANG_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


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
    ``head``, ``sidebar_header``, ``sidebar_nav``, ``sidebar_footer``,
    ``topbar_actions``, ``composer_actions``, ``welcome``, ``body_end``.

    ``scripts`` are JS modules loaded after the app's own — where custom UI
    component renderers register (``window.lovia.registerRenderer``; see
    :func:`~lovia.web.ui_result`). An absolute URL is used as is; anything
    else is relative to the app, so it follows a mount prefix. Serving the
    files is yours: ``app.mount("/ext", StaticFiles(directory="ext"))``.

    ``messages`` overrides or adds UI strings per language —
    ``{"zh": {"composer.placeholder": "问点什么…"}, "ja": {...}}``. A
    language the UI doesn't ship becomes selectable in Settings. Keys under a
    prefix of your own (``acme.help``) label your template's elements through
    ``data-i18n``; an unknown key under one of the UI's prefixes (``nav.``,
    ``composer.``, …) — a typo, or a string a release renamed — is logged as a
    warning in the browser console. ``default_lang`` is the language for a
    user who hasn't picked one in Settings; unset, the browser's decides.
    """

    empty_title: str = "Where shall we begin?"
    empty_description: str | Sequence[str] = (
        "A good question is already half the answer."
    )
    empty_examples: Sequence[str] = ()
    login_url: str | None = None
    templates: str | Path | None = None
    scripts: Sequence[str] = ()
    default_lang: str | None = None
    messages: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for lang, strings in self.messages.items():
            if not isinstance(lang, str) or not _LANG_RE.match(lang):
                raise UserError(
                    f"ChatUI messages: invalid language tag {lang!r}",
                    hint='use a BCP 47 tag such as "ja" or "pt-BR"',
                )
            if not isinstance(strings, Mapping) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in strings.items()
            ):
                raise UserError(
                    f"ChatUI messages[{lang!r}] must map message keys to strings",
                )
        known = (*BUILTIN_LANGS, *self.messages)
        if self.default_lang is not None and self.default_lang not in known:
            raise UserError(
                f"ChatUI default_lang {self.default_lang!r} has no strings",
                hint=f"use one of {', '.join(known)}, or add it to messages",
            )


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


def _as_list(value: str | Sequence[str]) -> list[str]:
    # A bare string is one item, not an iterable of characters.
    return [value] if isinstance(value, str) else list(value)


def _script_url(src: str, base_path: str) -> str:
    if "://" in src or src.startswith("//"):
        return src
    return f"{base_path}/{src.lstrip('/')}"


def build_ui_router(ui: ChatUI, *, title: str) -> APIRouter:
    """Router that serves the bundled single-page chat UI."""
    router = APIRouter()
    templates = _templates(ui.templates)
    examples = _as_list(ui.empty_examples)
    scripts = _as_list(ui.scripts)
    i18n = {
        "default_lang": ui.default_lang,
        "messages": {lang: dict(strings) for lang, strings in ui.messages.items()},
    }

    @router.get("/", include_in_schema=False)
    async def index(request: Request) -> Any:
        base_path = request.scope.get("root_path", "").rstrip("/")
        response = templates.TemplateResponse(
            request,
            "index.html",
            {
                "title": title,
                "empty_title": ui.empty_title,
                "empty_description": ui.empty_description,
                "empty_examples": examples,
                "scripts": [_script_url(s, base_path) for s in scripts],
                "app_config": {
                    "i18n": i18n,
                    # The mount prefix (a proxy's root_path, or app.mount's):
                    # the client prepends it to every /api call.
                    "base_path": base_path,
                    # The versioned asset prefix, for vendored files a module
                    # loads on demand (charts.js → echarts).
                    "static_base": request.url_for("static", path="").path,
                    "login_url": ui.login_url,
                },
            },
        )
        # The shell must always revalidate so a new build's version-stamped
        # asset URLs (see `_asset_token` in app.py) are picked up right after an
        # upgrade, instead of a heuristically-cached shell pointing at the old,
        # now-404 prefix.
        response.headers["Cache-Control"] = "no-cache"
        # The page's own frames are all srcdoc, which this allows; what it
        # stops is a sandboxed page (static/js/html.js, the Files preview)
        # navigating itself to a URL that carries its data away.
        response.headers["Content-Security-Policy"] = "frame-src 'none'"
        return response

    return router
