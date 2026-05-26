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


class ApprovalDenied(LoviaError):
    """Raised when a tool requiring approval is explicitly denied."""
