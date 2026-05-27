"""Tool definition and the ``@tool`` decorator.

A :class:`Tool` is a thin wrapper around an async callable. The runner is the
only thing that ever invokes it, so we keep the surface area small:

* ``name``, ``description``, ``parameters`` form the JSON Schema the model
  sees.
* ``invoke`` runs the underlying callable with already-validated kwargs.
* ``needs_approval`` is an optional boolean (or predicate) that pauses the
  runner so the application can ask a human.

Tools can be created in three ways: by decorating a function with ``@tool``,
by subclassing :class:`Tool`, or by passing a callable directly to
:func:`as_tool`.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, get_origin, get_type_hints

from .run_context import RunContext
from .schema import function_args_schema, validate_args


# Predicate that may inspect the parsed arguments and current context to decide
# whether a tool invocation needs human approval.
ApprovalPredicate = Callable[[dict[str, Any], "RunContext"], bool]

# Tool middleware: ``before`` may mutate/replace the arguments before they
# reach the underlying callable; ``after`` may transform or replace the
# returned value before the runner serializes it back to the model. Either
# hook may be sync or async; the runner always awaits the result.
ToolBefore = Callable[
    [dict[str, Any], "RunContext"],
    "dict[str, Any] | Awaitable[dict[str, Any]]",
]
ToolAfter = Callable[
    [Any, "RunContext"],
    "Any | Awaitable[Any]",
]


@dataclass
class Tool:
    """An executable capability the model can request."""

    name: str
    description: str
    parameters: dict[str, Any]
    # The underlying callable. The runner always awaits the result, so sync
    # callables are wrapped during construction.
    invoke: Callable[[dict[str, Any], "RunContext"], Awaitable[Any]]
    needs_approval: bool | ApprovalPredicate = False
    # Optional middleware. ``before`` runs after argument validation but
    # before the underlying callable; ``after`` runs on the returned value
    # before the runner stringifies it. Use them for logging, redaction,
    # caching, mocking, rate limiting, etc.
    before: ToolBefore | None = None
    after: ToolAfter | None = None
    # When True the runner passes the RunContext as the first argument under
    # the parameter name stored in ``_context_param``.
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


async def run_tool(tool: "Tool", args: dict[str, Any], ctx: "RunContext") -> Any:
    """Invoke a tool, applying its optional ``before`` / ``after`` middleware."""
    if tool.before is not None:
        args = await _maybe_await(tool.before(args, ctx))
    result = await tool.invoke(args, ctx)
    if tool.after is not None:
        result = await _maybe_await(tool.after(result, ctx))
    return result


def tool(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    needs_approval: bool | ApprovalPredicate = False,
    before: ToolBefore | None = None,
    after: ToolAfter | None = None,
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

        @tool(name="search_web", needs_approval=True)
        async def search(ctx: RunContext[Deps], query: str) -> list[str]:
            return await ctx.context.client.search(query)
    """

    def wrap(func: Callable[..., Any]) -> Tool:
        tool_name = name or func.__name__
        tool_desc = description or (inspect.getdoc(func) or "").strip()
        parameters, _ = function_args_schema(func)

        sig = inspect.signature(func)
        context_param = _find_context_param(func, sig)
        wants_context = context_param is not None

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
            before=before,
            after=after,
            _wants_context=wants_context,
            _context_param=context_param,
        )

    if fn is None:
        return wrap
    return wrap(fn)


def _find_context_param(func: Callable[..., Any], sig: inspect.Signature) -> str | None:
    """Return the name of the parameter annotated as ``RunContext`` (or ``None``).

    We resolve annotations lazily via ``get_type_hints`` so that ``from
    __future__ import annotations`` (string-form annotations) works.
    """
    try:
        hints = get_type_hints(func, include_extras=False)
    except Exception:
        # Unresolvable annotations (forward refs to missing names, etc.)
        # silently fall through to "no context"; this matches how the rest
        # of the framework treats schema introspection.
        return None
    for name in sig.parameters:
        annotation = hints.get(name)
        if annotation is None:
            continue
        origin = get_origin(annotation) or annotation
        if origin is RunContext:
            return name
    return None
