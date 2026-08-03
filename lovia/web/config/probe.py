"""Probing a model endpoint: reachability, auth, model list, context window.

Shared by the ``--check`` diagnosis and the web UI's "test connection"
button, so every front-end reports identical outcomes.
"""

from __future__ import annotations

import difflib
import enum

import httpx

from ...http_config import resolve_trust_env, resolve_verify
from ...providers import Provider, provider_from_string
from ...providers._windows import window_from_models_payload
from ...providers.base import context_window as provider_context_window
from .schema import Connection


class ValidationOutcome(enum.Enum):
    OK = "ok"
    AUTH_FAILED = "auth_failed"
    UNREACHABLE = "unreachable"
    UNVERIFIABLE = "unverifiable"


def validate_connection(
    conn: Connection,
    *,
    timeout: float = 10.0,
    transport: httpx.BaseTransport | None = None,
) -> tuple[ValidationOutcome, str]:
    """Probe ``GET {base_url}/models`` and classify the response.

    Only called for freshly entered values — configured launches never pay
    for this request. A successful body doubles as a context-window source:
    vLLM, SGLang, OpenRouter, Groq and Together publish the model's window
    there, so setup need not ask for a number the endpoint knows.
    """
    assert conn.base_url is not None and conn.flavor is not None
    try:
        with httpx.Client(
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
            trust_env=resolve_trust_env(None),
            verify=resolve_verify(),
        ) as client:
            response = client.get(
                f"{conn.base_url}/models",
                headers=conn.flavor.auth_headers(conn.api_key),
            )
    except httpx.TransportError as exc:
        return ValidationOutcome.UNREACHABLE, str(exc) or type(exc).__name__
    if response.status_code in (401, 403):
        return ValidationOutcome.AUTH_FAILED, f"HTTP {response.status_code}"
    if response.is_success:
        _adopt_reported_window(conn, response)
        conn.available_models = _listed_model_ids(response)
        return ValidationOutcome.OK, f"HTTP {response.status_code}"
    return ValidationOutcome.UNVERIFIABLE, f"HTTP {response.status_code}"


def _listed_model_ids(response: httpx.Response) -> list[str] | None:
    """Model ids from a ``/models`` body; None when the shape is foreign."""
    try:
        payload = response.json()
    except ValueError:
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    ids = [
        entry["id"]
        for entry in data
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    ]
    return ids or None


def _adopt_reported_window(conn: Connection, response: httpx.Response) -> None:
    """Take the window from a ``/models`` body, unless one is configured.

    Marked ``window_from_endpoint`` so the value serves this launch but is
    never persisted — a deployment fact, not a configuration choice.
    """
    if conn.context_window is not None or conn.model is None:
        return
    try:
        payload = response.json()
    except ValueError:
        return  # not every /models endpoint answers with JSON
    window = window_from_models_payload(payload, conn.model)
    if window is not None:
        conn.context_window = window
        conn.window_from_endpoint = True


def unlisted_model_note(conn: Connection) -> str | None:
    """A warn-only line when the endpoint's ``/models`` omits the model.

    Soft on purpose: gateways often list only part of what they serve (or
    nothing), so an absent id is a hint, not an error — but it catches the
    typo that would otherwise surface as a failure on the first chat message.
    """
    ids = conn.available_models
    if not ids or conn.model is None:
        return None
    bare = conn.model.split(":", 1)[-1]
    if conn.model in ids or bare in ids:
        return None
    close = difflib.get_close_matches(bare, ids, n=3, cutoff=0.6)
    hint = f" — close: {', '.join(close)}" if close else ""
    return (
        f"note: the endpoint does not list {bare!r}{hint} (gateways don't "
        "always list every model; continuing)"
    )


def build_provider(conn: Connection) -> Provider | None:
    """Construct the provider for ``conn`` (cheap, no I/O); None if unknown."""
    if conn.model is None:
        return None
    try:
        return provider_from_string(
            conn.model, api_key=conn.api_key, base_url=conn.base_url
        )
    except ValueError:
        return None


def known_context_window(conn: Connection) -> int | None:
    """The window the provider can name without I/O, if it can name one.

    That is an explicit setting, whatever the endpoint has already told this
    process, or the bundled table — never a fresh network probe.
    """
    provider = build_provider(conn)
    if provider is None:
        return None
    return provider_context_window(provider)
