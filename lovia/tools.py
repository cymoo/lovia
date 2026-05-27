"""Tool definition and the ``@tool`` decorator.

A :class:`Tool` is a thin wrapper around an async callable. The runner is the
only thing that invokes it, so the surface area stays small:

* ``name``, ``description``, ``parameters`` form the JSON Schema the model
  sees.
* ``invoke`` runs the underlying callable with already-validated kwargs.
* The remaining fields are **flat policies** — boolean / numeric knobs the
  runner respects (``needs_approval``, ``retries``, ``timeout``,
  ``result_renderer``) plus a single ``wrap`` escape hatch for the rare case
  where flat knobs aren't enough (caching, custom auth, mocking, ...).

There is intentionally no "middleware" concept. ``wrap`` is one callable; if
you want to combine two, write a third that composes them.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, get_origin, get_type_hints

from .run_context import RunContext
from .schema import function_args_schema, validate_args


# Predicate that may inspect the parsed arguments and current context to decide
# whether a tool invocation needs human approval.
ApprovalPredicate = Callable[[dict[str, Any], "RunContext"], bool]

# A ``wrap`` callable receives the underlying ``invoke``, the validated args,
# and the run context. It must return (or await) the tool result. Use it to
# insert custom behaviour around a single attempt (caching, mocking, custom
# auth, redaction). Retries and timeout, when configured, are applied *around*
# wrap — i.e. wrap sees one attempt at a time.
ToolWrap = Callable[
    [Callable[[dict[str, Any], "RunContext"], Awaitable[Any]], dict[str, Any], "RunContext"],
    Awaitable[Any],
]

# Render the raw return value as the string the model receives. ``None`` uses
# the default renderer (str for strings, json.dumps for everything else).
ToolResultRenderer = Callable[[Any, "RunContext"], "str | Awaitable[str]"]


@dataclass
class Tool:
    """An executable capability the model can request."""

    name: str
    description: str
    parameters: dict[str, Any]
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
    # When True the runner passes the RunContext to invoke as the named kwarg.
    _wants_context: bool = field(default=False, repr=False)
    _context_param: str | None = field(default=None, repr=False)

    def requires_approval(self, args: dict[str, Any], ctx: "RunContext") -> bool:
        if callable(self.needs_approval) and not isinstance(self.needs_approval, bool):
            return bool(self.needs_approval(args, ctx))
        return bool(self.needs_approval)

    def openai_schema(self) -> dict[str, Any]:
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


def default_result_renderer(result: Any) -> str:
    """Render a tool result as the string the model will see."""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str, ensure_ascii=False)
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
    """Invoke ``tool`` honouring its ``wrap`` / ``retries`` / ``timeout`` policies.

    Retries and timeout are applied *around* ``wrap`` so a wrap implementation
    only ever sees a single attempt (and can rely on its own state without
    worrying about re-entrant calls).
    """
    attempts = tool.retries if tool.retries is not None else default_retries
    attempts = max(1, attempts)
    timeout = tool.timeout if tool.timeout is not None else default_timeout

    async def one_attempt(a: dict[str, Any], c: "RunContext") -> Any:
        if tool.wrap is not None:
            return await tool.wrap(tool.invoke, a, c)
        return await tool.invoke(a, c)

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


async def render_tool_result(tool: "Tool", result: Any, ctx: "RunContext") -> str:
    """Convert a raw tool result into the string the model receives."""
    if tool.result_renderer is None:
        return default_result_renderer(result)
    rendered = tool.result_renderer(result, ctx)
    rendered = await _maybe_await(rendered)
    return rendered if isinstance(rendered, str) else str(rendered)


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
) -> Any:
    """Decorate a function to turn it into a :class:`Tool`.

    The function may be sync or async; sync functions are run on a thread so
    they don't block the event loop. Tools opt in to receiving the
    :class:`RunContext` by annotating their first parameter as
    ``RunContext`` or ``RunContext[Deps]``. The parameter name does not
    matter — only the annotation does.

    Examples::

        @tool
        async def add(a: int, b: int) -> int:
            '''Add two integers.'''
            return a + b

        @tool(name="search_web", needs_approval=True, retries=3, timeout=10)
        async def search(ctx: RunContext[Deps], query: str) -> list[str]:
            return await ctx.context.client.search(query)
    """

    def make(func: Callable[..., Any]) -> Tool:
        tool_name = name or func.__name__
        tool_desc = description or (inspect.getdoc(func) or "").strip()
        parameters, _ = function_args_schema(func)

        sig = inspect.signature(func)
        context_param = _find_context_param(func, sig)
        is_async = inspect.iscoroutinefunction(func)

        async def invoke(args: dict[str, Any], ctx: "RunContext") -> Any:
            cleaned = validate_args(func, args)
            kwargs = dict(cleaned)
            if context_param is not None:
                kwargs[context_param] = ctx
            if is_async:
                return await func(**kwargs)
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
