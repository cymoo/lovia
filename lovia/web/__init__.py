"""Optional web layer: serve any lovia agent over HTTP + SSE plus a chat UI.

Install with ``pip install lovia[web]``. The module is fully decoupled from
``lovia`` core: nothing in ``lovia`` imports ``lovia.web`` automatically, so
agents that don't need HTTP keep their lightweight dependency footprint.

Public surface::

    from lovia.web import serve, create_app

    serve(agent)                       # single agent on 127.0.0.1:8000
    serve({"writer": a, "researcher": b})

    app = create_app(agents)           # raw ASGI app — run with any server
    app = create_app(agents, ui=False) # JSON + SSE only — bring your own UI

Bring your own UI: mount the UI-free API router into your own FastAPI app::

    from fastapi import FastAPI
    from lovia.web import RouterDeps, build_api_router, ChatStore
    from lovia.web.approvals import ApprovalRegistry

    deps = RouterDeps(
        agents={"bot": agent},
        store=ChatStore.in_memory(),
        approvals=ApprovalRegistry(),
    )
    app = FastAPI()
    app.include_router(build_api_router(deps))
"""

from __future__ import annotations

try:
    from .api import RouterDeps, build_api_router
    from .app import create_app, serve
    from .auth import generate_token, token_dependency
    from .scheduling import Scheduling
    from .store import ChatMeta, ChatStore
except ImportError as exc:  # pragma: no cover - depends on optional env
    from ._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

__all__ = [
    "ChatMeta",
    "ChatStore",
    "RouterDeps",
    "Scheduling",
    "build_api_router",
    "create_app",
    "generate_token",
    "serve",
    "token_dependency",
]
