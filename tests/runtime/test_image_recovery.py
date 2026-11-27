"""A provider refusing the images in a request no longer fails the run (#532):
the call goes again with images withheld as their text marker, and the chat
remembers what it decided (runtime/images.py, docs/design/vision-images.md)."""

from __future__ import annotations

from typing import Any

import pytest

from lovia import Agent, Runner, events, tool
from lovia.context import CompactionRequest, ContextResult
from lovia.exceptions import ContextOverflowError, ProviderError
from lovia.parts import ImagePart, TextPart
from lovia.messages import user
from lovia.providers.anthropic import _to_anthropic_messages
from lovia.runtime.images import ImageCall
from lovia.stores import InMemorySession
from lovia.transcript import (
    FinishDelta,
    InputEntry,
    TextDelta,
    ToolCallDelta,
    ToolCallEntry,
    ToolResultEntry,
    TranscriptEntry,
)

PIXELS = "image exceeds the maximum allowed size: 8000 pixels on a side"
NO_IMAGES = (
    "Failed to deserialize the JSON body: unknown variant `image_url`, expected `text`"
)


def png(tag: str) -> ImagePart:
    return ImagePart(data=f"iVBORw0KGgo{tag}AAAA", mime_type="image/png")


def images_in(entries: list[TranscriptEntry]) -> list[ImagePart]:
    found: list[ImagePart] = []
    for e in entries:
        if isinstance(e, InputEntry) and isinstance(e.content, list):
            found += [p for p in e.content if isinstance(p, ImagePart)]
        elif isinstance(e, ToolResultEntry) and e.parts:
            found += [p for p in e.parts if isinstance(p, ImagePart)]
    return found


def refusal(body: str, status: int = 400) -> ProviderError:
    # As the built-in adapters raise it: a 4xx is not worth retrying as is.
    return ProviderError(
        f"HTTP {status}: {body}", status_code=status, body=body, retryable=False
    )


class Gate:
    """A vision endpoint that refuses a request ``refuse(images)`` names a
    body for, answers "ok" otherwise, and records what each call sent."""

    name = "gate"
    model = "m"
    base_url = "https://gate.example/v1"
    supports_vision = True

    def __init__(self, refuse: Any, script: list[Any] | None = None) -> None:
        self.refuse = refuse
        self.script = list(script or [])
        self.sent: list[list[ImagePart]] = []
        self.calls: list[list[TranscriptEntry]] = []

    async def stream(self, entries, **kwargs):
        images = images_in(entries)
        self.sent.append(images)
        self.calls.append(list(entries))
        body = self.refuse(images)
        if body:
            raise refusal(body)
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            for delta in step:
                yield delta
            return
        yield TextDelta(text="ok")
        yield FinishDelta(reason="stop")


def ask(*parts: Any) -> list[Any]:
    return [user([TextPart("look"), *parts])]


async def run(agent: Agent, input: Any, **kw: Any) -> tuple[Any, list[events.Event]]:
    seen: list[events.Event] = []
    result = None
    async for ev in Runner.stream(agent, input, **kw):
        seen.append(ev)
        if isinstance(ev, events.RunCompleted):
            result = ev.result
    return result, seen


def withheld(seen: list[events.Event]) -> list[tuple[int, bool]]:
    return [
        (e.count, e.unsupported) for e in seen if isinstance(e, events.ImagesWithheld)
    ]


async def test_a_refused_image_becomes_its_marker_and_the_run_goes_on() -> None:
    gate = Gate(lambda imgs: PIXELS if imgs else None)
    agent = Agent(name="a", model=gate)
    result, seen = await run(agent, ask(png("A")))

    assert result.output == "ok"
    assert [len(s) for s in gate.sent] == [1, 0]
    retried = gate.calls[1][-1]
    assert isinstance(retried, InputEntry) and isinstance(retried.content, list)
    assert retried.content[0] == TextPart("look")
    assert isinstance(retried.content[1], TextPart)
    assert retried.content[1].text.startswith("[image: image/png")
    assert withheld(seen) == [(1, False)]
    # The transcript keeps the image: only the view changed.
    kept = result.entries[0]
    assert isinstance(kept.content[1], ImagePart)


