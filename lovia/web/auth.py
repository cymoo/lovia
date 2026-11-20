"""Bearer-token authentication for the web API.

The default posture is friction-free locally and safe by default remotely:
binding to loopback needs no credentials, while ``serve()`` never exposes the
API on a non-loopback host without one — it generates and prints a token for
a bare agent, and refuses an app built without ``token`` or ``auth``.

One token, two carriers:

* ``Authorization: Bearer <token>`` — API clients and the bundled UI's
  ``fetch`` calls (SSE included: the UI consumes streams via ``fetch``).
* the ``lovia_token`` cookie — set client-side when the UI is opened through
  its ``/?token=...`` link. Requests the browser makes *without* JS headers
  (``<img>`` previews, download links) carry credentials this way.

Only the API router is guarded: ``create_app`` serves ``/healthz``, the UI
shell and the static assets outside it — they contain no data.

For any richer scheme (sessions, OAuth, per-user identity), pass a FastAPI
dependency as ``create_app(auth=...)`` instead of a token — it replaces this
module's check wholesale.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

try:
    from fastapi import Request
except ImportError as exc:  # pragma: no cover - depends on optional env
    from ._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from .errors import WebError

#: Cookie the bundled UI stores its token in (see ``static/js/main.js``).
TOKEN_COOKIE = "lovia_token"


def is_loopback(host: str) -> bool:
    """True for loopback binds. NB: "0.0.0.0" / "::" are wildcards, not loopback."""
    return host in {"127.0.0.1", "localhost", "::1"} or host.startswith("127.")


def is_local_name(value: str | None) -> bool:
    """True when a ``Host`` header or an ``Origin`` names this machine.

    Without a token, the Host is what tells a same-machine browser from a
    foreign page whose domain was pointed at 127.0.0.1 (DNS rebinding).
    """
    if not value:
        return False
    netloc = value.split("://", 1)[1] if "://" in value else value
    try:
        name = urlsplit(f"//{netloc}").hostname or ""
    except ValueError:  # a malformed IPv6 literal
        return False
    return name in {"localhost", "127.0.0.1", "::1"} or name.endswith(".localhost")


def generate_token() -> str:
    """A fresh URL-safe token (~32 chars, 24 bytes of entropy)."""
    return secrets.token_urlsafe(24)


def token_dependency(token: str) -> Callable[[Request], Awaitable[None]]:
    """A FastAPI dependency that rejects requests lacking ``token``.

    Accepts the token from the ``Authorization: Bearer`` header first, then
    the :data:`TOKEN_COOKIE` cookie. Comparison is constant-time.
    """
    token = (token or "").strip()
    if not token:
        # A blank token could never be presented (both carriers strip) — it
        # would lock every client out while looking like auth is on.
        raise ValueError("token must be non-empty")

    def supplied(request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
        return request.cookies.get(TOKEN_COOKIE)

    async def dependency(request: Request) -> None:
        candidate = supplied(request)
        if candidate is not None and secrets.compare_digest(candidate, token):
            return
        raise WebError(
            401,
            "server_token",
            "missing or invalid server token",
            hint="pass it as 'Authorization: Bearer <token>', or open the UI "
            "via its /?token=... link",
            headers={"www-authenticate": "Bearer"},
        )

    return dependency
