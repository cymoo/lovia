"""Bearer-token authentication for the web API.

The default posture is friction-free locally and safe by default remotely:
binding to loopback needs no credentials, while ``serve()`` refuses to expose
the API on a non-loopback host without a token — generating and printing one
when the caller didn't provide it.

One token, two carriers:

* ``Authorization: Bearer <token>`` — API clients and the bundled UI's
  ``fetch`` calls (SSE included: the UI consumes streams via ``fetch``).
* the ``lovia_token`` cookie — set client-side when the UI is opened through
  its ``/?token=...`` link. Requests the browser makes *without* JS headers
  (``<img>`` previews, download links) carry credentials this way.

``/healthz`` stays open for probes. The static assets and the UI shell are
public by design — they contain no data; every ``/api/*`` route is guarded.

For any richer scheme (sessions, OAuth, per-user identity), pass a FastAPI
dependency as ``create_app(auth=...)`` instead of a token — it replaces this
module's check wholesale.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable

try:
    from fastapi import HTTPException, Request
except ImportError as exc:  # pragma: no cover - depends on optional env
    from ._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

#: Cookie the bundled UI stores its token in (see ``static/js/main.js``).
TOKEN_COOKIE = "lovia_token"

#: Paths exempt from token auth — health probes must not need credentials.
OPEN_PATHS = frozenset({"/healthz"})


def is_loopback(host: str) -> bool:
    """True for loopback binds. NB: "0.0.0.0" / "::" are wildcards, not loopback."""
    return host in {"127.0.0.1", "localhost", "::1"} or host.startswith("127.")


def generate_token() -> str:
    """A fresh URL-safe token (~32 chars, 24 bytes of entropy)."""
    return secrets.token_urlsafe(24)


def token_dependency(token: str) -> Callable[[Request], Awaitable[None]]:
    """A FastAPI dependency that rejects requests lacking ``token``.

    Accepts the token from the ``Authorization: Bearer`` header first, then
    the :data:`TOKEN_COOKIE` cookie. Comparison is constant-time.
    """
    if not token:
        raise ValueError("token must be non-empty")

    def supplied(request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
        return request.cookies.get(TOKEN_COOKIE)

    async def dependency(request: Request) -> None:
        if request.url.path in OPEN_PATHS:
            return
        candidate = supplied(request)
        if candidate is not None and secrets.compare_digest(candidate, token):
            return
        raise HTTPException(
            status_code=401,
            # "server token" (not just "unauthorized") so clients — including
            # the bundled UI's error mapping — can tell this apart from a
            # model-provider auth failure.
            detail="missing or invalid server token: pass it as "
            "'Authorization: Bearer <token>', or open the UI via its "
            "/?token=... link",
            headers={"www-authenticate": "Bearer"},
        )

    return dependency
