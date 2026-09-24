"""Optional web layer: serve any lovia agent over HTTP + SSE plus a chat UI.

Install with ``pip install lovia[web]``. The module is fully decoupled from
``lovia`` core: nothing in ``lovia`` imports ``lovia.web`` automatically, so
agents that don't need HTTP keep their lightweight dependency footprint.

Public surface::

    from lovia.web import serve, create_app

    serve(agent)                       # single agent on 127.0.0.1:8000
    serve({"writer": a, "researcher": b})

    app = create_app(agents, db_path="chats.db")  # every option lives here
    serve(app, host="0.0.0.0")         # ... or run it with any ASGI server
    app = create_app(agents, ui=False) # JSON + SSE only — bring your own UI
    app = create_app(agent, ui=ChatUI(empty_title="Ask me"))  # customize it

Bring your own UI: mount the UI-free API router into your own FastAPI app::

    from fastapi import FastAPI
    from lovia.web import RouterDeps, build_api_router, ChatStore

    deps = RouterDeps(agents={"bot": agent}, store=ChatStore.in_memory())
    app = FastAPI(lifespan=deps.lifespan)  # or enter it inside your own
    app.include_router(build_api_router(deps))
"""

from __future__ import annotations

try:
    from .api import RouterDeps, build_api_router
    from .app import create_app, serve
    from .auth import generate_token, is_loopback, token_dependency
    from .followups import FollowupFn, FollowupRequest, generate_followups
    from .questions import QuestionRegistry
    from .scheduling import Scheduling
    from .store import ChatMeta, ChatStore
    from .subagents import subagent_deliver, wire_subagents
    from .ui import SURFACE_NOTE, ChatUI
except ImportError as exc:  # pragma: no cover - depends on optional env
    from ._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

__all__ = [
    "ChatMeta",
    "ChatStore",
    "ChatUI",
    "FollowupFn",
    "FollowupRequest",
    "QuestionRegistry",
    "RouterDeps",
    "SURFACE_NOTE",
    "Scheduling",
    "build_api_router",
    "create_app",
    "generate_followups",
    "generate_token",
    "is_loopback",
    "serve",
    "subagent_deliver",
    "token_dependency",
    "wire_subagents",
]
