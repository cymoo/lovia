"""Machine-readable errors: the two code vocabularies of the web API.

Every error an API route raises answers with the same body::

    {"detail": {"code": "session_not_found", "message": "...", "hint": "..."}}

``code`` is one of :data:`ErrorCode` — branch on it, never on ``message``
wording. ``hint`` is present only when there is a suggested fix. The shape
rides FastAPI's default ``HTTPException`` handler, so it survives mounting
:func:`~lovia.web.build_api_router` into any app, no handler to install.

Errors raised outside these routes keep FastAPI's own shapes: request
validation answers 422 with ``detail`` as a list, and a custom ``auth=``
dependency answers however it chooses.

A failure *inside* a run arrives instead as the chat stream's ``error``
event, classified by :func:`run_error_code` into a :data:`RunErrorCode`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

import httpx

try:
    from fastapi import HTTPException
except ImportError as exc:  # pragma: no cover - depends on optional env
    from ._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ..exceptions import (
    BudgetExceeded,
    ContextOverflowError,
    GuardrailTripped,
    LoviaError,
    MaxTurnsExceeded,
    OutputValidationError,
    ProviderError,
    RunCancelled,
)

ErrorCode = Literal[
    "invalid_request",
    "server_token",
    "local_origin_required",
    "path_denied",
    "agent_not_found",
    "session_not_found",
    "schedule_not_found",
    "model_not_found",
    "file_not_found",
    "turn_not_found",
    "image_not_found",
    "approval_not_found",
    "question_not_found",
    "process_not_found",
    "run_not_found",
    "feature_unavailable",
    "run_active",
    "run_stopping",
    "agent_unregistered",
    "schedule_not_fired",
    "model_exists",
    "model_in_use",
    "file_too_large",
    "unsupported_file_type",
    "too_many_runs",
]


class WebError(HTTPException):
    """An API error carrying a stable :data:`ErrorCode`.

    Still an ``HTTPException``: code that branches on ``status_code`` keeps
    working.
    """

    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        message: str,
        *,
        hint: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        detail: dict[str, str] = {"code": code, "message": message}
        if hint:
            detail["hint"] = hint
        super().__init__(
            status_code, detail=detail, headers=dict(headers) if headers else None
        )
        self.code = code

    @classmethod
    def from_exc(
        cls, status_code: int, code: ErrorCode, exc: BaseException
    ) -> WebError:
        """Wrap ``exc``, lifting a :class:`~lovia.exceptions.LoviaError` hint
        into its own field instead of leaving it folded into the message."""
        if isinstance(exc, LoviaError):
            return cls(status_code, code, Exception.__str__(exc), hint=exc.hint)
        return cls(status_code, code, str(exc))


RunErrorCode = Literal[
    "tool_error",
    "provider_auth",
    "rate_limited",
    "overloaded",
    "timeout",
    "network",
    "provider_error",
    "context_overflow",
    "budget_exceeded",
    "max_turns",
    "cancelled",
    "guardrail",
    "output_invalid",
    "internal",
]

_STATUS_CODES: dict[int, RunErrorCode] = {
    401: "provider_auth",
    403: "provider_auth",
    408: "timeout",
    429: "rate_limited",
    503: "overloaded",
    504: "timeout",
    529: "overloaded",
}


def run_error_code(exc: BaseException) -> RunErrorCode:
    """Classify an exception that ended a run.

    A provider error maps by its HTTP status, or — when the request never got
    one — by the transport failure the adapter chained as ``__cause__``.
    """
    if isinstance(exc, ContextOverflowError):
        return "context_overflow"
    if isinstance(exc, ProviderError):
        if exc.status_code is not None:
            return _STATUS_CODES.get(exc.status_code, "provider_error")
        if isinstance(exc.__cause__, httpx.TimeoutException):
            return "timeout"
        if isinstance(exc.__cause__, httpx.TransportError):
            return "network"
        return "provider_error"
    if isinstance(exc, BudgetExceeded):
        return "budget_exceeded"
    if isinstance(exc, MaxTurnsExceeded):
        return "max_turns"
    if isinstance(exc, RunCancelled):
        return "cancelled"
    if isinstance(exc, GuardrailTripped):
        return "guardrail"
    if isinstance(exc, OutputValidationError):
        return "output_invalid"
    return "internal"
