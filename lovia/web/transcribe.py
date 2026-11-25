"""Speech to text for the composer's voice input.

The page records a take, converts it to WAV and posts it to
``POST /api/transcribe``; the server hands it to a :data:`TranscribeFn` and
returns the text. Nothing is written to disk and the engine's key never
reaches the browser.

``create_app(transcribe=...)`` takes any :data:`TranscribeFn`.
:func:`openai_transcriber` is the built-in one, for an OpenAI-compatible
``/audio/transcriptions`` endpoint::

    from lovia.web import create_app
    from lovia.web.transcribe import openai_transcriber

    create_app(agent, transcribe=openai_transcriber(
        base_url="https://api.openai.com/v1",
        api_key=os.environ["OPENAI_API_KEY"],
        model="gpt-transcribe",
    ))
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from ..exceptions import LoviaError
from ..http_config import resolve_trust_env, resolve_verify

__all__ = [
    "TranscribeFn",
    "TranscriptionError",
    "TranscriptionRequest",
    "openai_transcriber",
]

# Upstream's own words, shortened: enough to act on, never a page of HTML.
_DETAIL_MAX = 200


@dataclass(frozen=True)
class TranscriptionRequest:
    """One take to transcribe. ``audio`` is a WAV file (the bundled page
    sends 16 kHz mono 16-bit PCM)."""

    audio: bytes


TranscribeFn = Callable[[TranscriptionRequest], Awaitable[str]]
"""A transcriber: takes a take, returns its text (possibly empty)."""


class TranscriptionError(LoviaError):
    """The engine refused the take, or could not be reached."""


def _detail(response: httpx.Response) -> str:
    """Why upstream refused: its error message if it sent one, else the body."""
    try:
        error = response.json().get("error")
        text = error.get("message") if isinstance(error, dict) else error
    except (ValueError, AttributeError):
        text = None
    return " ".join(str(text or response.text).split())


def openai_transcriber(
    *,
    base_url: str,
    model: str,
    api_key: str | None = None,
    language: str | None = None,
    timeout: float = 60.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> TranscribeFn:
    """A :data:`TranscribeFn` for an OpenAI-compatible endpoint.

    ``base_url`` is the API root (``.../v1``); the take goes to
    ``{base_url}/audio/transcriptions``. ``language`` is an ISO 639-1 code,
    or ``None`` to let the model detect it. TLS and proxy settings follow
    the providers' (``LOVIA_HTTP_*``, ``LOVIA_PROVIDER_TRUST_ENV``). One
    attempt, no retry: the person is waiting, and can press retry.
    """
    url = f"{base_url.rstrip('/')}/audio/transcriptions"
    fields: dict[str, str] = {"model": model}
    if language:
        # gpt-transcribe replaced ``language`` with a list, and refuses both.
        key = "languages[]" if "gpt-transcribe" in model else "language"
        fields[key] = language
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def redact(text: str) -> str:
        # The key first, the cut second: a cut through the key would leave
        # a prefix no replace could find.
        text = text.replace(api_key, "***") if api_key else text
        return text[:_DETAIL_MAX]

    async def transcribe(request: TranscriptionRequest) -> str:
        if api_key and not (api_key.isascii() and api_key.isprintable()):
            # Refused here, in fixed words: the HTTP stack would refuse it
            # too, quoting the key in a spelling no redaction can match.
            raise TranscriptionError(
                "the API key has a character a header cannot carry",
                hint="paste the key again: a line break or a smart quote got in",
            )
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                transport=transport,
                trust_env=resolve_trust_env(None),
                verify=resolve_verify(),
            ) as client:
                response = await client.post(
                    url,
                    headers=headers,
                    data=fields,
                    files={"file": ("audio.wav", request.audio, "audio/wav")},
                )
        except httpx.TimeoutException as exc:
            raise TranscriptionError(
                f"the transcription service did not answer in {timeout:g}s"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — see below
            # Not only transport errors: a key with a character no header
            # can carry fails inside httpx with the key in its message, and
            # nothing may leave here unredacted.
            raise TranscriptionError(
                f"could not reach the transcription service: "
                f"{redact(str(exc)) or type(exc).__name__}"
            ) from exc
        if not response.is_success:
            raise TranscriptionError(
                f"the transcription service answered HTTP {response.status_code}: "
                f"{redact(_detail(response))}"
            )
        try:
            text = response.json()["text"]
        except (ValueError, KeyError, TypeError) as exc:
            raise TranscriptionError(
                "the transcription service answered without a text"
            ) from exc
        if not isinstance(text, str):
            raise TranscriptionError(
                "the transcription service answered without a text"
            )
        return text

    return transcribe
