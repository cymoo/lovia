"""FastAPI application factory + convenience launcher."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..agent import Agent
from ..session import Session
from ..stores import InMemorySession
from .approvals import ApprovalRegistry
from .routes import build_router

_STATIC = Path(__file__).parent / "static"


def _normalise(
    agent_or_agents: "Agent[Any, Any] | Mapping[str, Agent[Any, Any]]",
) -> dict[str, Agent[Any, Any]]:
    if isinstance(agent_or_agents, Mapping):
        return dict(agent_or_agents)
    return {agent_or_agents.name: agent_or_agents}


def create_app(
    agent_or_agents: "Agent[Any, Any] | Mapping[str, Agent[Any, Any]]",
    *,
    session: Session | None = None,
    title: str = "lovia",
) -> FastAPI:
    """Build a FastAPI app that exposes the given agent(s).

    Returns a plain ASGI app — run it with any ASGI server.
    """
    agents = _normalise(agent_or_agents)
    sess: Session = session if session is not None else InMemorySession()
    approvals = ApprovalRegistry()

    app = FastAPI(title=title, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.include_router(build_router(agents, sess, approvals))
    app.mount(
        "/static", StaticFiles(directory=str(_STATIC), check_dir=False), name="static"
    )

    # Stash for tests / introspection.
    app.state.agents = agents
    app.state.session = sess
    app.state.approvals = approvals
    return app


def serve(
    agent_or_agents: "Agent[Any, Any] | Mapping[str, Agent[Any, Any]]",
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    session: Session | None = None,
    title: str = "lovia",
    **uvicorn_kwargs: Any,
) -> None:
    """Convenience: build the app and run it under uvicorn (blocking)."""
    import uvicorn

    app = create_app(agent_or_agents, session=session, title=title)
    uvicorn.run(app, host=host, port=port, **uvicorn_kwargs)
