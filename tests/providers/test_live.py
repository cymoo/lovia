"""Opt-in live integration tests for configured provider endpoints."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from pydantic import BaseModel

from lovia import (
    Agent,
    Message,
    FilePart,
    ImagePart,
    ModelSettings,
    RunResult,
    Runner,
    TextPart,
    user,
)
from lovia import events
from lovia.tools import tool

pytestmark = pytest.mark.live_provider

LiveInput = str | list[Message]

_ONE_PIXEL_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO"
    "+/p9sAAAAASUVORK5CYII="
)


class TinyAnswer(BaseModel):
    answer: str


def _load_env_file() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _require_live() -> None:
    _load_env_file()
    if os.getenv("LOVIA_LIVE_TESTS") != "1":
        pytest.skip("opt-in: set LOVIA_LIVE_TESTS=1 to run live provider tests")


def _openai_chat_model() -> str:
    _require_live()
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY is not configured")
    return os.getenv("OPENAI_DEFAULT_MODEL", "gpt-5.4")


def _anthropic_model() -> str:
    _require_live()
    if not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY is not configured")
    return os.getenv("ANTHROPIC_DEFAULT_MODEL", "claude-haiku-4-5")


def _env_host(name: str, default: str) -> str | None:
    return urlparse(os.getenv(name) or default).hostname


def _require_openai_content_parts() -> None:
    if os.getenv("LOVIA_LIVE_OPENAI_CONTENT_TESTS") == "1":
        return
    if _env_host("OPENAI_BASE_URL", "https://api.openai.com") != "api.openai.com":
        pytest.skip(
            "OpenAI content-part live tests require the official endpoint or "
            "LOVIA_LIVE_OPENAI_CONTENT_TESTS=1"
        )


def _require_anthropic_content_blocks() -> None:
    if os.getenv("LOVIA_LIVE_ANTHROPIC_CONTENT_TESTS") == "1":
        return
    if _env_host("ANTHROPIC_BASE_URL", "https://api.anthropic.com") != (
        "api.anthropic.com"
    ):
        pytest.skip(
            "Anthropic content-block live tests require the official endpoint or "
            "LOVIA_LIVE_ANTHROPIC_CONTENT_TESTS=1"
        )


@tool
def live_add(a: int, b: int) -> int:
    """Add two integers."""

    return a + b


async def _collect_stream(
    agent: Agent, input_data: LiveInput
) -> tuple[RunResult, str, list[events.Event]]:
    handle = Runner.stream(agent, input_data)
    chunks: list[str] = []
    seen: list[events.Event] = []
    async for event in handle:
        seen.append(event)
        if isinstance(event, events.TextDelta):
            chunks.append(event.delta)
    return await handle.result(), "".join(chunks), seen


def _event_types(seen: list[events.Event]) -> set[type[events.Event]]:
    return {type(event) for event in seen}


def _assert_text_result(result: RunResult) -> str:
    assert isinstance(result.output, str)
    text = result.output.strip()
    assert text
    assert result.usage.output_tokens > 0
    return text


def _image_input(prompt: str) -> list[Message]:
    return [
        user(
            [
                TextPart(prompt),
                ImagePart(data=_ONE_PIXEL_PNG, mime_type="image/png", detail="low"),
            ]
        )
    ]


def _file_input(prompt: str, file: FilePart) -> list[Message]:
    return [user([TextPart(prompt), file])]


def _minimal_pdf_bytes(marker: str) -> bytes:
    escaped = marker.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    page_text = f"BT /F1 12 Tf 36 120 Td ({escaped}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length "
        + str(len(page_text)).encode("ascii")
        + b" >>\nstream\n"
        + page_text
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = b"%PDF-1.4\n"
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += f"{index} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"
    xref_offset = len(pdf)
    pdf += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii")
    for offset in offsets:
        pdf += f"{offset:010d} 00000 n \n".encode("ascii")
    pdf += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")
    return pdf


@pytest.mark.asyncio
async def test_openai_chat_live_round_trip() -> None:
    model_name = _openai_chat_model()
    agent = Agent(
        name="probe",
        model=f"openai:{model_name}",
        instructions="Answer in one short sentence.",
    )
    result = await Runner.run(agent, "Say hi.")

    _assert_text_result(result)


@pytest.mark.asyncio
async def test_openai_chat_live_streaming_contract() -> None:
    model_name = _openai_chat_model()
    agent = Agent(
        name="probe",
        model=f"openai:{model_name}",
        instructions="Answer with exactly the word pong.",
    )

    result, streamed_text, seen = await _collect_stream(agent, "ping")

    assert streamed_text.strip()
    assert streamed_text.strip() in _assert_text_result(result)
    assert events.TextDelta in _event_types(seen)
    assert events.MessageCompleted in _event_types(seen)
    assert events.RunCompleted in _event_types(seen)


@pytest.mark.asyncio
async def test_openai_chat_live_structured_output() -> None:
    model_name = _openai_chat_model()
    agent = Agent(
        name="probe",
        model=f"openai:{model_name}",
        instructions="Return the requested structured answer.",
        output_type=TinyAnswer,
    )

    result = await Runner.run(agent, "Set answer to ok.")

    assert isinstance(result.output, TinyAnswer)
    assert result.output.answer


@pytest.mark.asyncio
async def test_openai_chat_live_provider_options() -> None:
    model_name = _openai_chat_model()
    agent = Agent(
        name="probe",
        model=f"openai:{model_name}",
        instructions="Answer with exactly the word ok.",
        settings=ModelSettings(provider_options={"openai": {"n": 1}}),
    )

    result = await Runner.run(agent, "Reply ok.")

    assert "ok" in _assert_text_result(result).lower()


@pytest.mark.asyncio
async def test_openai_chat_live_tool_call() -> None:
    model_name = _openai_chat_model()
    agent = Agent(
        name="probe",
        model=f"openai:{model_name}",
        instructions=(
            "When arithmetic is requested, call live_add exactly once and then "
            "answer with only the numeric result."
        ),
        tools=[live_add],
        settings=ModelSettings(parallel_tool_calls=False),
    )

    result, _, seen = await _collect_stream(
        agent, "Use the tool to add a=2 and b=3, then answer."
    )

    assert "5" in str(result.output)
    completions = [
        event for event in seen if isinstance(event, events.ToolCallCompleted)
    ]
    assert any(
        event.call.name == "live_add"
        and str(event.result) == "5"
        and not event.is_error
        for event in completions
    )


@pytest.mark.asyncio
async def test_openai_chat_live_image_input() -> None:
    model_name = _openai_chat_model()
    _require_openai_content_parts()
    agent = Agent(
        name="probe",
        model=f"openai:{model_name}",
        instructions="Answer in one short sentence.",
    )

    result = await Runner.run(
        agent,
        _image_input("Inspect the attached PNG and reply with exactly ok."),
    )

    assert "ok" in _assert_text_result(result).lower()


@pytest.mark.asyncio
async def test_openai_chat_live_inline_file_input() -> None:
    model_name = _openai_chat_model()
    _require_openai_content_parts()
    marker = "LOVIA_OPENAI_FILE_OK"
    agent = Agent(
        name="probe",
        model=f"openai:{model_name}",
        instructions="Read attached files before answering.",
    )

    result = await Runner.run(
        agent,
        _file_input(
            "Answer with only the exact marker token in the attached text file.",
            FilePart.from_bytes(
                f"marker: {marker}\n".encode(),
                mime_type="text/plain",
                filename="marker.txt",
            ),
        ),
    )

    assert marker in _assert_text_result(result)


@pytest.mark.asyncio
async def test_anthropic_live_round_trip() -> None:
    model_name = _anthropic_model()
    agent = Agent(
        name="probe",
        model=f"anthropic:{model_name}",
        instructions="Answer in one short sentence.",
    )

    result = await Runner.run(agent, "Say hi.")

    _assert_text_result(result)


@pytest.mark.asyncio
async def test_anthropic_live_streaming_contract() -> None:
    model_name = _anthropic_model()
    agent = Agent(
        name="probe",
        model=f"anthropic:{model_name}",
        instructions="Answer with exactly the word pong.",
    )

    result, streamed_text, seen = await _collect_stream(agent, "ping")

    assert streamed_text.strip()
    assert streamed_text.strip() in _assert_text_result(result)
    assert events.TextDelta in _event_types(seen)
    assert events.MessageCompleted in _event_types(seen)
    assert events.RunCompleted in _event_types(seen)


@pytest.mark.asyncio
async def test_anthropic_live_structured_output() -> None:
    model_name = _anthropic_model()
    agent = Agent(
        name="probe",
        model=f"anthropic:{model_name}",
        instructions="Return the requested structured answer.",
        output_type=TinyAnswer,
    )

    result = await Runner.run(agent, "Set answer to ok.")

    assert isinstance(result.output, TinyAnswer)
    assert result.output.answer


@pytest.mark.asyncio
async def test_anthropic_live_model_settings() -> None:
    model_name = _anthropic_model()
    agent = Agent(
        name="probe",
        model=f"anthropic:{model_name}",
        instructions="Answer with exactly the word ok.",
        settings=ModelSettings(max_tokens=64, temperature=0),
    )

    result = await Runner.run(agent, "Reply ok.")

    assert "ok" in _assert_text_result(result).lower()


@pytest.mark.asyncio
async def test_anthropic_live_tool_call() -> None:
    model_name = _anthropic_model()
    agent = Agent(
        name="probe",
        model=f"anthropic:{model_name}",
        instructions=(
            "When arithmetic is requested, call live_add exactly once and then "
            "answer with only the numeric result."
        ),
        tools=[live_add],
        settings=ModelSettings(max_tokens=128, parallel_tool_calls=False),
    )

    result, _, seen = await _collect_stream(
        agent, "Use the tool to add a=2 and b=3, then answer."
    )

    assert "5" in str(result.output)
    completions = [
        event for event in seen if isinstance(event, events.ToolCallCompleted)
    ]
    assert any(
        event.call.name == "live_add"
        and str(event.result) == "5"
        and not event.is_error
        for event in completions
    )


@pytest.mark.asyncio
async def test_anthropic_live_image_input() -> None:
    model_name = _anthropic_model()
    _require_anthropic_content_blocks()
    agent = Agent(
        name="probe",
        model=f"anthropic:{model_name}",
        instructions="Answer in one short sentence.",
    )

    result = await Runner.run(
        agent,
        _image_input("Inspect the attached PNG and reply with exactly ok."),
    )

    assert "ok" in _assert_text_result(result).lower()


@pytest.mark.asyncio
async def test_anthropic_live_text_file_input() -> None:
    model_name = _anthropic_model()
    _require_anthropic_content_blocks()
    marker = "LOVIA_ANTHROPIC_TEXT_FILE_OK"
    agent = Agent(
        name="probe",
        model=f"anthropic:{model_name}",
        instructions="Read attached files before answering.",
    )

    result = await Runner.run(
        agent,
        _file_input(
            "Answer with only the exact marker token in the attached text file.",
            FilePart.from_bytes(
                f"marker: {marker}\n".encode(),
                mime_type="text/plain",
                filename="marker.txt",
            ),
        ),
    )

    assert marker in _assert_text_result(result)


@pytest.mark.asyncio
async def test_anthropic_live_pdf_file_input() -> None:
    model_name = _anthropic_model()
    _require_anthropic_content_blocks()
    marker = "LOVIA_ANTHROPIC_PDF_OK"
    agent = Agent(
        name="probe",
        model=f"anthropic:{model_name}",
        instructions="Read attached files before answering.",
    )

    result = await Runner.run(
        agent,
        _file_input(
            "Answer with only the exact marker token in the attached PDF.",
            FilePart.from_bytes(
                _minimal_pdf_bytes(marker),
                mime_type="application/pdf",
                filename="marker.pdf",
            ),
        ),
    )

    assert marker in _assert_text_result(result)