async def test_an_image_already_accepted_is_kept_and_only_the_new_one_goes() -> None:
    # Two images per request at most: the 3rd one is refused, the first two
    # (sent and accepted in an earlier run) stay.
    limit = {"n": 2}

    def refuse(imgs: list[ImagePart]) -> str | None:
        return "too many images in request" if len(imgs) > limit["n"] else None

    gate = Gate(refuse)
    agent = Agent(name="a", model=gate)
    session = InMemorySession()
    await run(agent, ask(png("A"), png("B")), session=session, session_id="s")
    _, seen = await run(agent, ask(png("C")), session=session, session_id="s")

    assert [len(s) for s in gate.sent] == [2, 3, 2]
    assert [p.data for p in gate.sent[2]] == [png("A").data, png("B").data]
    assert withheld(seen) == [(1, False)]

    # Remembered by position, not content: the same picture sent again later
    # (here once the endpoint takes more) is tried again, while the refused
    # copy stays withheld in history.
    limit["n"] = 3
    _, seen = await run(agent, ask(png("C")), session=session, session_id="s")
    assert [p.data for p in gate.sent[-1]] == [png(t).data for t in "ABC"]
    assert withheld(seen) == []


async def test_an_unattributable_refusal_withholds_all_on_the_second_try() -> None:
    # Refused while any image is left: new ones first, then all.
    gate = Gate(lambda imgs: PIXELS if imgs else None)
    agent = Agent(name="a", model=gate)
    session = InMemorySession()
    gate.refuse = lambda imgs: None
    await run(agent, ask(png("A")), session=session, session_id="s")
    gate.refuse = lambda imgs: PIXELS if imgs else None
    result, seen = await run(agent, ask(png("B")), session=session, session_id="s")

    assert result.output == "ok"
    assert [len(s) for s in gate.sent] == [1, 2, 1, 0]
    assert withheld(seen) == [(1, False), (1, False)]


async def test_a_model_without_vision_is_remembered_for_the_chat() -> None:
    gate = Gate(lambda imgs: NO_IMAGES if imgs else None)
    agent = Agent(name="a", model=gate)
    session = InMemorySession()
    _, seen = await run(agent, ask(png("A")), session=session, session_id="s")
    assert withheld(seen) == [(1, True)]

    # The next run withholds from its first call: no refusal, no event.
    _, seen = await run(agent, ask(png("B")), session=session, session_id="s")
    assert [len(s) for s in gate.sent] == [1, 0, 0]
    assert withheld(seen) == []

    # Another chat on the same endpoint tries on its own.
    _, seen = await run(agent, ask(png("C")), session=session, session_id="other")
    assert withheld(seen) == [(1, True)]


async def test_a_refusal_after_output_or_without_images_stands() -> None:
    class _Partial(Gate):
        async def stream(self, entries, **kwargs):
            self.sent.append(images_in(entries))
            yield TextDelta(text="par")
            raise refusal(PIXELS)

    partial = _Partial(lambda imgs: None)
    with pytest.raises(ProviderError):
        await Runner.run(Agent(name="a", model=partial), ask(png("A")))
    assert len(partial.sent) == 1

    # The same words in an answer to a request with no image are not ours.
    plain = Gate(lambda imgs: PIXELS)
    with pytest.raises(ProviderError):
        await Runner.run(Agent(name="a", model=plain), "hi")
    assert len(plain.sent) == 1


async def test_a_tool_image_keeps_its_path_and_gets_a_marker_in_place() -> None:
    # view_image returns [path, image]; Anthropic encodes a tool result from
    # its parts, so the image must turn into text where it was, not vanish.
    result = ToolResultEntry(
        call_id="c1",
        output="shot.png\n[image: image/png, 6 B]",
        parts=[TextPart("shot.png"), png("S")],
    )
    call = ImageCall({}, [result], head=0, model="k")
    assert call.withhold_more([result], "image") == 1
    view = call.apply([result])
    _, messages = _to_anthropic_messages(view, include_images=True)
    content = messages[0]["content"][0]["content"]
    assert [b["type"] for b in content] == ["text", "text"]
    assert content[0]["text"] == "shot.png"
    assert content[1]["text"].startswith("[image: image/png")
    assert isinstance(result.parts[1], ImagePart)  # the transcript's entry is untouched


