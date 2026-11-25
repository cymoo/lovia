"""Voice input: ``POST /api/transcribe``, its config, and the built-in engine."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent  # noqa: E402
from lovia.web import create_app  # noqa: E402
from lovia.web.api.transcribe import MAX_AUDIO_BYTES  # noqa: E402
from lovia.web.builder import Deployment  # noqa: E402
from lovia.web.config import (  # noqa: E402
    AssistantConfig,
    ConfigRuntime,
    LoadedConfig,
    WebConfig,
    storage,
)
from lovia.web.store import ChatStore  # noqa: E402
from lovia.web.transcribe import (  # noqa: E402
    TranscriptionError,
    TranscriptionRequest,
    openai_transcriber,
    transcription_endpoint,
)

from ..scripted_provider import ScriptedProvider  # noqa: E402

WAV = {"content-type": "audio/wav"}
TAKE = b"RIFF....WAVEfmt "


def _client(**kw: Any) -> TestClient:
    agent = Agent(name="bot", model=ScriptedProvider([]))
    kw.setdefault("store", ChatStore.in_memory())
    kw.setdefault("titles", False)
    return TestClient(create_app(agent, **kw))


async def _echo(request: TranscriptionRequest) -> str:
    return f"  heard {len(request.audio)} bytes \n"


# ---------------------------------------------------------------- route -


def test_off_by_default() -> None:
    c = _client()
    assert c.get("/api/info").json()["features"]["transcription"] is False
    res = c.post("/api/transcribe", content=TAKE, headers=WAV)
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "feature_unavailable"


def test_a_take_comes_back_as_text() -> None:
    c = _client(transcribe=_echo)
    assert c.get("/api/info").json()["features"]["transcription"] is True
    res = c.post("/api/transcribe", content=TAKE, headers=WAV)
    assert res.status_code == 200, res.text
    assert res.json() == {"text": f"heard {len(TAKE)} bytes"}


@pytest.mark.parametrize(
    "headers", [{"content-type": "text/plain"}, {"content-type": "audio/webm"}, {}]
)
def test_only_wav_is_taken(headers: dict[str, str]) -> None:
    res = _client(transcribe=_echo).post(
        "/api/transcribe", content=TAKE, headers=headers
    )
    assert res.status_code == 415
    assert res.json()["detail"]["code"] == "unsupported_file_type"


def test_an_empty_take_is_refused() -> None:
    res = _client(transcribe=_echo).post("/api/transcribe", content=b"", headers=WAV)
    assert res.status_code == 422


def _scope(headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/transcribe",
        "raw_path": b"/api/transcribe",
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 80),
    }


async def _status(app: Any, scope: dict[str, Any], receive: Any) -> int:
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return int(sent[0]["status"])


async def _never() -> dict[str, Any]:
    raise AssertionError("the body was read")


@pytest.mark.parametrize(
    "kw, headers, status",
    [
        # Refused by the guard, by its type, by its declared length: each
        # before a byte of the take is read.
        ({"token": "sesame"}, [(b"content-type", b"audio/wav")], 401),
        ({}, [(b"content-type", b"audio/webm")], 415),
        (
            {},
            [
                (b"content-type", b"audio/wav"),
                (b"content-length", str(MAX_AUDIO_BYTES + 1).encode()),
            ],
            413,
        ),
    ],
)
async def test_a_refused_take_is_not_read(
    kw: dict[str, Any], headers: list[tuple[bytes, bytes]], status: int
) -> None:
    agent = Agent(name="bot", model=ScriptedProvider([]))
    app = create_app(
        agent, store=ChatStore.in_memory(), titles=False, transcribe=_echo, **kw
    )
    assert await _status(app, _scope(headers), _never) == status


async def test_the_cap_holds_while_an_undeclared_body_arrives() -> None:
    agent = Agent(name="bot", model=ScriptedProvider([]))
    app = create_app(agent, store=ChatStore.in_memory(), titles=False, transcribe=_echo)
    chunk = b"\0" * (1 << 20)
    served = 0

    async def receive() -> dict[str, Any]:
        nonlocal served
        served += 1
        return {"type": "http.request", "body": chunk, "more_body": True}

    scope = _scope([(b"content-type", b"audio/wav")])
    assert await _status(app, scope, receive) == 413
    # Stopped just past the cap, not after an endless stream.
    assert served == MAX_AUDIO_BYTES // len(chunk) + 1


def test_an_engine_failure_is_a_502_never_its_own_status() -> None:
    async def refused(request: TranscriptionRequest) -> str:
        raise TranscriptionError("the transcription service answered HTTP 401: bad key")

    res = _client(transcribe=refused).post("/api/transcribe", content=TAKE, headers=WAV)
    assert res.status_code == 502
    detail = res.json()["detail"]
    assert detail["code"] == "transcription_failed"
    assert "HTTP 401" in detail["message"]
    assert detail["hint"]


def test_a_custom_engines_own_hint_is_kept() -> None:
    async def refused(request: TranscriptionRequest) -> str:
        raise TranscriptionError("no quota", hint="top up the account")

    res = _client(transcribe=refused).post("/api/transcribe", content=TAKE, headers=WAV)
    assert res.json()["detail"] == {
        "code": "transcription_failed",
        "message": "no quota",
        "hint": "top up the account",
    }


# ------------------------------------------------- the built-in engine -


def _engine(handler: Any, **kw: Any) -> Any:
    kw.setdefault("base_url", "https://stt.example/v1/")
    kw.setdefault("model", "whisper-large-v3-turbo")
    kw.setdefault("api_key", "sk-secret-1234567890")
    return openai_transcriber(transport=httpx.MockTransport(handler), **kw)


async def test_the_take_goes_to_audio_transcriptions() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "你好 world"})

    text = await _engine(handler)(TranscriptionRequest(audio=TAKE))
    assert text == "你好 world"
    assert seen["url"] == "https://stt.example/v1/audio/transcriptions"
    assert seen["auth"] == "Bearer sk-secret-1234567890"
    assert TAKE in seen["body"] and b"audio/wav" in seen["body"]
    assert b'name="model"\r\n\r\nwhisper-large-v3-turbo' in seen["body"]
    # No language set: neither spelling is sent, the model detects it.
    assert b"language" not in seen["body"]


@pytest.mark.parametrize(
    "model, sent, unsent",
    [
        ("whisper-large-v3-turbo", b'name="language"\r\n\r\nzh', b"languages[]"),
        ("gpt-transcribe", b'name="languages[]"\r\n\r\nzh', b'name="language"'),
    ],
)
async def test_the_language_field_follows_the_model(
    model: str, sent: bytes, unsent: bytes
) -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, json={"text": "ok"})

    await _engine(handler, model=model, language="zh")(TranscriptionRequest(audio=TAKE))
    assert sent in bodies[0] and unsent not in bodies[0]


async def test_a_keyless_endpoint_gets_no_authorization() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"text": "ok"})

    assert await _engine(handler, api_key=None)(TranscriptionRequest(audio=TAKE))


@pytest.mark.parametrize(
    "response, said",
    [
        (
            httpx.Response(401, json={"error": {"message": "Incorrect API key"}}),
            "HTTP 401: Incorrect API key",
        ),
        (httpx.Response(429, json={"error": "slow down"}), "HTTP 429: slow down"),
        (httpx.Response(500, text="<html>oops</html>"), "HTTP 500: <html>oops"),
        (httpx.Response(200, text="not json"), "without a text"),
        (httpx.Response(200, json={"words": []}), "without a text"),
        (httpx.Response(200, json={"text": None}), "without a text"),
    ],
)
async def test_upstream_failures_say_why(response: httpx.Response, said: str) -> None:
    with pytest.raises(TranscriptionError, match=said):
        await _engine(lambda request: response)(TranscriptionRequest(audio=TAKE))


async def test_transport_failures_say_why() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(TranscriptionError, match="did not answer"):
        await _engine(timeout)(TranscriptionRequest(audio=TAKE))
    with pytest.raises(TranscriptionError, match="could not reach.*refused"):
        await _engine(down)(TranscriptionRequest(audio=TAKE))


async def test_the_key_and_a_long_body_stay_out_of_the_message() -> None:
    key = "sk-secret-1234567890"
    body = f"key {key} is wrong " + "x" * 5000

    with pytest.raises(TranscriptionError) as caught:
        await _engine(lambda request: httpx.Response(400, text=body))(
            TranscriptionRequest(audio=TAKE)
        )
    message = str(caught.value)
    assert key not in message and "***" in message
    assert len(message) < 400


async def test_a_key_cut_by_the_length_limit_leaves_no_prefix() -> None:
    key = "sk-secret-1234567890"
    body = "x" * 185 + " " + key  # the cut at 200 falls inside the key

    with pytest.raises(TranscriptionError) as caught:
        await _engine(lambda request: httpx.Response(400, text=body))(
            TranscriptionRequest(audio=TAKE)
        )
    assert "sk-" not in str(caught.value)


@pytest.mark.parametrize(
    # A pasted smart quote; a line wrap or a NUL inside the key.
    "key",
    ["sk-secret-1234567890”", "sk-secret-12345\n67890", "sk-secret-12345\x0067890"],
)
async def test_a_key_no_header_can_carry_fails_without_being_shown(key: str) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"text": "ok"})

    engine = _engine(handler, api_key=key)
    with pytest.raises(TranscriptionError, match="a header cannot carry") as caught:
        await engine(TranscriptionRequest(audio=TAKE))
    # Refused before the HTTP stack could quote it in an error of its own.
    assert "sk-secret" not in str(caught.value) and not sent

    res = _client(transcribe=engine).post("/api/transcribe", content=TAKE, headers=WAV)
    assert res.status_code == 502 and "sk-secret" not in res.text


def test_any_engine_exception_reads_as_its_message() -> None:
    async def broken(request: TranscriptionRequest) -> str:
        raise UnicodeEncodeError("ascii", "é", 0, 1, "ordinal not in range")

    res = _client(transcribe=broken).post("/api/transcribe", content=TAKE, headers=WAV)
    # The message, not the exception's argument tuple.
    assert res.json()["detail"]["message"].startswith("'ascii' codec can't encode")


# --------------------------------------------------------- the settings -


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.chdir(tmp_path)
    path = storage.config_path(tmp_path / "home")
    config = WebConfig(assistant=AssistantConfig(memory=False, subagents=False))
    loaded = LoadedConfig(config, path, storage.display_path(path), exists=False)
    runtime = ConfigRuntime(
        deployment=Deployment(), loaded=loaded, store=ChatStore.in_memory()
    )
    app = create_app({}, store=runtime.store, config_runtime=runtime, titles=False)
    # A loopback Host header passes the rebinding guard.
    with TestClient(app, base_url="http://localhost") as client:
        yield client, runtime


def _add_model(client: TestClient, **fields: object) -> str:
    payload: dict[str, object] = {
        "model": "gpt-5.5",
        "base_url": "https://gw.example/v1",
        "api_key": "sk-chat-1234567890",
    }
    payload.update(fields)
    res = client.post("/api/config/models", json=payload)
    assert res.status_code == 201, res.text
    return str(res.json()["id"])


def _voice(client: TestClient, **fields: object) -> dict[str, Any]:
    res = client.put("/api/config/voice", json=fields)
    assert res.status_code == 200, res.text
    return dict(res.json()["config"]["voice"])


def _on(client: TestClient) -> bool:
    return bool(client.get("/api/info").json()["features"]["transcription"])


def test_voice_is_off_until_a_model_and_an_endpoint_are_set(served: Any) -> None:
    client, runtime = served
    # default_app's own transcriber is always there to ask; the flag must
    # follow the configuration, not that.
    assert not _on(client)
    assert client.post("/api/transcribe", content=TAKE, headers=WAV).status_code == 404

    assert not _voice(client, model="gpt-transcribe")["enabled"]
    assert not _on(client)

    # Saved before any chat model exists.
    voice = _voice(client, base_url="https://stt.example/v1/", api_key="sk-stt-123456")
    assert voice["enabled"] and voice["base_url"] == "https://stt.example/v1"
    assert voice["api_key"] == {"set": True, "hint": voice["api_key"]["hint"]}
    assert "sk-stt-123456" not in str(voice)
    assert _on(client)
    assert runtime.loaded.path.exists()


def test_the_key_keeps_or_clears_by_the_usual_convention(served: Any) -> None:
    client, runtime = served
    _voice(client, model="m", base_url="https://stt.example/v1", api_key="sk-stt")
    _voice(client, language="ZH")
    assert runtime.config.voice.api_key == "sk-stt"
    assert runtime.config.voice.language == "zh"
    _voice(client, api_key="", language=None)
    assert runtime.config.voice.api_key is None
    assert runtime.config.voice.language is None


def test_a_blank_connection_or_endpoint_costs_nothing_kept(served: Any) -> None:
    client, runtime = served
    profile = _add_model(client)
    _voice(client, model="m", base_url="https://stt.example/v1", api_key="sk-stt")
    _voice(client, connection="  ")
    assert runtime.config.voice.base_url == "https://stt.example/v1"
    assert runtime.config.voice.api_key == "sk-stt"

    _voice(client, connection=profile)
    _voice(client, base_url=" / ")
    assert runtime.config.voice.connection == profile


def test_a_borrowed_connection_brings_its_environment_key(
    served: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, runtime = served
    made: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "lovia.web.config.runtime.openai_transcriber",
        lambda **kw: made.append(kw) or _echo,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    # A keyless profile chats on the provider's environment key; so does a
    # take sent through it — but an endpoint of voice's own gets none.
    profile = _add_model(client, api_key=None)
    _voice(client, connection=profile, model="gpt-transcribe")
    assert runtime.transcriber() and made[-1]["api_key"] == "sk-from-env"
    _voice(client, base_url="https://stt.example/v1")
    assert runtime.transcriber() and made[-1]["api_key"] is None


def test_a_connection_lends_its_endpoint_and_key(
    served: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, runtime = served
    made: list[dict[str, Any]] = []

    def fake(**kw: Any) -> Any:
        made.append(kw)
        return _echo

    monkeypatch.setattr("lovia.web.config.runtime.openai_transcriber", fake)
    profile = _add_model(client)
    _voice(client, base_url="https://stt.example/v1", api_key="sk-stt")
    voice = _voice(client, connection=profile, model="gpt-transcribe", language="zh")
    # Naming a connection drops the endpoint kept before.
    assert voice["base_url"] is None and voice["api_key"]["set"] is False
    assert voice["enabled"] and voice["problem"] is None

    res = client.post("/api/transcribe", content=TAKE, headers=WAV)
    assert res.status_code == 200, res.text
    assert made[-1] == {
        "base_url": "https://gw.example/v1/audio/transcriptions",
        "api_key": "sk-chat-1234567890",
        "model": "gpt-transcribe",
        "language": "zh",
    }

    # A reference, not a copy: the profile's new key is the next take's.
    client.put(
        f"/api/config/models/{profile}",
        json={
            "model": "gpt-5.5",
            "base_url": "https://gw.example/v1",
            "api_key": "sk-new",
        },
    )
    client.post("/api/transcribe", content=TAKE, headers=WAV)
    assert made[-1]["api_key"] == "sk-new"

    # And naming an endpoint drops the connection.
    voice = _voice(client, base_url="https://stt.example/v1")
    assert voice["connection"] is None and voice["enabled"]


def test_a_connection_with_no_base_url_is_the_official_endpoint(served: Any) -> None:
    client, runtime = served
    profile = _add_model(client, base_url=None)
    _voice(client, connection=profile, model="gpt-transcribe")
    assert runtime.config.voice_endpoint() == (
        "https://api.openai.com/v1/audio/transcriptions",
        "sk-chat-1234567890",
        None,
    )


@pytest.mark.parametrize(
    "typed, url",
    [
        # An API root: no path, or one ending in a version.
        ("https://stt.example", "https://stt.example/audio/transcriptions"),
        ("https://stt.example/v1/", "https://stt.example/v1/audio/transcriptions"),
        (
            "https://open.example/api/paas/v4",
            "https://open.example/api/paas/v4/audio/transcriptions",
        ),
        # Anything else is the endpoint itself.
        (
            "https://stt.example/v1/audio/transcriptions",
            "https://stt.example/v1/audio/transcriptions",
        ),
        ("https://stt.example/recognize", "https://stt.example/recognize"),
        # Roots of other shapes: a dotted or capital version, a gateway's
        # ``openai``; a query stays where it was.
        (
            "https://stt.example/api/V1.1",
            "https://stt.example/api/V1.1/audio/transcriptions",
        ),
        (
            "https://gw.example/v1beta/openai/",
            "https://gw.example/v1beta/openai/audio/transcriptions",
        ),
        (
            "https://stt.example/v1?api-version=2",
            "https://stt.example/v1/audio/transcriptions?api-version=2",
        ),
        (
            "https://api.deepseek.com:8443/v1?x=1",
            "https://api.deepseek.com:8443/v1/tools/asr?x=1",
        ),
        (
            "https://api.deepseek.com/v1/tools/asr",
            "https://api.deepseek.com/v1/tools/asr",
        ),
        # A known service's root leads to its own route.
        ("https://api.deepseek.com", "https://api.deepseek.com/v1/tools/asr"),
        ("https://API.DeepSeek.com/v1", "https://API.DeepSeek.com/v1/tools/asr"),
    ],
)
def test_one_address_field_takes_a_root_or_the_endpoint(typed: str, url: str) -> None:
    assert transcription_endpoint(typed)[0] == url
    # The configuration resolves it, then the transcriber does again.
    assert transcription_endpoint(url)[0] == url
    assert (
        transcription_endpoint(transcription_endpoint(typed, root=True)[0])[0]
        == (transcription_endpoint(typed, root=True)[0])
    )


def _refuses_early(first: Any) -> Any:
    """A service that judges the key before the upload ends: the take gets
    ``first`` (a reset, a gateway's empty error), a bodiless ask the 401."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.content:
            if isinstance(first, Exception):
                raise first
            return first
        return httpx.Response(
            401, json={"error": {"message": "key sk-secret-1234567890 is invalid"}}
        )

    return handler


@pytest.mark.parametrize(
    "first",
    [
        httpx.Response(550),
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
        httpx.WriteError("Broken pipe"),
        httpx.ReadError("Connection reset by peer"),
    ],
)
async def test_a_take_cut_off_over_the_key_says_it_was_the_key(first: Any) -> None:
    fn = _engine(_refuses_early(first))
    with pytest.raises(TranscriptionError) as caught:
        await fn(TranscriptionRequest(audio=TAKE))
    said = str(caught.value)
    assert "refused the API key (HTTP 401)" in said and "is invalid" in said
    assert "sk-secret-1234567890" not in said


async def test_a_refusal_that_is_not_about_the_key_stays_what_it_was() -> None:
    asked: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(bool(request.content))
        return httpx.Response(550 if request.content else second)

    # A 403 may be about anything: only a 401 is the key's.
    for second in (400, 403):
        asked.clear()
        with pytest.raises(TranscriptionError, match="answered HTTP 550$"):
            await _engine(handler)(TranscriptionRequest(audio=TAKE))
        assert asked == [True, False]

    # The service's own refusal, empty or not, and a timeout: not asked.
    for status in (404, 413, 429):
        asked.clear()
        with pytest.raises(TranscriptionError, match=f"answered HTTP {status}$"):
            await _engine(lambda r: asked.append(True) or httpx.Response(status))(
                TranscriptionRequest(audio=TAKE)
            )
        assert asked == [True]

    def slow(request: httpx.Request) -> httpx.Response:
        asked.append(True)
        raise httpx.ReadTimeout("slow", request=request)

    asked.clear()
    with pytest.raises(TranscriptionError, match="did not answer"):
        await _engine(slow)(TranscriptionRequest(audio=TAKE))
    assert asked == [True]


async def test_a_take_cut_off_with_no_key_set_says_one_is_wanted() -> None:
    fn = _engine(_refuses_early(httpx.Response(550)), api_key=None)
    with pytest.raises(TranscriptionError, match="asked for an API key"):
        await fn(TranscriptionRequest(audio=TAKE))


async def test_asked_services_stay_as_they_were() -> None:
    asked: list[bool] = []

    # An answer in the service's own words needs no second opinion, and a
    # service never reached is not asked again.
    asked.clear()
    with pytest.raises(TranscriptionError, match="HTTP 429: slow down"):
        await _engine(
            lambda r: asked.append(True) or httpx.Response(429, text="slow down")
        )(TranscriptionRequest(audio=TAKE))

    def unreachable(request: httpx.Request) -> httpx.Response:
        asked.append(True)
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(TranscriptionError, match="could not reach"):
        await _engine(unreachable)(TranscriptionRequest(audio=TAKE))
    assert asked == [True, True]


async def test_an_address_nothing_answers_at_may_have_been_a_root() -> None:
    fn = _engine(lambda r: httpx.Response(404), base_url="https://gw.example/compat")
    with pytest.raises(TranscriptionError) as caught:
        await fn(TranscriptionRequest(audio=TAKE))
    assert "/audio/transcriptions" in (caught.value.hint or "")
    # Not said of an address that already is the OpenAI route.
    fn = _engine(lambda r: httpx.Response(404))
    with pytest.raises(TranscriptionError) as caught:
        await fn(TranscriptionRequest(audio=TAKE))
    assert caught.value.hint is None
    # Nor of a route lovia chose itself.
    fn = _engine(lambda r: httpx.Response(404), base_url="https://api.deepseek.com")
    with pytest.raises(TranscriptionError) as caught:
        await fn(TranscriptionRequest(audio=TAKE))
    assert caught.value.hint is None


def test_an_address_that_is_none_breaks_nothing_but_the_take(served: Any) -> None:
    client, _ = served
    # Saved, and the page still boots and Settings still loads to fix it.
    voice = _voice(client, base_url="https://[oops/v1", model="m")
    assert voice["base_url"] == "https://[oops/v1"
    assert client.get("/api/info").status_code == 200
    assert client.get("/api/config").status_code == 200
    res = client.post("/api/transcribe", content=TAKE, headers=WAV)
    assert res.status_code == 502, res.text


@pytest.mark.parametrize("typed", ["https://", "//host/v1", "http:/typo"])
def test_only_a_bare_host_gets_a_scheme(typed: str) -> None:
    kept = WebConfig.model_validate({"voice": {"base_url": typed}}).voice.base_url
    assert kept == typed.rstrip("/")


def test_an_address_typed_without_a_scheme_is_https() -> None:
    config = WebConfig.model_validate({"voice": {"base_url": "api.deepseek.com/"}})
    assert config.voice.base_url == "https://api.deepseek.com"
    assert config.voice_endpoint() == (
        "https://api.deepseek.com/v1/tools/asr",
        None,
        120,
    )


def test_a_connection_is_a_root_whatever_it_looks_like() -> None:
    # A chat model's base URL need not end in a version.
    assert transcription_endpoint("https://gw.example/openai", root=True) == (
        "https://gw.example/openai/audio/transcriptions",
        None,
    )
    assert transcription_endpoint("https://api.deepseek.com/beta", root=True) == (
        "https://api.deepseek.com/v1/tools/asr",
        120,
    )


async def test_a_take_names_no_model_where_none_was_given() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "ok", "duration_ms": 1})

    fn = _engine(handler, base_url="https://api.deepseek.com/v1/tools/asr", model=None)
    assert await fn(TranscriptionRequest(audio=TAKE)) == "ok"
    assert seen["url"] == "https://api.deepseek.com/v1/tools/asr"
    assert b'name="model"' not in seen["body"]


