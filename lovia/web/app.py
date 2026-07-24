"""FastAPI application factory + convenience launcher."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Mapping

try:
    from fastapi import Depends, FastAPI
    from fastapi.staticfiles import StaticFiles
except ImportError as exc:  # pragma: no cover - depends on optional env
    from ._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from .. import __version__
from ..agent import Agent
from ..context import ContextPolicy
from ..providers import Provider
from ..reliability import RetryPolicy, RunBudget
from ..session import Session
from ..tracing import Tracer
from .api import RouterDeps, build_api_router
from .api.memory import memory_plugin
from .approvals import ApprovalRegistry
from .auth import generate_token, is_loopback, token_dependency
from .scheduler import Scheduler
from .store import ChatStore
from .ui import build_ui_router

_STATIC = Path(__file__).parent / "static"

# Long-cache directive for the versioned static mount. Safe *only* because the
# mount path carries a build-specific token (see `_asset_token`): the bytes at
# any given URL never change, so the browser may keep them forever and never
# revalidate — a new build simply serves from a new URL prefix.
_IMMUTABLE_CACHE = "public, max-age=31536000, immutable"


def _register_web_mimetypes() -> None:
    """Pin the content types of the shell assets we serve.

    ``StaticFiles`` types every file through ``mimetypes.guess_type``, which
    Python seeds from platform sources — ``/etc/*mime.types``, Homebrew's
    ``/usr/local/etc/mime.types``, and on **Windows** the registry
    (``HKEY_CLASSES_ROOT\\.js``). Those ``.js`` mappings vary by machine and are
    infamously ``text/plain`` on Windows, where the browser then refuses our
    ES-module entry point ("Expected a JavaScript-or-Wasm module script but the
    server responded with a MIME type of 'text/plain'") and the UI never boots.
    Registering these makes the served types deterministic on every platform.
    """
    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("text/javascript", ".mjs")
    mimetypes.add_type("text/css", ".css")


# Run at import: `add_type` seeds `mimetypes` after its lazy `init()`, so our
# entries win over whatever the platform database resolved for these extensions.
_register_web_mimetypes()


class _ImmutableStaticFiles(StaticFiles):
    """``StaticFiles`` that marks successful responses immutable.

    Paired with the version-stamped mount path in :func:`create_app`, this gives
    the bundled UI proper cache-busting: assets cache forever, and an upgrade
    changes their URLs so the browser fetches the new module graph with no manual
    cache clear. 404/405 responses are left alone.
    """

    async def get_response(self, path: str, scope: Any) -> Any:
        response = await super().get_response(path, scope)
        if getattr(response, "status_code", None) == 200:
            response.headers["Cache-Control"] = _IMMUTABLE_CACHE
        return response


def _asset_token() -> str:
    """A short cache-busting token for the static mount, stable within a run.

    Hashes the package version plus each static file's ``(relpath, size, mtime)``
    so the token changes on every upgrade (``pip`` rewrites mtimes) and on any
    local edit across restarts — versioning the asset URL path so browsers pick
    up the new module graph without a manual cache clear. Falls back to the bare
    version if the tree can't be walked.
    """
    try:
        h = hashlib.sha256(__version__.encode())
        for p in sorted(_STATIC.rglob("*")):
            if p.is_file():
                st = p.stat()
                h.update(p.relative_to(_STATIC).as_posix().encode())
                h.update(f"\0{st.st_size}\0{st.st_mtime_ns}\0".encode())
        return h.hexdigest()[:12]
    except OSError:
        return "".join(c for c in __version__ if c.isalnum()) or "static"


def _normalise(
    agent_or_agents: "Agent[Any] | Mapping[str, Agent[Any]]",
) -> dict[str, Agent[Any]]:
    if isinstance(agent_or_agents, Mapping):
        return dict(agent_or_agents)
    return {agent_or_agents.name: agent_or_agents}


def _default_db_path(name: str) -> Path:
    """Default SQLite path under ``./.lovia``, derived from the agent's name."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return Path(".lovia") / f"{safe}.db"


def _clean_token(token: str | None) -> str | None:
    """Strip a caller-supplied token; reject one that is empty after stripping.

    ``token=""`` must fail fast, not silently disable auth (or, in ``serve``,
    silently skip the off-loopback auto-generation).
    """
    if token is None:
        return None
    cleaned = token.strip()
    if not cleaned:
        raise ValueError("token must be non-empty")
    return cleaned


def _display_host(host: str) -> str:
    """A browsable form of ``host`` for printed URLs — wildcards aren't one."""
    if host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    # An IPv6 literal needs brackets, or the port reads as part of the address.
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def create_app(
    agent_or_agents: "Agent[Any] | Mapping[str, Agent[Any]]",
    *,
    db_path: str | Path | None = None,
    session: Session | None = None,
    store: ChatStore | None = None,
    context_policy: ContextPolicy | None = None,
    title_model: str | Provider | None = None,
    generate_titles: bool = True,
    title: str = "lovia",
    max_turns: int = 50,
    budget: RunBudget | None = None,
    retry: RetryPolicy | None = None,
    tracer: Tracer | None = None,
    max_background_runs: int = 8,
    approval_timeout: float | None = None,
    scheduler_poll: float = 1.0,
    ui: bool = True,
    cors_origins: Sequence[str] | None = None,
    token: str | None = None,
    auth: Any = None,
    empty_title: str = "Where shall we begin?",
    empty_description: str | Sequence[str] | None = None,
    empty_examples: Sequence[str] | None = None,
) -> FastAPI:
    """Build a FastAPI app that exposes the given agent(s).

    Storage precedence (highest first):

    * ``store`` — fully-formed :class:`ChatStore`.
    * ``db_path`` — persist transcripts + metadata to a SQLite file.
    * ``session`` — bring-your-own :class:`Session`; metadata kept in-memory.
    * neither — default: SQLite file ``./.lovia/<agent_name>.db``.

    ``title_model`` overrides the model used to generate chat titles; defaults
    to the first agent's own ``model``.

    ``context_policy`` is a server-level override applied to every served
    agent; ``None`` (default) lets each agent's own ``context_policy`` — or
    the standard :class:`Compaction` — apply. Pass ``NoopContextPolicy()``
    to disable compaction server-wide.

    Run limits apply to every chat turn the server drives: ``max_turns`` caps
    the agent loop per request and ``budget`` (a :class:`RunBudget`) caps token
    spend, wall-clock, and tool calls. ``budget`` is a template — copied per run
    — so its clock and counters never carry across runs. ``retry`` (a
    :class:`RetryPolicy`) overrides the agent's own provider-retry posture for
    server-driven turns; ``None`` inherits it.

    ``approval_timeout`` auto-denies a pending tool approval after that many
    seconds (default ``None``: wait forever). Set it when using scheduled runs
    with approval-gated tools — a clientless run parked on an approval otherwise
    occupies one of the ``max_background_runs`` slots until someone opens the
    chat and decides.

    ``ui`` controls the bundled single-page chat UI: when ``True`` (default) the
    app also serves ``GET /`` and ``/static``; set it to ``False`` for a pure
    JSON + SSE server you drive from your own front-end (see
    :func:`lovia.web.build_api_router`). ``cors_origins`` lists the origins such
    a front-end is served from (e.g. ``["http://localhost:5173"]``) — omitted,
    no CORS headers are sent and cross-origin browsers are refused.

    ``token`` guards every ``/api/*`` route with bearer-token auth (see
    :mod:`lovia.web.auth`: ``Authorization: Bearer`` header or the UI's token
    cookie; ``/healthz`` stays open). ``auth`` replaces that check with your
    own FastAPI dependency (sessions, OAuth, …) — pass one or the other, not
    both. Neither is set by default: :func:`create_app` alone imposes no auth,
    while :func:`serve` refuses non-loopback binds without one.

    ``empty_title`` and ``empty_description`` customize the blank chat state;
    ``empty_description`` may be a string or a list of short lines, and
    ``empty_examples`` lists clickable starter prompts (clicking fills the
    composer without sending).
    """
    agents = _normalise(agent_or_agents)

    token = _clean_token(token)
    if token is not None and auth is not None:
        raise ValueError("pass either token= or auth=, not both")

    if store is not None:
        chat_store = store
    elif db_path is not None:
        chat_store = ChatStore.sqlite(db_path)
    elif session is not None:
        chat_store = ChatStore(session, meta_path=":memory:")
    else:
        chat_store = ChatStore.sqlite(_default_db_path(next(iter(agents), "lovia")))

    approvals = ApprovalRegistry()

    deps = RouterDeps(
        agents=agents,
        store=chat_store,
        approvals=approvals,
        title=title,
        # ``None`` = no server-level override: each agent's own context_policy
        # (or the loop's default Compaction) applies per run.
        context_policy=context_policy,
        title_model=title_model,
        generate_titles=generate_titles,
        max_turns=max_turns,
        budget=budget,
        retry=retry,
        tracer=tracer,
        max_background_runs=max_background_runs,
        approval_timeout=approval_timeout,
    )

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Run records left "running" belong to a previous process that died
        # with them — settle them before anything new starts.
        await chat_store.sweep_stale_runs()
        # Start the scheduler loop; on shutdown stop it, then wind down any live
        # background runs cooperatively (leaving resumable checkpoints).
        scheduler = Scheduler(deps, poll_interval=scheduler_poll)
        _app.state.scheduler = scheduler
        scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()
            await deps.supervisor.shutdown()
            # End any open /api/events streams so shutdown doesn't wait on them.
            if deps._bus is not None:
                deps._bus.close()
            # Background memory curation (curate_in_background) gets a bounded
            # window to land — a clean stop shouldn't drop the last run's
            # curation, but must not hang shutdown on a stuck model call.
            for agent in deps.agents.values():
                plugin = memory_plugin(agent)
                if plugin is not None:
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(plugin.drain(), timeout=15.0)

    app = FastAPI(
        title=title,
        lifespan=_lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    if cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_methods=["*"],
            allow_headers=["*"],
        )
    # Auth guards the API router only: static assets and the UI shell carry no
    # data, and serving them lets the UI collect the token client-side.
    guard = auth if auth is not None else (token_dependency(token) if token else None)
    app.include_router(
        build_api_router(deps),
        dependencies=[Depends(guard)] if guard is not None else None,
    )
    if ui:
        app.include_router(
            build_ui_router(
                title=title,
                empty_title=empty_title,
                empty_description=empty_description,
                empty_examples=empty_examples,
            )
        )
        # Version-stamp the static URL prefix so `url_for('static', …)` (and the
        # relative ES-module imports resolved beneath it) change on every build —
        # the cache-busting seam. Assets under it are served immutable; the HTML
        # shell (ui.py) is no-cache so it always re-reads the current prefix.
        app.mount(
            f"/static/{_asset_token()}",
            _ImmutableStaticFiles(directory=str(_STATIC), check_dir=False),
            name="static",
        )

    # Stash for tests / introspection.
    app.state.agents = agents
    app.state.store = chat_store
    app.state.session = chat_store.session
    app.state.approvals = approvals
    app.state.context_policy = context_policy
    app.state.tracer = tracer
    app.state.deps = deps
    return app


def serve(
    agent_or_agents: "Agent[Any] | Mapping[str, Agent[Any]]",
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    db_path: str | Path | None = None,
    session: Session | None = None,
    store: ChatStore | None = None,
    context_policy: ContextPolicy | None = None,
    title_model: str | Provider | None = None,
    generate_titles: bool = True,
    title: str = "lovia",
    max_turns: int = 50,
    budget: RunBudget | None = None,
    retry: RetryPolicy | None = None,
    tracer: Tracer | None = None,
    approval_timeout: float | None = None,
    ui: bool = True,
    cors_origins: Sequence[str] | None = None,
    token: str | None = None,
    auth: Any = None,
    empty_title: str = "Where shall we begin?",
    empty_description: str | Sequence[str] | None = None,
    empty_examples: Sequence[str] | None = None,
    **uvicorn_kwargs: Any,
) -> None:
    """Convenience: build the app and run it under uvicorn (blocking).

    ``max_turns`` / ``budget`` set the per-request run limits and ``retry``
    overrides the agent's retry posture (see :func:`create_app`); ``ui=False``
    serves the JSON + SSE API only; any remaining keyword arguments are
    forwarded to ``uvicorn.run`` (e.g. ``log_level``, ``reload``, ``workers``).

    Safe by default off-loopback: binding a non-loopback ``host`` with neither
    ``token`` nor ``auth`` generates a token and prints it (with a ready
    ``/?token=...`` UI link) — the API is never exposed unauthenticated.
    Loopback binds stay credential-free unless a ``token`` is passed.
    """
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - depends on optional env
        from ._deps import raise_missing_web_extra

        raise_missing_web_extra(exc)

    token = _clean_token(token)  # "" must not skip the generation below
    ui_url = f"http://{_display_host(host)}:{port}/?token="
    if token is None and auth is None and not is_loopback(host):
        token = generate_token()
        # stdout on purpose: this must be visible at every log level — it is
        # the only copy of the credential.
        print(
            f"web API token (generated): {token}\n"
            f"  fix it with serve(token=...), --token, or LOVIA_WEB_TOKEN\n"
            f"  UI: {ui_url}{token}",
            flush=True,
        )
    elif token:
        print(f"web API auth enabled — UI: {ui_url}{token}", flush=True)

    app = create_app(
        agent_or_agents,
        db_path=db_path,
        session=session,
        store=store,
        context_policy=context_policy,
        title_model=title_model,
        generate_titles=generate_titles,
        title=title,
        max_turns=max_turns,
        budget=budget,
        retry=retry,
        tracer=tracer,
        approval_timeout=approval_timeout,
        ui=ui,
        cors_origins=cors_origins,
        token=token,
        auth=auth,
        empty_title=empty_title,
        empty_description=empty_description,
        empty_examples=empty_examples,
    )
    uvicorn.run(app, host=host, port=port, **uvicorn_kwargs)
