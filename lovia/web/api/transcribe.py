"""The voice-input route: one recorded take in, its text out."""

from __future__ import annotations

import logging

try:
    from fastapi import APIRouter, Request
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ...exceptions import LoviaError
from ..errors import WebError
from ..schemas import Transcription
from ..transcribe import TranscriptionRequest
from .deps import RouterDeps
from .routing import GuardedRoute

log = logging.getLogger(__name__)

# Five minutes of 16 kHz mono 16-bit WAV — the page's own limit — is 9.6 MB.
MAX_AUDIO_BYTES = 10 * 1024 * 1024
_SETTINGS_HINT = "check the voice input settings"


def build_transcribe_router(deps: RouterDeps) -> APIRouter:
    router = APIRouter(route_class=GuardedRoute)

    @router.post("/api/transcribe", response_model=Transcription)
    async def transcribe(request: Request) -> Transcription:
        """Transcribe one take: the body is a WAV file, ``audio/wav``.

        A raw body, not a form: nothing is spooled to disk, and the cap
        holds while it arrives. The media type is not one a page may send
        cross-site without a preflight, so no other site can spend the
        server's key through a visitor's browser.
        """
        fn = deps.transcriber()
        if fn is None:
            raise WebError(404, "feature_unavailable", "voice input is not configured")
        media_type = request.headers.get("content-type", "").partition(";")[0]
        if media_type.strip().lower() != "audio/wav":
            raise WebError(
                415, "unsupported_file_type", "the take must be sent as audio/wav"
            )
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_AUDIO_BYTES:
            raise WebError(413, "file_too_large", "the take is too long")
        audio = bytearray()
        async for chunk in request.stream():
            audio.extend(chunk)
            if len(audio) > MAX_AUDIO_BYTES:
                raise WebError(413, "file_too_large", "the take is too long")
        if not audio:
            raise WebError(422, "invalid_request", "empty take")
        try:
            text = await fn(TranscriptionRequest(audio=bytes(audio)))
        except Exception as exc:  # noqa: BLE001 — any engine, any failure
            # Never upstream's own status: the page reads a 401 as "signed
            # out of lovia", and it was the engine's key that was refused.
            log.warning("transcription failed: %s", exc)
            if isinstance(exc, LoviaError):
                message, hint = Exception.__str__(exc), exc.hint
            else:
                message, hint = str(exc), None
            raise WebError(
                502,
                "transcription_failed",
                message or type(exc).__name__,
                hint=hint or _SETTINGS_HINT,
            ) from exc
        return Transcription(text=str(text).strip())

    return router
