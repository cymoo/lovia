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
from ..stores import InMemorySession
from .approvals import ApprovalRegistry
from .routes import build_router

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
    session: Session | None = None,
    context_policy: ContextPolicy | None = None,
    title: str = "lovia",
) -> FastAPI:
    """Build a FastAPI app that exposes the given agent(s).

    Returns a plain ASGI app — run it with any ASGI server.

    Pass ``context_policy`` (e.g. :class:`~lovia.SummarizingContextPolicy`)
    to keep long-running sessions under the model's context window. The
    same policy instance is shared across all routed agents — handoff is
    transparent to the policy because it operates on the session
    transcript, not on the agent.
    """
    agents = _normalise(agent_or_agents)
    sess: Session = session if session is not None else InMemorySession()
    approvals = ApprovalRegistry()

    app = FastAPI(title=title, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.include_router(
        build_router(agents, sess, approvals, context_policy=context_policy)
    )
    app.mount(
        "/static", StaticFiles(directory=str(_STATIC), check_dir=False), name="static"
    )

    # Stash for tests / introspection.
    app.state.agents = agents
    app.state.session = sess
    app.state.approvals = approvals
    app.state.context_policy = context_policy
    return app


def serve(
    agent_or_agents: "Agent[Any] | Mapping[str, Agent[Any]]",
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    session: Session | None = None,
    context_policy: ContextPolicy | None = None,
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
        session=session,
        context_policy=context_policy,
        title=title,
    )
    uvicorn.run(app, host=host, port=port, **uvicorn_kwargs)
