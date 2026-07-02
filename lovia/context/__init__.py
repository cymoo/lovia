"""Context-window management for long-running agent conversations.

Compaction is a per-call view transform: a :class:`ContextPolicy` shapes only
what is sent to the provider for one model call and never mutates the
transcript or the Session. The default :class:`Compaction` is *sticky*:
its decisions (cleared and offloaded tool results, the running summary)
are recorded in per-run scratch state and replayed verbatim on later calls,
so the rendered prompt prefix stays byte-stable across turns — prompt-cache
friendly — while the stored history remains the untouched source of truth.

Layers, bottom up:

* :mod:`~lovia.context.tokens` — token estimation and window watermarks.
* :mod:`~lovia.context.state` — the sticky decision record.
* :mod:`~lovia.context.render` — pure transcript+state → view rendering.
* :mod:`~lovia.context.stages` — composable strategies (offload, clear,
  summarize) that record decisions cheap-first.
* :mod:`~lovia.context.compaction` — the default policy orchestrating stages
  under trigger/target hysteresis.
"""

from .compaction import Compaction
from .policy import (
    CompactionRequest,
    ContextPolicy,
    ContextResult,
    NoopContextPolicy,
)
from .prompts import (
    REQUIRED_SECTIONS,
    SUMMARY_SYSTEM_PROMPT,
    SUMMARY_WRAPPER,
)
from .render import (
    clear_marker,
    offload_marker,
    render_view,
    summary_entry,
)
from .state import CompactionState, OffloadRecord, SummaryState
from .stages import (
    ClearToolResults,
    OffloadToolResults,
    Stage,
    StageContext,
    SummarizeHistory,
)
from .store import FileResultStore, InMemoryResultStore, ResultStore
from .summarizer import LLMSummarizer, Summarizer, transcript_to_text
from .tokens import TokenBudget, TokenCounter

__all__ = [
    "ClearToolResults",
    "CompactionRequest",
    "CompactionState",
    "Compaction",
    "ContextPolicy",
    "ContextResult",
    "FileResultStore",
    "InMemoryResultStore",
    "LLMSummarizer",
    "NoopContextPolicy",
    "OffloadRecord",
    "OffloadToolResults",
    "REQUIRED_SECTIONS",
    "ResultStore",
    "SUMMARY_SYSTEM_PROMPT",
    "SUMMARY_WRAPPER",
    "Stage",
    "StageContext",
    "SummarizeHistory",
    "Summarizer",
    "SummaryState",
    "TokenBudget",
    "TokenCounter",
    "clear_marker",
    "offload_marker",
    "render_view",
    "summary_entry",
    "transcript_to_text",
]
