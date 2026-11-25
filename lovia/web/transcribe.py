"""Speech to text for the composer's voice input.

The page records a take, converts it to WAV and posts it to
``POST /api/transcribe``; the server hands it to a :data:`TranscribeFn` and
returns the text. Nothing is written to disk and the engine's key never
reaches the browser.

``create_app(transcribe=...)`` takes any :data:`TranscribeFn`.
:func:`openai_transcriber` is the built-in one, for an OpenAI-compatible
``/audio/transcriptions`` endpoint, or any that takes the same upload (a
``file`` form field, answered with ``{"text": ...}``)::

    from lovia.web import create_app
    from lovia.web.transcribe import openai_transcriber

    create_app(agent, transcribe=openai_transcriber(
        base_url="https://api.openai.com/v1",
        api_key=os.environ["OPENAI_API_KEY"],
        model="gpt-transcribe",
    ))
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..exceptions import LoviaError
from ..http_config import resolve_trust_env, resolve_verify

__all__ = [
    "TranscribeFn",
    "TranscriptionError",
    "TranscriptionRequest",
    "openai_transcriber",
    "transcription_endpoint",
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


OPENAI_PATH = "/audio/transcriptions"
# Services whose transcription is not at the OpenAI path of their API root:
# host -> (path, longest take in seconds).
_SERVICES: dict[str, tuple[str, int]] = {
    "api.deepseek.com": ("/v1/tools/asr", 120),
}
# The last path segment of an API root: none, or a version (``v1``, ``v4``).
# or the ``openai`` a gateway hangs its compatible API under.
_ROOT = re.compile(r"(^|/)(v\d+[\w.]*|openai)$", re.IGNORECASE)


def transcription_endpoint(
    base_url: str, *, root: bool = False
) -> tuple[str, int | None]:
    """``(url, longest take in seconds or None)`` for a typed address.

    One field takes both spellings: an API root (no path, or one ending in
    a version) gets the transcription path of its service, anything else is
    the endpoint itself. ``root=True`` says it is a root whatever it looks
    like — a chat model's connection always is. Resolving an address twice
    gives the same answer.
    """
    try:
        parts = urlsplit(base_url.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        # Not an address at all (``https://[oops``). As typed: the take
        # fails saying so, and nothing that only asks where it goes does.
        return base_url.strip(), None
    path = parts.path.rstrip("/")
    service = _SERVICES.get(host)
    limit = service[1] if service else None
    if path.endswith(OPENAI_PATH) or (service and path == service[0]):
        pass
    elif root or not path or _ROOT.search(path):
        path = service[0] if service else path + OPENAI_PATH
    return urlunsplit(parts._replace(path=path)), limit


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
    model: str | None = None,
    api_key: str | None = None,
    language: str | None = None,
    timeout: float = 60.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> TranscribeFn:
    """A :data:`TranscribeFn` for an OpenAI-compatible endpoint.

    ``base_url`` is the API root (``.../v1``: the take goes to
    ``{base_url}/audio/transcriptions``) or the endpoint's full address
    (``https://api.deepseek.com/v1/tools/asr``), told apart by
    :func:`transcription_endpoint`. ``model`` may be left out for a service
    that has only one. ``language`` is an ISO 639-1 code, or ``None`` to
    let the model detect it. TLS and proxy settings follow
    the providers' (``LOVIA_HTTP_*``, ``LOVIA_PROVIDER_TRUST_ENV``). One
    attempt, no retry: the person is waiting, and can press retry.
    """
    url, _ = transcription_endpoint(base_url)
    # Neither route is one lovia chose: the address was used as typed.
    as_typed = transcription_endpoint(url, root=True)[0] != url
    fields: dict[str, str] = {"model": model} if model else {}
    if language:
        # gpt-transcribe replaced ``language`` with a list, and refuses both.
        key = "languages[]" if "gpt-transcribe" in (model or "") else "language"
        fields[key] = language
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def redact(text: str) -> str:
        # The key first, the cut second: a cut through the key would leave
        # a prefix no replace could find.
        text = text.replace(api_key, "***") if api_key else text
        return text[:_DETAIL_MAX]

    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            trust_env=resolve_trust_env(None),
            verify=resolve_verify(),
        )

    async def key_refused() -> str | None:
        """The service's word that the key is wrong, asked without the take.

        A service that judges the key before the upload ends closes the
        connection under it, and what arrives is a reset or a gateway's
        empty error (DeepSeek: HTTP 550) instead of its 401. The same
        request with no body is small enough to get the real answer. Only
        a 401 is taken as one: a 403 may be about anything.
        """
        try:
            async with client() as http:
                probe = await asyncio.wait_for(http.post(url, headers=headers), 10)
        except Exception:  # noqa: BLE001 — a second opinion, never an error
            return None
        if probe.status_code != 401:
            return None
        said = redact(_detail(probe))
        what = "refused the API key" if api_key else "asked for an API key"
        return f"the transcription service {what} (HTTP {probe.status_code})" + (
            f": {said}" if said else ""
        )

    async def transcribe(request: TranscriptionRequest) -> str:
        if api_key and not (api_key.isascii() and api_key.isprintable()):
            # Refused here, in fixed words: the HTTP stack would refuse it
            # too, quoting the key in a spelling no redaction can match.
            raise TranscriptionError(
                "the API key has a character a header cannot carry",
                hint="paste the key again: a line break or a smart quote got in",
            )
        try:
            async with client() as http:
                response = await http.post(
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
            # A connection cut mid-request was reached: ask why.
            cut = isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError))
            cut = cut and not isinstance(exc, httpx.ConnectError)
            raise TranscriptionError(
                (await key_refused() if cut else None)
                or f"could not reach the transcription service: "
                f"{redact(str(exc)) or type(exc).__name__}"
            ) from exc
        if not response.is_success:
            said = redact(_detail(response))
            # A gateway's empty error; a 4xx is the service's own answer.
            unexplained = not said and response.status_code >= 500
            if unexplained and (why := await key_refused()):
                raise TranscriptionError(why)
            raise TranscriptionError(
                f"the transcription service answered HTTP {response.status_code}"
                + (f": {said}" if said else ""),
                # An address taken for the endpoint that nothing answers at
                # was probably an API root of an unusual shape.
                hint=f"if the address is an API root, add {OPENAI_PATH} to it"
                if as_typed and response.status_code in (404, 405)
                else None,
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