async def test_a_reshaped_history_does_not_withhold_another_image() -> None:
    # A remembered position applies only while its image is still there.
    a, b = png("A"), png("B")
    entries: list[TranscriptEntry] = [InputEntry(role="user", content=[a])]
    call = ImageCall({}, entries, head=0, model="k")
    call.withhold_more(entries, "image")
    scratch: dict[str, Any] = {}
    call.remember(scratch)
    assert scratch["images"]["withheld"]
    # Remembered, it is applied before the context policy sees the entries.
    again = ImageCall(scratch, entries, head=0, model="k")
    assert again.entries[0] is not entries[0]
    assert not any(isinstance(p, ImagePart) for p in again.entries[0].content)

    edited: list[TranscriptEntry] = [InputEntry(role="user", content=[b])]
    later = ImageCall(scratch, edited, head=0, model="k")
    assert later.entries == edited and later.entries[0] is edited[0]


async def test_images_before_the_models_last_answer_were_accepted() -> None:
    # Read from the transcript, so a resumed run knows it too: the user's
    # image went out with the call that answered; the screenshot after it is
    # the new one.
    from lovia.transcript import AssistantTextEntry

    entries: list[TranscriptEntry] = [
        InputEntry(role="user", content=[TextPart("look"), png("A")]),
        ToolCallEntry(call_id="c1", name="shot", arguments="{}"),
        ToolResultEntry(call_id="c1", output="", parts=[png("S")]),
    ]
    call = ImageCall({}, entries, head=0, model="k")
    assert call.withhold_more(entries, "image") == 1
    view = call.apply(entries)
    assert isinstance(view[0].content[1], ImagePart)
    assert isinstance(view[2].parts[0], TextPart)
    assert AssistantTextEntry  # the other kind of model output counts alike


async def test_the_same_image_object_twice_is_two_images() -> None:
    # A tool returning a cached ImagePart: the copy the model already took
    # stays, the new occurrence goes.
    shared = png("X")
    entries: list[TranscriptEntry] = [
        InputEntry(role="user", content=[shared]),
        ToolCallEntry(call_id="c1", name="again", arguments="{}"),
        ToolResultEntry(call_id="c1", output="", parts=[shared]),
    ]
    call = ImageCall({}, entries, head=0, model="k")
    assert call.withhold_more(entries, "image") == 1
    view = call.apply(entries)
    assert view[0].content[0] is shared
    assert isinstance(view[2].parts[0], TextPart)


async def test_a_model_without_vision_is_remembered_but_not_its_images() -> None:
    # Another model of the chat (a switch, a handoff) still gets them.
    entries: list[TranscriptEntry] = [InputEntry(role="user", content=[png("A")])]
    call = ImageCall({}, entries, head=0, model="blind")
    assert call.withhold_more(entries, "unsupported") == 1
    scratch: dict[str, Any] = {}
    call.remember(scratch)
    assert scratch["images"] == {"blind": ["blind"], "withheld": []}
    other = ImageCall(scratch, entries, head=0, model="sees")
    assert other.entries[0] is entries[0]


async def test_an_overflow_after_an_image_refusal_keeps_the_image_out() -> None:
    @tool
    async def dump() -> str:
        """Return a huge payload."""
        return "x" * 40_000

    @tool
    async def shot() -> list[Any]:
        """Take a screenshot."""
        return [TextPart("shot.png"), png("S")]

    gate = Gate(
        lambda imgs: None,
        script=[
            [
                ToolCallDelta(index=0, call_id="c1", name="dump", arguments="{}"),
                ToolCallDelta(index=1, call_id="c2", name="shot", arguments="{}"),
                FinishDelta(reason="tool_calls"),
            ],
        ],
    )
    calls = {"n": 0}

    def refuse(imgs: list[ImagePart]) -> str | None:
        calls["n"] += 1
        return PIXELS if calls["n"] == 2 and imgs else None

    gate.refuse = refuse
    overflow_once = {"done": False}
    original = gate.stream

    async def stream(entries, **kwargs):
        if calls["n"] == 2 and not overflow_once["done"]:
            overflow_once["done"] = True
            calls["n"] += 1
            gate.sent.append(images_in(entries))
            raise ContextOverflowError("prompt is too long", reported_window=8_192)
        async for d in original(entries, **kwargs):
            yield d

    gate.stream = stream  # type: ignore[method-assign]
    agent = Agent(name="a", model=gate, tools=[dump, shot])
    result, seen = await run(agent, "go")

    assert result.output == "ok"
    # call 1 tools; call 2 refused (image); call 3 overflowed (no image);
    # call 4, after re-compaction, still without the image.
    assert [len(s) for s in gate.sent] == [0, 1, 0, 0]
    assert withheld(seen) == [(1, False)]
    assert any(isinstance(e, events.ContextCompacted) for e in seen)


