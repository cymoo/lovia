"""Token accounting for context compaction.

Two small pieces:

* :class:`TokenCounter` — fast per-entry token *estimates*. Field-length
  arithmetic (no serialization), flat per-image/per-file costs so a base64
  blob is not billed as text, an ``id()``-keyed memo so a long transcript is
  re-counted in O(new entries) per turn, and dispatch to a provider's own
  :class:`~lovia.providers.base.TokenEstimator` when it ships a tokenizer.
  Tool schemas — the fixed additive payload every request carries alongside
  the entries — are counted separately via :meth:`TokenCounter.count_tools`.
* :class:`TokenBudget` — the window math: usable space after reserving output
  headroom, plus the *trigger* (start compacting) and *target* (stop
  compacting) watermarks. The gap between the two is the hysteresis that
  keeps compaction bursty instead of firing every turn.

Estimates are deliberately rough; :class:`~lovia.context.Compaction`
corrects them with a calibration ratio learned from the provider's real
input-token counts.
"""

from __future__ import annotations

import json
import weakref
from dataclasses import dataclass
from typing import Sequence

from ..parts import FilePart, ImagePart, TextPart
from ..providers.base import TokenEstimator
from ..transcript import (
    AssistantTextEntry,
    InputEntry,
    ReasoningEntry,
    ToolCallEntry,
    ToolResultEntry,
    TranscriptEntry,
)

_CHARS_PER_TOKEN = 4  # the textbook heuristic; calibration absorbs the error


