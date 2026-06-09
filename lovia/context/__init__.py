"""Context-window management for long-running agent conversations.

Compaction is a pure per-call view transform: a :class:`ContextPolicy` shapes
only what is sent to the provider for one model call and never mutates the
transcript or the Session.
"""

from .policy import (
    DEFAULT_SUMMARY_PROMPT,
    CompactingContextPolicy,
    CompactionRequest,
    ContextPolicy,
    ContextResult,
    ContextSummarizer,
    LLMSummarizer,
    NoopContextPolicy,
    make_summary_entry,
)

__all__ = [
    "CompactingContextPolicy",
    "CompactionRequest",
    "ContextPolicy",
    "ContextResult",
    "ContextSummarizer",
    "DEFAULT_SUMMARY_PROMPT",
    "LLMSummarizer",
    "NoopContextPolicy",
    "make_summary_entry",
]