class _KeepAll:
    """A context policy that never compacts."""

    async def compact(self, req: CompactionRequest) -> ContextResult:
        return ContextResult(entries=list(req.entries))


async def test_decisions_are_kept_without_a_compacting_policy() -> None:
    gate = Gate(lambda imgs: NO_IMAGES if imgs else None)
    agent = Agent(name="a", model=gate, context_policy=_KeepAll())
    session = InMemorySession()
    await run(agent, ask(png("A")), session=session, session_id="s")
    await run(agent, ask(png("B")), session=session, session_id="s")
    assert [len(s) for s in gate.sent] == [1, 0, 0]


async def test_a_retry_policy_never_resends_the_refused_images() -> None:
    from lovia.reliability import RetryPolicy

    gate = Gate(lambda imgs: PIXELS if imgs else None)
    retry = RetryPolicy(max_attempts=4, retry_on=lambda exc: True, backoff_base=0)
    result = await Runner.run(Agent(name="a", model=gate), ask(png("A")), retry=retry)
    assert result.output == "ok"
    assert [len(s) for s in gate.sent] == [1, 0]

    # A request with no image is the retry policy's as before, whatever its
    # refusal says about vision.
    flaky = Gate(lambda imgs: None, script=[refusal("try a vision model")])
    result = await Runner.run(Agent(name="a", model=flaky), "hi", retry=retry)
    assert result.output == "ok" and len(flaky.sent) == 2


async def test_a_provider_that_does_not_declare_vision_gets_tool_images_recovered() -> (
    None
):
    @tool
    async def shot() -> list[Any]:
        """Take a screenshot."""
        return [TextPart("shot.png"), png("S")]

    class Undeclared(Gate):
        pass

    gate = Undeclared(
        lambda imgs: PIXELS if imgs else None,
        script=[
            [
                ToolCallDelta(index=0, call_id="c1", name="shot", arguments="{}"),
                FinishDelta(reason="tool_calls"),
            ]
        ],
    )
    del Gate.supports_vision  # an adapter that says nothing of vision
    try:
        result, seen = await run(Agent(name="a", model=gate, tools=[shot]), "go")
    finally:
        Gate.supports_vision = True  # type: ignore[attr-defined]
    assert result.output == "ok"
    assert withheld(seen) == [(1, False)]


async def test_an_injected_image_never_reaches_a_model_known_blind() -> None:
    entries: list[TranscriptEntry] = [InputEntry(role="user", content=[TextPart("hi")])]
    injected = InputEntry(role="user", content=[png("I")])
    call = ImageCall(
        {"images": {"blind": ["k"], "withheld": []}}, entries, head=0, model="k"
    )
    view = call.apply([*entries, injected])
    assert not any(isinstance(p, ImagePart) for p in view[-1].content)


async def test_no_images_remembers_the_model_not_an_earlier_tries_exclusions() -> None:
    # First a vague refusal (the new image goes), then "takes no images":
    # only the model is the fact; the new image may suit another model.
    from lovia.transcript import AssistantTextEntry

    entries: list[TranscriptEntry] = [
        InputEntry(role="user", content=[png("A")]),
        AssistantTextEntry(content="seen"),
        InputEntry(role="user", content=[png("B")]),
    ]
    call = ImageCall({}, entries, head=0, model="k")
    assert call.withhold_more(entries, "image") == 1
    assert call.withhold_more(entries, "unsupported") == 1
    scratch: dict[str, Any] = {}
    call.remember(scratch)
    assert scratch["images"] == {"blind": ["k"], "withheld": []}
