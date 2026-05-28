"""Context window management for multi-turn conversations.

A :class:`ContextPolicy` decides what the model sees on every turn. It runs
just before each provider call and may rewrite the transcript — typically by
summarizing older turns when the token count approaches the model's context
window so the conversation can keep going forever without crashing the
provider.

The protocol is deliberately small (one ``apply`` method + a reactive
counterpart) so users can swap in their own strategy. Core ships:

* :class:`NoopContextPolicy` — default zero-overhead behavior.
* :class:`SummarizingContextPolicy` — proactive LLM summarization once the
  prompt crosses ``compact_at_ratio * max_tokens``, plus a more aggressive
  reactive path triggered when the provider returns
  :class:`~lovia.ContextOverflowError`.

The policy is **stateless with respect to persistence**: it returns a
rewritten item list and (optionally) invokes the user-supplied ``archive``
hook. The :class:`~lovia.Runner` is responsible for writing the result back
to the :class:`~lovia.Session` and dispatching the
:class:`~lovia.events.ContextCompacted` event.

Three orthogonal layers (don't conflate them):

* :class:`~lovia.Session` — active transcript for the current conversation
  (rewritten in place after compaction).
* ``archive`` callback — write-only snapshot of the pre-compaction
  transcript, for audit / replay. Framework never reads it back.
* :class:`~lovia.Memory` — long-term semantic store that spans sessions.
  Wire it up to ``ContextCompacted`` in your hooks if you want auto-feed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from .items import (
    InputMessageItem,
    Item,
    MessageOutputItem,
    ReasoningItem,
    ToolCallItem,
    ToolCallOutputItem,
    safe_window,
)
from .providers.base import (
    ModelSettings,
    context_window as _provider_context_window,
    estimate_tokens as _estimate_tokens,
)


logger = logging.getLogger(__name__)


__all__ = [
    "ArchiveEvent",
    "ArchiveCallback",
    "ContextPolicy",
    "NoopContextPolicy",
    "PolicyContext",
    "ProviderSummarizer",
    "SummarizingContextPolicy",
    "Summarizer",
    "DEFAULT_SUMMARY_PROMPT",
    "extract_compaction_summary",
]


# ---------------------------------------------------------------------------
# Default summary prompt
# ---------------------------------------------------------------------------


DEFAULT_SUMMARY_PROMPT = """\
You are compacting a long agent transcript so the conversation can continue \
without losing important state. Produce a faithful, third-person summary \
covering ONLY what is needed for the next turns:

1. **User goal(s)** — what the user is ultimately trying to achieve.
2. **Key findings & decisions** — facts established, conclusions reached, \
   approaches ruled out (with brief justification).
3. **Files / artifacts changed** — paths touched, what changed, and why.
4. **Outstanding work** — concrete next steps the assistant intended to take.
5. **User constraints & preferences** — explicit rules the user gave (tone, \
   style, must/must-not-do, deadlines, names, IDs, etc.).

