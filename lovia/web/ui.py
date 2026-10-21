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

from .. import __version__
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
# Where create_app serves ``ChatUI.assets``.
ASSETS_PATH = "/ui"

# Languages the bundled UI ships strings for; ``ChatUI.messages`` adds more.
BUILTIN_LANGS = ("en", "zh")
# A BCP 47-style tag: "ja", "pt-BR", "zh-Hant".
_LANG_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


@dataclass(frozen=True)
class ChatUI:
    """Options for the bundled chat UI: ``create_app(agent, ui=ChatUI(...))``.

    ``empty_title`` and ``empty_description`` (a string or a list of short
    lines) fill the blank chat state — unset, lovia's own copy shows in the
    user's language; ``empty_examples`` are clickable starter prompts on it —
    clicking one fills the composer, it doesn't send.

    ``login_url`` is where the page sends a signed-out user: an API call that
    a custom ``auth=`` dependency answers with 401 navigates there. A
    ``{next}`` in it becomes the current page's URL-encoded path, for the
    login flow to return to. Unset, the page only says the user is signed
    out. The token check (``token=``) keeps its own prompt either way.

    ``assets`` is your UI directory, served at ``<app>/ui/`` — every file
    in it except templates (``*.html``) and dotfiles is public. An
    ``index.html`` there replaces the page; start it with
    ``{% extends "lovia/index.html" %}`` and fill only the blocks you need —
    ``head``, ``sidebar_header``, ``sidebar_nav``, ``sidebar_footer``,
    ``topbar_actions``, ``composer_actions``, ``welcome``, ``body_end``.
    The files are revalidated on every load, so an edit shows on reload.

    ``scripts`` are JS modules the page imports before it starts — where
    extensions register (``window.lovia``; see :func:`~lovia.web.ui_result`
    for component renderers). One that fails to load is reported and
    skipped; one still loading after 5 seconds stops holding up the start.
    ``styles`` load after the bundled stylesheet, ``logo`` replaces the
    sidebar's title, ``favicon`` the tab icon. Each of these is a file in
    ``assets`` (``"theme.css"``), a path on the app (``"/brand/x.css"``,
    following a mount prefix), or a URL (``"https://…"``).

    ``messages`` overrides or adds UI strings per language —
    ``{"zh": {"composer.placeholder": "问点什么…"}, "ja": {...}}``. A
    language the UI doesn't ship becomes selectable in Settings. Keys under a
    prefix of your own (``acme.help``) label your template's elements through
    ``data-i18n``; an unknown key under one of the UI's prefixes (``nav.``,
    ``composer.``, …) — a typo, or a string a release renamed — is logged as a
    warning in the browser console. ``default_lang`` is the language for a
    user who hasn't picked one in Settings; unset, the browser's decides.
    """

    empty_title: str | None = None
    empty_description: str | Sequence[str] | None = None
    empty_examples: Sequence[str] = ()
    login_url: str | None = None
    assets: str | Path | None = None
    scripts: Sequence[str] = ()
    styles: Sequence[str] = ()
    logo: str | None = None
    favicon: str | None = None
    default_lang: str | None = None
    messages: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.assets is not None and not Path(self.assets).expanduser().is_dir():
            raise UserError(
                f"ChatUI assets directory not found: {self.assets}",
                hint="pass an existing directory holding your index.html, "
                "scripts and styles",
            )
        refs = [*_as_list(self.scripts), *_as_list(self.styles)]
        refs += [ref for ref in (self.logo, self.favicon) if ref]
        for ref in refs:
            if self.assets is None and _in_assets(ref):
                raise UserError(
                    f"ChatUI: {ref!r} names a file in assets, but no assets "
                    "directory is set",
                    hint='set assets=..., or give a path on the app ("/ext/x.js") '
                    'or a URL ("https://…")',
                )
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


def _is_url(ref: str) -> bool:
    return "://" in ref or ref.startswith("//")


def _in_assets(ref: str) -> bool:
    return not _is_url(ref) and not ref.startswith("/")


def _ref_url(ref: str, base_path: str) -> str:
    """Where the page loads a ``ChatUI`` file reference from."""
    if _is_url(ref):
        return ref
    if ref.startswith("/"):
        return base_path + ref
    return f"{base_path}{ASSETS_PATH}/{ref}"


def build_ui_router(ui: ChatUI, *, title: str) -> APIRouter:
    """Router that serves the bundled single-page chat UI."""
    router = APIRouter()
    templates = _templates(ui.assets)
    examples = _as_list(ui.empty_examples)
    scripts = _as_list(ui.scripts)
    styles = _as_list(ui.styles)
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
                "styles": [_ref_url(s, base_path) for s in styles],
                "logo": ui.logo and _ref_url(ui.logo, base_path),
                "favicon": ui.favicon and _ref_url(ui.favicon, base_path),
                "app_config": {
                    "version": __version__,
                    # Imported by main.js before the app starts (extensions).
                    "scripts": [_ref_url(s, base_path) for s in scripts],
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
        return response

    return router