def test_a_borrowed_deepseek_connection_needs_nothing_else(served: Any) -> None:
    client, runtime = served
    profile = _add_model(
        client, model="deepseek-chat", base_url="https://api.deepseek.com"
    )
    voice = _voice(client, connection=profile)
    assert voice["enabled"] and voice["problem"] is None and _on(client)
    assert runtime.config.voice_endpoint() == (
        "https://api.deepseek.com/v1/tools/asr",
        "sk-chat-1234567890",
        120,
    )
    # The page stops a take where the service would refuse it.
    assert client.get("/api/info").json()["take_seconds"] == 120


def test_the_openai_route_is_off_until_a_model_is_named(served: Any) -> None:
    client, _ = served
    voice = _voice(client, base_url="https://stt.example/v1")
    assert not voice["enabled"] and voice["problem"] == "model_missing"
    assert not _on(client)
    voice = _voice(client, model="whisper-1")
    assert voice["enabled"] and voice["problem"] is None
    assert client.get("/api/info").json()["take_seconds"] is None


def test_a_connection_that_leads_nowhere_turns_voice_off_quietly(
    served: Any, tmp_path: Path
) -> None:
    client, runtime = served
    keep = _add_model(client, name="keep")
    lent = _add_model(client, name="lent")
    _voice(client, connection=lent, model="gpt-transcribe")
    assert _on(client)

    # Re-pointed at an Anthropic endpoint: the save goes through.
    res = client.put(
        f"/api/config/models/{lent}",
        json={"model": "claude-opus-5-5", "flavor": "anthropic", "api_key": "sk-ant"},
    )
    assert res.status_code == 200, res.text
    voice = res.json()["config"]["voice"]
    assert voice["problem"] == "connection_not_openai" and not voice["enabled"]
    assert not _on(client)

    # Deleted: the delete goes through, and the reference stays as written.
    res = client.delete(f"/api/config/models/{lent}")
    assert res.status_code == 200, res.text
    voice = res.json()["config"]["voice"]
    assert voice["connection"] == lent and voice["problem"] == "connection_missing"
    assert client.post("/api/transcribe", content=TAKE, headers=WAV).status_code == 404

    # A file that names a connection that isn't there still loads.
    again = storage.load_config(runtime.loaded.path)
    assert again.voice.connection == lent and again.voice_endpoint() is None
    assert again.profile(keep) is not None


@pytest.mark.parametrize(
    "fields, said",
    [
        ({"language": "chinese!"}, "language"),
        ({"connection": "x", "base_url": "https://stt.example/v1"}, "not both"),
    ],
)
def test_voice_config_refuses_what_it_cannot_mean(
    fields: dict[str, str], said: str
) -> None:
    with pytest.raises(ValueError, match=said):
        WebConfig.model_validate({"voice": fields})


def test_a_voice_write_passes_the_rebinding_guard(served: Any) -> None:
    client, runtime = served
    runtime.require_local_host = True
    res = client.put(
        "/api/config/voice", json={"model": "m"}, headers={"host": "evil.example"}
    )
    assert res.status_code == 403
