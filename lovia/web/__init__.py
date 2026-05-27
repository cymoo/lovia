"""Optional web layer: serve any lovia agent over HTTP + SSE plus a chat UI.

Install with ``pip install lovia[web]``. The module is fully decoupled from
``lovia`` core — importing it pulls in FastAPI/uvicorn lazily.

Public surface::

    from lovia.web import serve, create_app

    serve(agent)                       # single agent on 127.0.0.1:8000
    serve({"writer": a, "researcher": b})

    app = create_app(agents)           # raw ASGI app — run with any server
"""

from __future__ import annotations

from .app import create_app, serve

__all__ = ["create_app", "serve"]