Rules:
- Be specific. Preserve exact identifiers, file paths, and numbers verbatim.
- Do NOT invent facts not present in the transcript.
- Do NOT include pleasantries or meta-commentary about the summarization.
- Output plain prose with the five headings above; no JSON, no code fences.
"""


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PolicyContext:
    """Per-turn information made available to :class:`ContextPolicy`.

    Attributes:
        provider: The :class:`~lovia.Provider` the runner is about to call.
            Policies may consult ``provider.context_window(model)`` and
            ``provider.estimate_tokens(items)`` via the helpers in
            :mod:`lovia.providers.base`.
        model: The model identifier (``provider.model``) used to look up the
            context window. ``None`` when the provider doesn't expose one.
        last_prompt_tokens: The ``usage.prompt_tokens`` value reported on the
            previous turn, when available. ``None`` on the first turn.
        session_id: Optional session identifier — included in
            :class:`ArchiveEvent` so write-only sinks can key on it.
    """

    provider: Any
    model: str | None
    last_prompt_tokens: int | None = None
    session_id: str | None = None


@dataclass
class ArchiveEvent:
    """Payload passed to the ``archive`` callback when compaction occurs.

    The callback is fire-and-forget — the framework never reads back from
    archives. Use it to persist full transcripts to JSONL / S3 / a database
    if you need audit or replay capabilities.
    """

    session_id: str | None
    items_before: list[Item]
    items_after: list[Item]
    summary: str | None
    reactive: bool = False


ArchiveCallback = Callable[[ArchiveEvent], Awaitable[None]]


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class ContextPolicy(Protocol):
    """Strategy that decides what items the model sees on each turn.

    Implementations are pure transformations: take the current item list,
    return a (possibly trimmed) list. Returning the same list object signals
    "no change" — the runner uses identity to decide whether to persist back
    to the :class:`~lovia.Session`.

    Two entry points:

    * :meth:`apply` runs before every model call (proactive compaction).
    * :meth:`apply_reactive` runs after the provider reports
      :class:`~lovia.ContextOverflowError` (last-resort compaction).
    """

    async def apply(self, items: list[Item], *, ctx: PolicyContext) -> list[Item]: ...

    async def apply_reactive(
        self, items: list[Item], *, ctx: PolicyContext
    ) -> list[Item]: ...


@runtime_checkable
class Summarizer(Protocol):
    """Pluggable summarization backend used by :class:`SummarizingContextPolicy`.

    The default implementation, :class:`ProviderSummarizer`, asks an LLM to
    produce the summary. Users who want rule-based or local-model
    summarization implement this protocol directly.
    """

    async def summarize(self, items: list[Item], *, ctx: PolicyContext) -> str: ...


# ---------------------------------------------------------------------------
# Default no-op policy
# ---------------------------------------------------------------------------


class NoopContextPolicy:
    """A :class:`ContextPolicy` that never modifies the transcript.

    Used when the caller doesn't pass ``context_policy=`` to
    :meth:`Runner.run`. Zero-cost: ``apply`` returns the input list
    unchanged (same identity).
    """

    name = "noop"

    async def apply(self, items: list[Item], *, ctx: PolicyContext) -> list[Item]:
        return items

    async def apply_reactive(
        self, items: list[Item], *, ctx: PolicyContext
    ) -> list[Item]:
        return items


# ---------------------------------------------------------------------------
# LLM-backed summarizer
# ---------------------------------------------------------------------------


class ProviderSummarizer:
    """Summarize a transcript by asking an LLM (typically a cheaper model).

    Args:
        provider: Override the provider used for the summarization call.
            When ``None`` we reuse the same provider the runner is about to
            call — works out of the box but bills at the main model's rate.
            Pass an explicit ``Provider`` (e.g. a Haiku adapter) to save
            cost on long conversations.
        prompt: System prompt sent to the summarizer; defaults to
            :data:`DEFAULT_SUMMARY_PROMPT`.
        settings: Optional :class:`~lovia.ModelSettings` for the call.
            Defaults to ``temperature=0`` for deterministic summaries.
    """

    def __init__(
        self,
        provider: Any | None = None,
        *,
        prompt: str = DEFAULT_SUMMARY_PROMPT,
        settings: ModelSettings | None = None,
    ) -> None:
        self.provider = provider
        self.prompt = prompt
        self.settings = settings or ModelSettings(temperature=0)

    async def summarize(self, items: list[Item], *, ctx: PolicyContext) -> str:
        provider = self.provider or ctx.provider
        # Flatten the transcript into a single user message — we don't want
        # the summarizer to "continue" the conversation, just describe it.
        transcript_text = _transcript_to_text(items)
        input_items: list[Item] = [
            InputMessageItem(role="system", content=self.prompt),
            InputMessageItem(
                role="user",
                content=(
                    "Summarize the following agent transcript per the rules above. "
                    "Begin your response with the five headings.\n\n"
                    "<transcript>\n" + transcript_text + "\n</transcript>"
                ),
            ),
        ]
        chunks: list[str] = []
        async for delta in provider.stream(input_items, settings=self.settings):
            text = getattr(delta, "text", None)
            # Only collect plain text deltas — usage/finish/tool deltas are
            # not relevant for the summary body.
            if isinstance(text, str) and getattr(delta, "type", "") == "text_delta":
                chunks.append(text)
        summary = "".join(chunks).strip()
        if not summary:
            # Surface as ValueError so the policy can decide whether to
            # fall back (e.g. reactive path) or propagate.
            raise ValueError("Summarizer returned empty text")
        return summary


def _transcript_to_text(items: list[Item]) -> str:
    """Best-effort flat rendering of items for the summarizer prompt."""
    out: list[str] = []
    for it in items:
        if isinstance(it, InputMessageItem):
            role = it.role
            content = (
                it.content
                if isinstance(it.content, str)
                else _blocks_to_text(it.content)
            )
            out.append(f"[{role}] {content}")
        elif isinstance(it, MessageOutputItem):
            out.append(f"[assistant] {it.content}")
        elif isinstance(it, ReasoningItem):
            # Skip reasoning — it's noisy and providers don't always echo it.
            continue
        elif isinstance(it, ToolCallItem):
            out.append(f"[tool_call:{it.name}] {it.arguments}")
        elif isinstance(it, ToolCallOutputItem):
            out.append(f"[tool_result] {it.output}")
    return "\n".join(out)


def _blocks_to_text(blocks: Any) -> str:
    try:
        # Reuse the framework's helper rather than re-implementing it.
        from .content import text_of

        return text_of(blocks)
    except Exception:  # pragma: no cover - defensive
        return str(blocks)


# ---------------------------------------------------------------------------
# Summarizing context policy (the default useful implementation)
# ---------------------------------------------------------------------------


_SUMMARY_PREFIX = "[Conversation summary — prior turns compacted]\n\n"
_SUMMARY_SUFFIX = "\n\n[End summary]"
_SUMMARY_OPEN = "[Conversation summary — prior turns compacted]"
_SUMMARY_CLOSE = "[End summary]"


def extract_compaction_summary(items: list[Item]) -> str | None:
    """Return the raw summary text from a compacted item list, if present."""
    if not items:
        return None
    head = items[0]
    if not isinstance(head, InputMessageItem):
        return None
    content = head.content
    if not isinstance(content, str):
        return None
    if _SUMMARY_OPEN not in content:
        return None
    body = content.split(_SUMMARY_OPEN, 1)[1]
    if _SUMMARY_CLOSE in body:
        body = body.rsplit(_SUMMARY_CLOSE, 1)[0]
    return body.strip()


@dataclass
class _CompactionConsecutiveFailures:
    """Small circuit breaker so we don't burn API quota on a broken summarizer."""

    max_failures: int = 3
    count: int = 0

    def record_success(self) -> None:
        self.count = 0

    def record_failure(self) -> None:
        self.count += 1

    @property
    def tripped(self) -> bool:
        return self.count >= self.max_failures


