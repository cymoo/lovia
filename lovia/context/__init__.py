"""Context compaction policies and helpers."""

from .archive import ArchiveRef, CompactionArchive, FileCompactionArchive
from .policy import (
    DEFAULT_SUMMARY_PROMPT,
    CompactingContextPolicy,
    ContextPolicy,
    ContextPolicyResult,
    NoopContextPolicy,
    PolicyContext,
    ProviderSummarizer,
    Summarizer,
    make_summary_entry,
)
from .stages import (
    ContextStage,
    MiddleSnipStage,
    StageResult,
    ToolResultBudgetStage,
    ToolResultRetentionStage,
)

__all__ = [
    "ArchiveRef",
    "CompactionArchive",
    "CompactingContextPolicy",
    "ContextPolicy",
    "ContextPolicyResult",
    "ContextStage",
    "DEFAULT_SUMMARY_PROMPT",
    "FileCompactionArchive",
    "MiddleSnipStage",
    "NoopContextPolicy",
    "PolicyContext",
    "ProviderSummarizer",
    "StageResult",
    "Summarizer",
    "ToolResultBudgetStage",
    "ToolResultRetentionStage",
    "make_summary_entry",
]
