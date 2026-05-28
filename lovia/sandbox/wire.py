"""One-call wiring of a sandbox into an existing :class:`~lovia.Agent`.

::

    from lovia import Agent, Runner
    from lovia.sandbox import LocalSandboxProvider, attach_sandbox
    from lovia.stores import InMemorySession

    base = Agent(name="coder", instructions="...", model="openai:gpt-4o-mini")
    async with LocalSandboxProvider() as provider:
        agent = attach_sandbox(base, provider)
        await Runner.run(agent, "build it", session=InMemorySession(), session_id="s1")

`attach_sandbox` is purely additive: it returns a clone with the sandbox
tools merged in. Lifecycle is handled by the provider's ``async with``
context (and by per-session lazy acquire inside ``sandbox_tools``); no
hidden event hooks, no surprise allocation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

from ..tools import Tool, ToolPolicy
from .audit import AuditPolicy, AuditStream, default_audit_policy
from .protocol import Sandbox, SandboxProvider
from .tools import sandbox_tools
from .types import ExecLimits

if TYPE_CHECKING:
    from ..agent import Agent

__all__ = ["attach_sandbox"]


def attach_sandbox(
    agent: "Agent",
    sb_or_provider: Sandbox | SandboxProvider,
    *,
    audit: AuditPolicy | None | str = "default",
    audit_stream: AuditStream | None = None,
    include: Iterable[str] | None = None,
    exclude: Iterable[str] | None = None,
    exec_limits: ExecLimits | None = None,
    extra_run_policies: Iterable[ToolPolicy] = (),
) -> "Agent":
    """Return a clone of ``agent`` with sandbox tools attached.

    Args:
        agent: The base agent to extend.
        sb_or_provider: A single :class:`Sandbox` or a
            :class:`SandboxProvider`. Providers enable per-session reuse;
            tools resolve the sandbox lazily from ``ctx.session_id``.
        audit: ``"default"`` (built-in policy), ``None`` (disabled), or a
            custom :class:`AuditPolicy`. Default-on for local sandboxes;
            pass ``None`` for fully isolated backends like Docker.
        audit_stream: Optional UI stream for live verdict updates.
        include / exclude: Tool name filters passed to
            :func:`sandbox_tools`.
        exec_limits: Per-call ``run`` limits.
        extra_run_policies: Extra :class:`ToolPolicy` chained around
            ``run`` (after the audit policy).
    """
    audit_policy: AuditPolicy | None
    if audit == "default":
        audit_policy = default_audit_policy()
    else:
        audit_policy = audit  # type: ignore[assignment]

    new_tools: list[Tool] = list(agent.tools) + sandbox_tools(
        sb_or_provider,
        audit=audit_policy,
        audit_stream=audit_stream,
        include=include,
        exclude=exclude,
        exec_limits=exec_limits,
        extra_run_policies=extra_run_policies,
    )
    return agent.clone(tools=new_tools)
