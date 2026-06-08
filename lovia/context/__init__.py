"""Context compaction policies and helpers."""

from .archive import ArchiveRef, CompactionArchive, FileCompactionArchive
from .policy import (
    DEFAULT_SUMMARY_PROMPT,
    CompactingContextPolicy,
    ContextPolicy,
    ContextPolicyResult,
    ContextSummarizer,
    LLMSummarizer,
    NoopContextPolicy,
    PolicyContext,
    make_summary_entry,
)
from .stages import (
    ContextStage,
    MiddleTrimStage,
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
    "ContextSummarizer",
    "DEFAULT_SUMMARY_PROMPT",
    "FileCompactionArchive",
    "LLMSummarizer",
    "MiddleTrimStage",
    "NoopContextPolicy",
    "PolicyContext",
    "StageResult",
    "ToolResultBudgetStage",
    "ToolResultRetentionStage",
    "make_summary_entry",
]
