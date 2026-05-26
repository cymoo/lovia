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
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from .schema import function_args_schema, validate_args

if TYPE_CHECKING:
    from .runner import RunContext


# Predicate that may inspect the parsed arguments and current context to decide
# whether a tool invocation needs human approval.
ApprovalPredicate = Callable[[dict[str, Any], "RunContext"], bool]


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
    # When True the runner passes the RunContext as the first argument.
    _wants_context: bool = field(default=False, repr=False)

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


def tool(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    needs_approval: bool | ApprovalPredicate = False,
) -> Any:
    """Decorate a function to turn it into a :class:`Tool`.

    The function may be sync or async; sync functions are run on a thread so
    they don't block the event loop. The first parameter named ``ctx`` or
    ``context`` (if present) receives the :class:`RunContext`.

    Examples::

        @tool
        async def add(a: int, b: int) -> int:
            '''Add two integers.'''
            return a + b

        @tool(name="search_web", needs_approval=True)
        async def search(query: str) -> list[str]:
            ...
    """

    def wrap(func: Callable[..., Any]) -> Tool:
        tool_name = name or func.__name__
        tool_desc = description or (inspect.getdoc(func) or "").strip()
        parameters, _ = function_args_schema(func)

        sig = inspect.signature(func)
        wants_context = any(
            p in sig.parameters for p in ("ctx", "context")
        )

        is_async = inspect.iscoroutinefunction(func)

        async def invoke(args: dict[str, Any], ctx: "RunContext") -> Any:
            cleaned = validate_args(func, args)
            kwargs = dict(cleaned)
            if wants_context:
                key = "ctx" if "ctx" in sig.parameters else "context"
                kwargs[key] = ctx
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
            _wants_context=wants_context,
        )

    if fn is None:
        return wrap
    return wrap(fn)
