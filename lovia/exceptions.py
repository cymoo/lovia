"""Exception types used across lovia.

The hierarchy is intentionally shallow: every error inherits from ``LoviaError``
so application code can catch the whole framework with a single ``except``.

Every framework exception carries an optional ``hint`` — a short, actionable
suggestion appended to ``str(exc)`` so users see "what to try next" without
digging into the traceback.
"""

from __future__ import annotations


class LoviaError(Exception):
    """Base class for all lovia errors.

    Subclasses may pass ``hint=`` to surface a one-line suggestion in the
    rendered message.
    """

    hint: str | None

    def __init__(
        self, message: str = "", *, hint: str | None = None, **extra: object
    ) -> None:
        super().__init__(message)
        self.hint = hint
        for k, v in extra.items():
            setattr(self, k, v)

    def __str__(self) -> str:
        base = super().__str__()
        if self.hint:
            return f"{base}\n  hint: {self.hint}" if base else self.hint
        return base


class UserError(LoviaError):
    """Raised when the caller has misconfigured the framework.

    Use this for problems the user can fix without inspecting tracebacks
    (e.g. unknown model name, missing dependency, invalid output type).
    """


class ProviderError(LoviaError):
    """Raised when a model provider returns an error or malformed payload.

    Extra fields populated by adapters when available:

    * ``vendor`` — short provider id (``"openai"``, ``"anthropic"``, ...).
    * ``model`` — the model name that produced the error.
    * ``status_code`` — HTTP status if applicable.
    * ``retryable`` — best-effort guess at whether retrying may succeed.
    """

    vendor: str | None = None
    model: str | None = None
    status_code: int | None = None
    retryable: bool | None = None


class ToolError(LoviaError):
    """Raised when tool invocation fails in a way the framework should surface.

    Tools may also raise arbitrary exceptions; those are caught by the runner
    and reported to the model as a tool error message. Use ``ToolError``
    explicitly when *you* (the tool author) want to convey a structured
    failure with a helpful hint.
    """

    tool_name: str | None = None


class InvalidToolArguments(ToolError):
    """Raised when a tool call's arguments fail schema validation.

    Deterministic for the given arguments, so :func:`lovia.tools.run_tool`
    does not retry it — the same args would fail the same way. It surfaces to
    the model as a tool-error result carrying the validation message it needs
    to correct the call.
    """


class MaxTurnsExceeded(LoviaError):
    """Raised when the runner exceeds ``max_turns`` without producing output."""


class OutputValidationError(LoviaError):
    """Raised when the model output cannot be parsed into ``output_type``.

    Extra fields:

    * ``raw`` — the raw model output (truncated) for debugging.
    * ``output_type_name`` — human-readable type the parser expected.
    """

    raw: str | None = None
    output_type_name: str | None = None


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


class MCPError(LoviaError):
    """Raised when an MCP server connection or tool call fails.

    Wraps the underlying transport/protocol exception so the model and the
    caller see a consistent, hint-bearing error instead of a raw
    ``BrokenPipeError``. Protocol-level *tool* failures reported via an MCP
    ``isError`` response are NOT raised — they are rendered back to the model
    so it can self-correct.

    Extra field populated when available:

    * ``tool_name`` — the MCP tool whose call failed.
    """

    tool_name: str | None = None