def _validate_watermark(value: int | float, name: str) -> None:
    """A watermark is a fraction of the usable window (float in ``(0, 1]``)
    or an absolute token count (int ``>= 1``)."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a fraction or a token count")
    if isinstance(value, float):
        if not 0 < value <= 1:
            raise ValueError(f"{name} as a fraction must be in (0, 1]")
    elif value < 1:
        raise ValueError(f"{name} as a token count must be >= 1")


def usable_tokens(window: int, reserve_output: int) -> int:
    """Prompt tokens left in ``window`` after reserving output headroom.

    ``reserve_output`` defaults to a size tuned for 100K+ windows, so it can
    exceed a small one outright (a 4K local model). Halving the window then
    beats reserving nothing and beats going negative.
    """
    if reserve_output >= window:
        return max(window // 2, 1)
    return window - reserve_output


@dataclass(frozen=True)
class TokenBudget:
    """Watermark math over a model's context window.

    Attributes:
        window: The model's total context window (prompt + output tokens).
        reserve_output: Headroom kept free for the model's reply. When it
            does not fit in ``window``, half the window is reserved instead.
        trigger: Prompt size at which compaction starts — a fraction of
            :attr:`usable` (float) or an absolute token count (int).
        target: Prompt size compaction shrinks down to, in the same units.
            Resolves below ``trigger`` — the gap is the anti-thrash
            hysteresis.
    """

    window: int
    reserve_output: int = 16_384
    trigger: int | float = 0.75
    target: int | float = 0.50

    def __post_init__(self) -> None:
        if self.window < 1:
            raise ValueError("window must be >= 1")
        if self.reserve_output < 0:
            raise ValueError("reserve_output must be >= 0")
        _validate_watermark(self.trigger, "trigger")
        _validate_watermark(self.target, "target")
        if type(self.trigger) is type(self.target) and self.target >= self.trigger:
            raise ValueError("target must be below trigger")

    @property
    def usable(self) -> int:
        """Prompt tokens available after reserving output headroom."""
        return usable_tokens(self.window, self.reserve_output)

    def _resolve(self, value: int | float) -> int:
        if isinstance(value, float):
            return max(1, int(value * self.usable))
        return max(1, min(value, self.usable))

    @property
    def trigger_tokens(self) -> int:
        """Prompt size at which compaction starts."""
        return self._resolve(self.trigger)

    @property
    def target_tokens(self) -> int:
        """Prompt size compaction tries to shrink down to.

        With mixed fraction/absolute watermarks the two can only be compared
        once the window is known; the target is capped below the trigger so
        hysteresis always exists.
        """
        return min(self._resolve(self.target), max(1, self.trigger_tokens - 1))

    def pressure(self, tokens: int) -> float:
        """Return ``tokens`` as a fraction of the usable window."""
        return tokens / self.usable


class TokenCounter:
    """Memoized per-entry token estimation.

    Estimates are O(1) per entry (string lengths only). Multimodal parts get
    flat costs — a base64-embedded image is counted as ``image_tokens``, not
    as megabytes of text. When ``provider`` implements
    :class:`~lovia.providers.base.TokenEstimator` it is consulted per entry
    instead (and still memoized, since real tokenizers are not free).

    The memo is keyed by ``id(entry)`` with a weakref liveness guard:
    transcript entries are immutable in practice (the runner only appends),
    so identity is a safe cache key as long as we detect id reuse after
    garbage collection. The memo is bounded; one counter may serve many runs.
    """

    def __init__(
        self,
        provider: object | None = None,
        *,
        image_tokens: int = 1_600,
        file_tokens: int = 2_000,
        entry_overhead: int = 8,
        memo_size: int = 8_192,
    ) -> None:
        self._estimator = provider if isinstance(provider, TokenEstimator) else None
        self.image_tokens = image_tokens
        self.file_tokens = file_tokens
        self.entry_overhead = entry_overhead
        self._memo_size = memo_size
        self._memo: dict[int, tuple[weakref.ref[TranscriptEntry], int]] = {}
        self._tool_memo: dict[int, tuple[weakref.ref[object], int]] = {}

    def count(self, entries: Sequence[TranscriptEntry]) -> int:
        """Estimated prompt tokens for ``entries``."""
        return sum(self.count_entry(entry) for entry in entries)

    def count_tools(self, tools: Sequence[object]) -> int:
        """Estimated tokens for the tool schemas sent alongside the entries.

        Measured on the same wire shape the provider receives
        (``Tool.openai_schema()``), memoized by tool identity. No
        :class:`TokenEstimator` dispatch — schemas are request framing, not
        entries — so the calibration ratio absorbs the residual.
        """
        return sum(self._count_tool(tool) for tool in tools)

    def _count_tool(self, tool: object) -> int:
        key = id(tool)
        hit = self._tool_memo.get(key)
        if hit is not None:
            ref, tokens = hit
            if ref() is tool:
                return tokens
        tokens = self._measure_tool(tool)
        if len(self._tool_memo) >= self._memo_size:
            self._tool_memo.pop(next(iter(self._tool_memo)))
        try:
            self._tool_memo[key] = (weakref.ref(tool), tokens)
        except TypeError:
            pass
        return tokens

    def _measure_tool(self, tool: object) -> int:
        schema = getattr(tool, "openai_schema", None)
        chars = 0
        if callable(schema):
            try:
                # ensure_ascii=False so CJK descriptions count as the
                # characters a tokenizer sees, not as 6-char \uXXXX escapes.
                chars = len(json.dumps(schema(), ensure_ascii=False))
            except Exception:
                chars = 0  # unknown shape: charge the flat minimum below
        return chars // _CHARS_PER_TOKEN + self.entry_overhead

    def count_entry(self, entry: TranscriptEntry) -> int:
        """Estimated tokens for one entry, memoized by identity."""
        key = id(entry)
        hit = self._memo.get(key)
        if hit is not None:
            ref, tokens = hit
            if ref() is entry:
                return tokens
        tokens = self._measure(entry)
        if len(self._memo) >= self._memo_size:
            # Evict in insertion order; old runs' entries die first anyway.
            self._memo.pop(next(iter(self._memo)))
        try:
            self._memo[key] = (weakref.ref(entry), tokens)
        except TypeError:  # pragma: no cover - entries are weakref-able
            pass
        return tokens

    def _measure(self, entry: TranscriptEntry) -> int:
        if self._estimator is not None:
            try:
                return int(self._estimator.estimate_tokens([entry]))
            except Exception:
                # A broken tokenizer must not break compaction; fall through
                # to the heuristic.
                pass
        if isinstance(entry, InputEntry):
            if isinstance(entry.content, str):
                chars = len(entry.content)
            else:
                tokens = self.entry_overhead
                for part in entry.content:
                    if isinstance(part, TextPart):
                        tokens += len(part.text) // _CHARS_PER_TOKEN
                    elif isinstance(part, ImagePart):
                        tokens += self.image_tokens
                    elif isinstance(part, FilePart):
                        tokens += self.file_tokens
                return tokens
        elif isinstance(entry, (AssistantTextEntry, ReasoningEntry)):
            chars = len(entry.content)
        elif isinstance(entry, ToolCallEntry):
            chars = len(entry.name) + len(entry.arguments)
        elif isinstance(entry, ToolResultEntry):
            chars = len(entry.output)
        else:  # pragma: no cover - exhaustive over TranscriptEntry
            chars = 0
        return chars // _CHARS_PER_TOKEN + self.entry_overhead


__all__ = ["TokenBudget", "TokenCounter", "usable_tokens"]