class SummarizingContextPolicy:
    """LLM-summarization context policy with reactive fallback.

    Behavior on every ``apply``:

    1. Compute a token threshold ``int(max_tokens * compact_at_ratio)``.
       If ``max_tokens`` was not set explicitly, fall back to
       ``provider.context_window(model)``. If neither is available,
       proactive compaction is skipped (reactive path still works).
    2. Estimate the current prompt size, preferring the last-reported
       ``usage.prompt_tokens`` and falling back to ``estimate_tokens``.
    3. If under threshold: optionally apply a "micro" compaction
       (placeholders for old tool results when
       ``keep_recent_tool_results`` is set), then return.
    4. If at/over threshold: ask the summarizer to produce a summary,
       then return ``[summary_item, *safe_window(items, tail=keep_recent_messages)]``.

    On :meth:`apply_reactive` the same compaction runs with a more
    aggressive tail (``reactive_keep_recent_messages``, default 5).

    A circuit breaker stops compaction attempts after
    ``max_consecutive_failures`` failed summarizations in a row; the
    original :class:`~lovia.ContextOverflowError` then propagates.
    """

    name = "summarizing"

    def __init__(
        self,
        *,
        max_tokens: int | None = None,
        compact_at_ratio: float = 0.8,
        keep_recent_messages: int = 10,
        keep_recent_tool_results: int | None = None,
        reactive_keep_recent_messages: int = 5,
        summarizer: Summarizer | None = None,
        summary_prompt: str = DEFAULT_SUMMARY_PROMPT,
        archive: ArchiveCallback | None = None,
        max_consecutive_failures: int = 3,
    ) -> None:
        if not 0 < compact_at_ratio < 1:
            raise ValueError("compact_at_ratio must be between 0 and 1 (exclusive)")
        if keep_recent_messages < 1:
            raise ValueError("keep_recent_messages must be >= 1")
        self.max_tokens = max_tokens
        self.compact_at_ratio = compact_at_ratio
        self.keep_recent_messages = keep_recent_messages
        self.keep_recent_tool_results = keep_recent_tool_results
        self.reactive_keep_recent_messages = reactive_keep_recent_messages
        self.summarizer: Summarizer = summarizer or ProviderSummarizer(
            prompt=summary_prompt
        )
        self.archive = archive
        self._breaker = _CompactionConsecutiveFailures(
            max_failures=max_consecutive_failures
        )

    # -- entry points ---------------------------------------------------------

    async def apply(self, items: list[Item], *, ctx: PolicyContext) -> list[Item]:
        threshold = self._threshold(ctx)
        if threshold is None:
            # No window info → can't act proactively; only L2 (if enabled).
            return self._maybe_micro_compact(items)
        # ``last_prompt_tokens`` reflects the prompt size of the *previous*
        # turn. Since then we've appended the assistant reply, tool outputs,
        # and (often) a new user message, so it systematically under-counts.
        # Always cross-check against an estimate of the current items_log
        # and take the larger of the two — otherwise a single large tool
        # result can push us past the model's hard cap before the next
        # turn's usage number arrives. See the regression in
        # ``test_summarizing_policy_uses_current_items_when_stale``.
        estimate = _estimate_tokens(ctx.provider, items)
        last = ctx.last_prompt_tokens or 0
        tokens = max(estimate, last)
        if tokens < threshold:
            return self._maybe_micro_compact(items)
        logger.info(
            "context.compact.proactive: triggering compaction "
            "(tokens≈%d, threshold=%d, items=%d, last_prompt=%s)",
            tokens,
            threshold,
            len(items),
            last or None,
        )
        return await self._compact(items, ctx=ctx, reactive=False)

    async def apply_reactive(
        self, items: list[Item], *, ctx: PolicyContext
    ) -> list[Item]:
        logger.warning(
            "context.compact.reactive: provider reported overflow; "
            "compacting (items=%d)",
            len(items),
        )
        return await self._compact(items, ctx=ctx, reactive=True)

    # -- internals ------------------------------------------------------------

    def _threshold(self, ctx: PolicyContext) -> int | None:
        cap = self.max_tokens
        if cap is None:
            cap = _provider_context_window(ctx.provider, ctx.model)
        if cap is None:
            return None
        return max(1, int(cap * self.compact_at_ratio))

    async def _compact(
        self,
        items: list[Item],
        *,
        ctx: PolicyContext,
        reactive: bool,
    ) -> list[Item]:
        if self._breaker.tripped:
            # Don't keep hammering a broken summarizer; just return as-is and
            # let the caller surface the underlying overflow.
            logger.warning(
                "context.compact: circuit breaker tripped after %d "
                "consecutive failures; skipping compaction",
                self._breaker.count,
            )
            return items
        try:
            summary = await self.summarizer.summarize(items, ctx=ctx)
        except Exception as exc:
            self._breaker.record_failure()
            logger.warning(
                "context.compact: summarizer failed (%s: %s); failure %d/%d",
                type(exc).__name__,
                exc,
                self._breaker.count,
                self._breaker.max_failures,
            )
            raise
        self._breaker.record_success()

        tail = (
            self.reactive_keep_recent_messages
            if reactive
            else self.keep_recent_messages
        )
        kept = safe_window(items, tail=tail)
        summary_item = InputMessageItem(
            role="user",
            content=f"{_SUMMARY_PREFIX}{summary}{_SUMMARY_SUFFIX}",
        )
        new_items: list[Item] = [summary_item, *kept]
        logger.info(
            "context.compact.done: reactive=%s, items %d → %d, "
            "kept_tail=%d, summary_chars=%d",
            reactive,
            len(items),
            len(new_items),
            tail,
            len(summary),
        )

        if self.archive is not None:
            try:
                await self.archive(
                    ArchiveEvent(
                        session_id=ctx.session_id,
                        items_before=list(items),
                        items_after=list(new_items),
                        summary=summary,
                        reactive=reactive,
                    )
                )
            except Exception as exc:
                # Archive is best-effort; don't let an audit-sink outage
                # crash a live run.
                logger.warning(
                    "context.archive: callback failed (%s: %s); ignoring",
                    type(exc).__name__,
                    exc,
                )
        return new_items

    def _maybe_micro_compact(self, items: list[Item]) -> list[Item]:
        """L2: replace older ``ToolCallOutputItem`` payloads with a placeholder.

        Only runs when ``keep_recent_tool_results`` is set. Returns the same
        list object when nothing changes so the runner's identity check
        skips a no-op session write.
        """
        n = self.keep_recent_tool_results
        if n is None:
            return items
        outputs = [
            (i, it) for i, it in enumerate(items) if isinstance(it, ToolCallOutputItem)
        ]
        if len(outputs) <= n:
            return items
        to_compact = outputs[:-n]
        changed = False
        new_items = list(items)
        for idx, it in to_compact:
            # Skip already-compacted entries and short outputs that aren't
            # worth replacing.
            if len(it.output) <= 120 or it.output.startswith(_MICRO_PLACEHOLDER):
                continue
            new_items[idx] = ToolCallOutputItem(
                call_id=it.call_id,
                output=_MICRO_PLACEHOLDER,
                raw=None,
                is_error=it.is_error,
            )
            changed = True
        return new_items if changed else items


_MICRO_PLACEHOLDER = "[Earlier tool result compacted. Re-run the tool if you need it.]"
