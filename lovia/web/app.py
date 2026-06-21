"""FastAPI application factory + convenience launcher."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Mapping

try:
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles
except ImportError as exc:  # pragma: no cover - depends on optional env
    from ._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ..agent import Agent
from ..context import Compaction, ContextPolicy
from ..providers import Provider
from ..reliability import RetryPolicy, RunBudget
from ..session import Session
from ..tracing import Tracer
from .api import RouterDeps, build_api_router
from .approvals import ApprovalRegistry
from .store import ChatStore
from .ui import build_ui_router

_STATIC = Path(__file__).parent / "static"

_DEFAULT_MAX_TOKENS = 65_536  # 64K


def _normalise(
    agent_or_agents: "Agent[Any] | Mapping[str, Agent[Any]]",
) -> dict[str, Agent[Any]]:
    if isinstance(agent_or_agents, Mapping):
        return dict(agent_or_agents)
    return {agent_or_agents.name: agent_or_agents}


def _default_db_path(agents: dict[str, Agent[Any]]) -> Path:
    """Derive a SQLite filename from the first agent's name."""
    name = next(iter(agents), "lovia")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return Path(f"{safe}.db")


def create_app(
    agent_or_agents: "Agent[Any] | Mapping[str, Agent[Any]]",
    *,
    db_path: str | Path | None = None,
    session: Session | None = None,
    store: ChatStore | None = None,
    context_policy: ContextPolicy | None = None,
    title_model: str | Provider | list[str | Provider] | None = None,
    generate_titles: bool = True,
    title: str = "lovia",
    max_turns: int = 50,
    budget: RunBudget | None = None,
    retry: RetryPolicy | None = None,
    tracer: Tracer | None = None,
    ui: bool = True,
    empty_title: str = "Wake up, Neo.",
    empty_description: str | Sequence[str] | None = None,
) -> FastAPI:
    """Build a FastAPI app that exposes the given agent(s).

    Storage precedence (highest first):

    * ``store`` — fully-formed :class:`ChatStore`.
    * ``db_path`` — persist transcripts + metadata to a SQLite file.
    * ``session`` — bring-your-own :class:`Session`; metadata kept in-memory.
    * neither — default: SQLite file named ``<agent_name>.db``.

    ``title_model`` overrides the model used to generate chat titles; defaults
    to the first agent's own ``model``.

    ``context_policy`` defaults to :class:`Compaction` with a
    64 K token cap.  Pass ``NoopContextPolicy()`` to disable compaction.

    Run limits apply to every chat turn the server drives: ``max_turns`` caps
    the agent loop per request, ``budget`` (a :class:`RunBudget`) bounds token
    spend, and ``retry`` (a :class:`RetryPolicy`) governs provider retries.

    ``ui`` controls the bundled single-page chat UI: when ``True`` (default) the
    app also serves ``GET /`` and ``/static``; set it to ``False`` for a pure
    JSON + SSE server you drive from your own front-end (see
    :func:`lovia.web.build_api_router`).

    ``empty_title`` and ``empty_description`` customize the blank chat state;
    ``empty_description`` may be a string or a list of short lines.
    """
    agents = _normalise(agent_or_agents)

    if store is not None:
        chat_store = store
    elif db_path is not None:
        chat_store = ChatStore.sqlite(db_path)
    elif session is not None:
        chat_store = ChatStore(session, meta_path=":memory:")
    else:
        chat_store = ChatStore.sqlite(_default_db_path(agents))

    effective_policy: ContextPolicy = context_policy or Compaction(
        context_window=_DEFAULT_MAX_TOKENS
    )

    approvals = ApprovalRegistry()

    deps = RouterDeps(
        agents=agents,
        store=chat_store,
        approvals=approvals,
        title=title,
        context_policy=effective_policy,
        title_model=title_model,
        generate_titles=generate_titles,
        max_turns=max_turns,
        budget=budget,
        retry=retry,
        tracer=tracer,
    )

    app = FastAPI(title=title, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.include_router(build_api_router(deps))
    if ui:
        app.include_router(
            build_ui_router(
                title=title,
                empty_title=empty_title,
                empty_description=empty_description,
            )
        )
        app.mount(
            "/static",
            StaticFiles(directory=str(_STATIC), check_dir=False),
            name="static",
        )

    # Stash for tests / introspection.
    app.state.agents = agents
    app.state.store = chat_store
    app.state.session = chat_store.session
    app.state.approvals = approvals
    app.state.context_policy = effective_policy
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
    title_model: str | Provider | list[str | Provider] | None = None,
    generate_titles: bool = True,
    title: str = "lovia",
    max_turns: int = 50,
    budget: RunBudget | None = None,
    retry: RetryPolicy | None = None,
    tracer: Tracer | None = None,
    ui: bool = True,
    empty_title: str = "Wake up, Neo.",
    empty_description: str | Sequence[str] | None = None,
    **uvicorn_kwargs: Any,
) -> None:
    """Convenience: build the app and run it under uvicorn (blocking).

    ``max_turns`` / ``budget`` / ``retry`` set the per-request run limits (see
    :func:`create_app`); ``ui=False`` serves the JSON + SSE API only; any
    remaining keyword arguments are forwarded to ``uvicorn.run`` (e.g.
    ``log_level``, ``reload``, ``workers``).
    """
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - depends on optional env
        from ._deps import raise_missing_web_extra

        raise_missing_web_extra(exc)

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
        ui=ui,
        empty_title=empty_title,
        empty_description=empty_description,
    )
    uvicorn.run(app, host=host, port=port, **uvicorn_kwargs)
