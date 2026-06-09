"""Tool definition, the ``@tool`` decorator, and built-in tool factories.

A :class:`Tool` is a thin wrapper around an async callable. The runner is the
only thing that invokes it, so the surface area stays small:

* ``name``, ``description``, ``parameters`` form the JSON Schema the model sees.
* ``invoke`` runs the underlying callable with already-validated kwargs.
* Simple policy kwargs (``needs_approval``, ``retries``, ``timeout``,
  ``result_renderer``) cover the common cases.
* Advanced callers can pass composable ``policies``; the legacy ``wrap`` escape
  hatch is normalized into the same chain.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Protocol,
    cast,
    get_origin,
    get_type_hints,
    overload,
)

from pydantic import BaseModel

from .._types import JsonObject, JsonSchema
from ..run_context import RunContext
from ..schema import function_args_schema, validate_args

# Predicate that may inspect the parsed arguments and current context to decide
# whether a tool invocation needs human approval.
ApprovalPredicate = Callable[[dict[str, Any], "RunContext"], bool]

# A ``wrap`` callable receives the underlying ``invoke``, the validated args,
# and the run context. It must return (or await) the tool result. Use it to
# insert custom behaviour around a single attempt (caching, mocking, custom
# auth, redaction). Retries and timeout, when configured, are applied *around*
# wrap — i.e. wrap sees one attempt at a time.
ToolInvoker = Callable[[dict[str, Any], "RunContext"], Awaitable[Any]]
ToolWrap = Callable[[ToolInvoker, dict[str, Any], "RunContext"], Awaitable[Any]]


class ToolPolicy(Protocol):
    """Composable behavior around one tool attempt.

    Policies receive the next callable in the chain plus validated arguments
    and context. They can mutate arguments, short-circuit, retry internally,
    redact results, cache, rate-limit, etc. Runner-level retries/timeouts are
    still applied around the composed attempt.
    """

    def __call__(
        self,
        invoke: ToolInvoker,
        args: dict[str, Any],
        ctx: "RunContext",
    ) -> Awaitable[Any]: ...


@dataclass(frozen=True)
class WrapPolicy:
    """Adapter that turns a legacy ``wrap`` callable into a ``ToolPolicy``."""

    wrap: ToolWrap

    async def __call__(
        self,
        invoke: ToolInvoker,
        args: dict[str, Any],
        ctx: "RunContext",
    ) -> Any:
        return await self.wrap(invoke, args, ctx)


# Render the raw return value as the string the model receives. ``None`` uses
# the default renderer (str for strings, json.dumps for everything else).
ToolResultRenderer = Callable[[Any, "RunContext"], "str | Awaitable[str]"]


@dataclass
class Tool:
    """An executable capability the model can request."""

    name: str
    description: str
    parameters: JsonSchema
    # The underlying callable. The runner always awaits the result, so sync
    # callables are wrapped during construction.
    invoke: Callable[[dict[str, Any], "RunContext"], Awaitable[Any]]
    # ---- flat policies ----
    needs_approval: bool | ApprovalPredicate = False
    # Maximum total number of attempts (1 = no retry). ``None`` means "use the
    # agent's default_tool_retries".
    retries: int | None = None
    # Per-attempt timeout in seconds. ``None`` means no timeout (or the
    # agent's default_tool_timeout if set).
    timeout: float | None = None
    # Optional custom renderer for the result string the model sees.
    result_renderer: ToolResultRenderer | None = None
    # Optional escape hatch for behaviours that don't fit a flat field.
    wrap: ToolWrap | None = None
    # Advanced per-attempt policy chain. Policies compose in list order.
    policies: tuple[ToolPolicy, ...] = field(default_factory=tuple)
    # When True the runner passes the RunContext to invoke as the named kwarg.
    _wants_context: bool = field(default=False, repr=False)
    _context_param: str | None = field(default=None, repr=False)

    def requires_approval(self, args: dict[str, Any], ctx: "RunContext") -> bool:
        if callable(self.needs_approval) and not isinstance(self.needs_approval, bool):
            return bool(self.needs_approval(args, ctx))
        return bool(self.needs_approval)

    def openai_schema(self) -> JsonObject:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _to_jsonable(value: Any) -> Any:
    """Recursively convert Pydantic models and dataclasses to JSON-safe types."""
    if isinstance(value, BaseModel):
        return value.model_dump()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    return value


def default_result_renderer(result: Any) -> str:
    """Render a tool result as the string the model will see."""
    if isinstance(result, str):
        return result
    result = _to_jsonable(result)
    try:
        return json.dumps(result, ensure_ascii=False)
    except TypeError:
        return str(result)


async def run_tool(
    tool: "Tool",
    args: dict[str, Any],
    ctx: "RunContext",
    *,
    default_retries: int = 1,
    default_timeout: float | None = None,
) -> Any:
    """Invoke ``tool`` honouring policy chain / retries / timeout.

    Retries and timeout are applied *around* the per-attempt policy chain so
    each policy sees a single attempt unless it intentionally loops itself.
    """
    attempts = tool.retries if tool.retries is not None else default_retries
    attempts = max(1, attempts)
    timeout = tool.timeout if tool.timeout is not None else default_timeout

    policies: tuple[ToolPolicy, ...]
    if tool.wrap is not None:
        policies = (*tool.policies, WrapPolicy(tool.wrap))
    else:
        policies = tool.policies

    async def one_attempt(a: dict[str, Any], c: "RunContext") -> Any:
        return await apply_tool_policies(tool.invoke, policies, a, c)

    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            if timeout is not None:
                return await asyncio.wait_for(one_attempt(args, ctx), timeout=timeout)
            return await one_attempt(args, ctx)
        except Exception as exc:  # noqa: BLE001 — we want to retry any tool error
            last_exc = exc
            if attempt >= attempts:
                raise
            # Bounded exponential backoff. Kept tiny because tools are usually
            # local; if you need fancier behaviour, use ``wrap``.
            await asyncio.sleep(min(0.5, 0.05 * (2 ** (attempt - 1))))
    # Unreachable, but keeps type-checkers happy.
    assert last_exc is not None
    raise last_exc


async def apply_tool_policies(
    invoke: ToolInvoker,
    policies: tuple[ToolPolicy, ...],
    args: dict[str, Any],
    ctx: "RunContext",
) -> Any:
    """Apply ``policies`` in order around ``invoke``."""

    next_in_chain = invoke
    for policy in reversed(policies):
        inner = next_in_chain

        async def wrapped(
            a: dict[str, Any],
            c: "RunContext",
            *,
            _policy: ToolPolicy = policy,
            _inner: ToolInvoker = inner,
        ) -> Any:
            return await _policy(_inner, a, c)

        next_in_chain = wrapped
    return await next_in_chain(args, ctx)


async def render_tool_result(
    tool: "Tool",
    result: Any,
    ctx: "RunContext",
    *,
    default: ToolResultRenderer | None = None,
) -> str:
    """Convert a raw tool result into the string the model receives.

    Resolution order:

    1. The tool's own ``result_renderer`` if set.
    2. ``default`` (typically ``agent.tool_result_renderer``) if provided.
    3. The framework's :func:`default_result_renderer` (``str`` /
       ``json.dumps``).
    """
    renderer = tool.result_renderer or default
    if renderer is None:
        return default_result_renderer(result)
    rendered = renderer(result, ctx)
    rendered_value = await _maybe_await(rendered)
    return rendered_value if isinstance(rendered_value, str) else str(rendered_value)


@overload
def tool(
    fn: Callable[..., Any],
    *,
    name: str | None = None,
    description: str | None = None,
    needs_approval: bool | ApprovalPredicate = False,
    retries: int | None = None,
    timeout: float | None = None,
    result_renderer: ToolResultRenderer | None = None,
    wrap: ToolWrap | None = None,
    policies: list[ToolPolicy] | tuple[ToolPolicy, ...] = (),
    strict: bool = False,
) -> Tool: ...


@overload
def tool(
    fn: None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    needs_approval: bool | ApprovalPredicate = False,
    retries: int | None = None,
    timeout: float | None = None,
    result_renderer: ToolResultRenderer | None = None,
    wrap: ToolWrap | None = None,
    policies: list[ToolPolicy] | tuple[ToolPolicy, ...] = (),
    strict: bool = False,
) -> Callable[[Callable[..., Any]], Tool]: ...


def tool(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    needs_approval: bool | ApprovalPredicate = False,
    retries: int | None = None,
    timeout: float | None = None,
    result_renderer: ToolResultRenderer | None = None,
    wrap: ToolWrap | None = None,
    policies: list[ToolPolicy] | tuple[ToolPolicy, ...] = (),
    strict: bool = False,
) -> Tool | Callable[[Callable[..., Any]], Tool]:
    """Decorate a function to turn it into a :class:`Tool`.

    The function may be sync or async; sync functions are run on a thread so
    they don't block the event loop. Tools opt in to receiving the
    :class:`RunContext` by annotating their first parameter as
    ``RunContext`` or ``RunContext[Deps]``. The parameter name does not
    matter — only the annotation does.

    Parameter metadata may be carried via :data:`typing.Annotated`. Both
    ``Annotated[str, "the query"]`` (bare string description) and
    ``Annotated[int, Field(ge=0)]`` (full pydantic ``Field``) are recognised.

    When ``strict=True`` the generated JSON Schema is marked
    ``additionalProperties: False`` and every argument becomes required —
    matching OpenAI's strict-mode requirements.
    """

    def make(func: Callable[..., Any]) -> Tool:
        tool_name = name or func.__name__
        tool_desc = description or (inspect.getdoc(func) or "").strip()
        parameters, _ = function_args_schema(func, strict=strict)

        sig = inspect.signature(func)
        context_param = _find_context_param(func, sig)
        is_async = inspect.iscoroutinefunction(func)

        async def invoke(args: dict[str, Any], ctx: "RunContext") -> Any:
            cleaned = validate_args(func, args)
            kwargs = dict(cleaned)
            if context_param is not None:
                kwargs[context_param] = ctx
            if is_async:
                return await cast(Awaitable[Any], func(**kwargs))
            # Offload sync work so we don't block the event loop.
            return await asyncio.to_thread(lambda: func(**kwargs))

        return Tool(
            name=tool_name,
            description=tool_desc,
            parameters=parameters,
            invoke=invoke,
            needs_approval=needs_approval,
            retries=retries,
            timeout=timeout,
            result_renderer=result_renderer,
            wrap=wrap,
            policies=tuple(policies),
            _wants_context=context_param is not None,
            _context_param=context_param,
        )

    if fn is None:
        return make
    return make(fn)


def _find_context_param(func: Callable[..., Any], sig: inspect.Signature) -> str | None:
    """Return the name of the parameter annotated as ``RunContext`` (or ``None``).

    Annotations are resolved lazily via ``get_type_hints`` so ``from __future__
    import annotations`` (string-form annotations) keeps working.
    """
    try:
        hints = get_type_hints(func, include_extras=False)
    except Exception:
        # Unresolvable forward refs etc. fall through to "no context"; this
        # matches how the rest of the framework treats schema introspection.
        return None
    for pname in sig.parameters:
        annotation = hints.get(pname)
        if annotation is None:
            continue
        origin = get_origin(annotation) or annotation
        if origin is RunContext:
            return pname
    return None


from .coding_tools import coding_tools  # noqa: E402
from .edit_file import edit_file  # noqa: E402
from .glob import glob  # noqa: E402
from .http import http_fetch  # noqa: E402
from .human import HumanChannel, HumanQuestion, ask_human  # noqa: E402
from .list_dir import list_dir  # noqa: E402
from .read_file import read_file  # noqa: E402
from .recall import recall_tool_result  # noqa: E402
from .search import (  # noqa: E402
    DuckDuckGoSearch,
    SearchResult,
    WebSearch,
    duckduckgo_search_tool,
    web_search,
)
from .shell import shell  # noqa: E402
from .time import now, sleep  # noqa: E402
from .todo import Status, Todo, TodoList, todo_tools  # noqa: E402
from .write_file import write_file  # noqa: E402

__all__ = [
    "ApprovalPredicate",
    "DuckDuckGoSearch",
    "HumanChannel",
    "HumanQuestion",
    "SearchResult",
    "Status",
    "Todo",
    "TodoList",
    "Tool",
    "ToolInvoker",
    "ToolPolicy",
    "ToolResultRenderer",
    "ToolWrap",
    "WrapPolicy",
    "WebSearch",
    "apply_tool_policies",
    "ask_human",
    "coding_tools",
    "default_result_renderer",
    "duckduckgo_search_tool",
    "edit_file",
    "glob",
    "http_fetch",
    "list_dir",
    "now",
    "read_file",
    "recall_tool_result",
    "render_tool_result",
    "run_tool",
    "shell",
    "sleep",
    "think",
    "tool",
    "todo_tools",
    "web_search",
    "write_file",
]
