"""Exception types used across lovia.

The hierarchy is intentionally shallow: every error inherits from ``LoviaError``
so application code can catch the whole framework with a single ``except``.
"""

from __future__ import annotations


class LoviaError(Exception):
    """Base class for all lovia errors."""


class UserError(LoviaError):
    """Raised when the caller has misconfigured the framework.

    Use this for problems the user can fix without inspecting tracebacks
    (e.g. unknown model name, missing dependency, invalid output type).
    """


class ProviderError(LoviaError):
    """Raised when a model provider returns an error or malformed payload."""


class ToolError(LoviaError):
    """Raised when tool invocation fails in a way the framework should surface.

    Tools may also raise arbitrary exceptions; those are caught by the runner
    and reported to the model as a tool error message.
    """


class MaxTurnsExceeded(LoviaError):
    """Raised when the runner exceeds ``max_turns`` without producing output."""


class OutputValidationError(LoviaError):
    """Raised when the model output cannot be parsed into ``output_type``."""


class BudgetExceeded(LoviaError):
    """Raised when a :class:`~lovia.RunBudget` limit is exceeded mid-run."""


class RunCancelled(LoviaError):
    """Raised when a :class:`~lovia.CancelToken` was tripped during a run."""


class GuardrailTripped(LoviaError):
    """Raised when an input or output :class:`~lovia.Guardrail` rejects a value."""


class ContextOverflowError(LoviaError):
    """Raised when a provider reports the prompt exceeds the model's context window.

    Provider adapters translate vendor-specific signals (HTTP 400 with
    ``context_length_exceeded``, "prompt is too long", etc.) into this
    framework-level error so the :class:`~lovia.Runner` can react with a
    single ``except`` clause. The original exception is preserved via
    ``raise ... from exc`` so users keep full debugging context.
    """
