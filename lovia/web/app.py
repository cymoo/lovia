"""FastAPI application factory + convenience launcher."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

try:
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles
except ImportError as exc:  # pragma: no cover - depends on optional env
    from ._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ..agent import Agent
from ..context_policy import ContextPolicy
from ..session import Session
from .approvals import ApprovalRegistry
from .routes import build_router
from .store import ChatStore

_STATIC = Path(__file__).parent / "static"


def _normalise(
    agent_or_agents: "Agent[Any] | Mapping[str, Agent[Any]]",
) -> dict[str, Agent[Any]]:
    if isinstance(agent_or_agents, Mapping):
        return dict(agent_or_agents)
    return {agent_or_agents.name: agent_or_agents}


def create_app(
    agent_or_agents: "Agent[Any] | Mapping[str, Agent[Any]]",
    *,
    db_path: str | Path | None = None,
    session: Session | None = None,
    store: ChatStore | None = None,
    context_policy: ContextPolicy | None = None,
    title_model: Any = None,
    generate_titles: bool = True,
    title: str = "lovia",
) -> FastAPI:
    """Build a FastAPI app that exposes the given agent(s).

    Storage precedence (highest first):

    * ``store`` — fully-formed :class:`ChatStore`.
    * ``db_path`` — persist transcripts + metadata to a SQLite file.
    * ``session`` — bring-your-own :class:`Session`; metadata kept in-memory.
    * neither — pure in-memory chats (lost on restart). Backward-compatible
      with the old signature.

    ``title_model`` overrides the model used to generate chat titles; defaults
    to the first agent's own ``model``.
    """
    agents = _normalise(agent_or_agents)

    if store is not None:
        chat_store = store
    elif db_path is not None:
        chat_store = ChatStore.sqlite(db_path)
    elif session is not None:
        chat_store = ChatStore(session, meta_path=":memory:")
    else:
        chat_store = ChatStore.in_memory()

    approvals = ApprovalRegistry()

    app = FastAPI(title=title, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.include_router(
        build_router(
            agents,
            chat_store,
            approvals,
            context_policy=context_policy,
            title_model=title_model,
            generate_titles=generate_titles,
            title=title,
        )
    )
    app.mount(
        "/static", StaticFiles(directory=str(_STATIC), check_dir=False), name="static"
    )

    # Stash for tests / introspection.
    app.state.agents = agents
    app.state.store = chat_store
    app.state.session = chat_store.session
    app.state.approvals = approvals
    app.state.context_policy = context_policy
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
    title_model: Any = None,
    generate_titles: bool = True,
    title: str = "lovia",
    **uvicorn_kwargs: Any,
) -> None:
    """Convenience: build the app and run it under uvicorn (blocking)."""
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
    )
    uvicorn.run(app, host=host, port=port, **uvicorn_kwargs)
