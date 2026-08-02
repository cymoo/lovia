"""Subagents plugin: background sub-agents that run while the parent keeps working.

The third piece of the multi-agent toolkit — **handoff** transfers the
conversation, **agent-as-tool** delegates and *waits*, ``Subagents`` delegates
*without* waiting: ``spawn_subagent`` returns immediately and the child runs
concurrently on the event loop while the parent continues its own turns.

Results re-enter the conversation through existing seams only — never as a
late tool result (which would break call/result pairing):

* completion pushes a rendered report into ``ctx.mailbox``, so it arrives as
  a normal user-side message at the next turn boundary;
* ``wait_subagents`` lets the model block for pending results — a report whose
  mailbox push has not been drained yet is *withdrawn* (:meth:`Mailbox.remove`)
  and returned as the tool result instead, so nothing is delivered twice;
* ``cancel_subagent`` stops one child cooperatively.

Two lifecycle modes, keyed on :attr:`Subagents.deliver`:

* **bounded** (``deliver=None``, the default) — children never outlive the
  run: whatever is still running when the run ends (success, cancel, failure)
  is cancelled in ``aclose``. Delivery is the run's own mailbox. This is the
  standalone-library mode; the instructions tell the model to wait or cancel
  before finishing.
* **detached** (``deliver=fn``) — children may outlive the run; every report
  goes through the callback instead of the mailbox (a serving layer routes it
  to the conversation, e.g. the web app's inject-or-start). Task references
  are held on the plugin *object* so detached work survives run teardown.

Children run headless: an approval request nobody resolves is denied by the
runner's default, so the run cannot hang — give children approval-free
toolsets for unattended work. Child spans join the parent's trace and child
token usage folds into the parent's :class:`~lovia.messages.Usage`, exactly
like ``agent_as_tool`` sub-runs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Annotated, Any, Awaitable, Callable, Sequence

from ..exceptions import RunCancelled, UserError
from ..reliability import CancelToken, RunBudget
from ..run_context import RunContext
from ..steering import Mailbox
from ..tools import Tool, tool
from ..transcript import InputEntry, TranscriptEntry
from ..types import JsonObject
from .base import PluginInstance, ViewInjector

if TYPE_CHECKING:
    from ..agent import Agent
    from ..runtime.result import RunResult

log = logging.getLogger(__name__)

_MAX_WAIT_SECONDS = 600
# How often blocked waits re-check the parent's cancel token (a plain flag,
# not awaitable), so a user cancel is honoured promptly mid-wait.
_POLL_SECONDS = 0.5


@dataclass
class SubagentReport:
    """One finished subagent, as delivered to a :data:`DeliverFn`."""

    id: str
    """Run-scoped task id (``t1``, ``t2``, …)."""

    agent: str
    """The child's ``Agent.name``."""

    prompt: str
    """The prompt the child was spawned with."""

    session_id: str | None
    """The parent run's session, captured at spawn — where the report belongs."""

    result: "RunResult | None"
    """The child's result, or ``None`` when it failed."""

    error: BaseException | None
    """The failure, or ``None`` on success."""

    text: str
    """The rendered report message (the same form the mailbox path pushes)."""


DeliverFn = Callable[[SubagentReport], Awaitable[None]]


@dataclass
class ChildSpec:
    """One spawn request, as handed to a :data:`RunChildFn` override."""

    id: str
    """Run-scoped task id (``t1``, ``t2``, …)."""

    agent: "Agent[Any]"
    """The child agent to run."""

    prompt: str
    """The self-contained prompt the child was spawned with."""

    token: CancelToken
    """The child's cancel signal — ``cancel_subagent`` trips it, and (in
    bounded mode) so does run teardown. An override must honor it: stop the
    child it started when the token cancels, and raise
    :class:`~lovia.exceptions.RunCancelled` for a cancelled outcome so no
    report is delivered."""

    parent_session_id: str | None
    """The spawning run's session, when it has one."""

    max_turns: int
    """Turn bound for the child run (the plugin's ``max_turns``)."""

    budget: RunBudget | None
    """A fresh per-spawn budget copy, when the plugin carries one."""

    mailbox: Mailbox
    """The child's inbound steering channel — ``send_to_subagent`` pushes
    into it and the child drains it at each turn start. An override must
    hand it to however it runs the child (``Runner.run(mailbox=...)`` or its
    serving-layer equivalent), or sends silently never arrive."""


RunChildFn = Callable[[ChildSpec], Awaitable["RunResult"]]


# --------------------------------------------------------------------------- #
# run-scoped state
# --------------------------------------------------------------------------- #


@dataclass
class _Record:
    id: str
    agent_name: str
    prompt: str
    token: CancelToken
    mailbox: Mailbox
    started: float  # time.monotonic() at spawn
    task: asyncio.Task[None] | None = None
    result: "RunResult | None" = None
    error: BaseException | None = None
    text: str = ""
    mailbox_token: int | None = None
    collected: bool = False  # returned (or announced) via wait_subagents

    @property
    def done(self) -> bool:
        return self.task is not None and self.task.done()

    @property
    def cancelled(self) -> bool:
        return isinstance(self.error, RunCancelled) or (
            self.task is not None and self.task.cancelled()
        )

    def elapsed(self) -> int:
        return int(time.monotonic() - self.started)


class _Registry:
    """Per-run spawn table, built fresh in ``setup``."""

    def __init__(self) -> None:
        self.records: dict[str, _Record] = {}
        self._seq = 0

    def next_id(self) -> str:
        self._seq += 1
        return f"t{self._seq}"

    def live(self) -> list[_Record]:
        return [r for r in self.records.values() if not r.done]


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def _stringify(output: Any) -> str:
    if output is None:
        return ""
    return output if isinstance(output, str) else repr(output)


def _truncate(s: str, limit: int | None) -> str:
    if limit is None or len(s) <= limit:
        return s
    head = max(limit * 2 // 3, 1)
    tail = max(limit - head, 1)
    dropped = len(s) - head - tail
    return f"{s[:head]}\n… [{dropped} chars truncated] …\n{s[-tail:]}"


def _render(rec: _Record, *, max_chars: int | None) -> str:
    tag = f"agent={rec.agent_name} elapsed={rec.elapsed()}s"
    if rec.error is None:
        body = _truncate(
            _stringify(rec.result.output if rec.result is not None else None),
            max_chars,
        )
        return f"[subagent {rec.id}: done] {tag}\n{body}"
    reason = f"{rec.error.__class__.__name__}: {rec.error}"
    return f"[subagent {rec.id}: failed] {tag}\n{reason}"


def _running_summary(records: Sequence[_Record]) -> str:
    parts = []
    for r in records:
        state = " (stopping)" if r.token.is_cancelled else ""
        parts.append(f"{r.id} {r.agent_name} ({r.elapsed()}s){state}")
    return ", ".join(parts)


# --------------------------------------------------------------------------- #
# instructions
# --------------------------------------------------------------------------- #

_INSTRUCTIONS_COMMON = (
    "## Background subagents\n"
    "You can delegate self-contained tasks to background subagents that run "
    "while you continue working: `spawn_subagent(prompt)` starts one and "
    "returns immediately with an id. Write every prompt standalone — the "
    "subagent sees nothing of this conversation and works in a fresh context. "
    "Spawn early, then keep doing your own work while they run. Reports "
    "arrive automatically as `[subagent …]` messages; call `wait_subagents` "
    "when you need pending results before continuing, "
    "`send_to_subagent(id, message)` to steer a running one (a forgotten "
    "constraint, a scope change), and `cancel_subagent(id)` to stop one. "
    "Tools that need approval are auto-denied inside a subagent, so delegate "
    "only work its tools can do unattended."
)

_INSTRUCTIONS_BOUNDED = (
    _INSTRUCTIONS_COMMON
    + "\nIMPORTANT: never finish your reply while subagents are still "
    "running — `wait_subagents` for them or cancel them first; work you "
    "have not collected is lost when you finish."
)

_INSTRUCTIONS_DETACHED = (
    _INSTRUCTIONS_COMMON
    + "\nYou may finish your reply while subagents are still running; their "
    "reports will arrive as later messages."
)


# --------------------------------------------------------------------------- #
# plugin
# --------------------------------------------------------------------------- #


@dataclass
class Subagents:
    """Plugin: spawn background subagents that run while the parent keeps working.

    ``Subagents([researcher, coder])`` offers a catalog the model picks from
    by ``Agent.name``; bare ``Subagents()`` spawns a clone of the *current*
    agent with ``plugins`` and ``handoffs`` stripped (no recursive spawning,
    no shared plugin state) — everything else (model, instructions, tools,
    workspace, hooks, guardrails) carries over.

    See the module docstring for the bounded/detached lifecycle split.
    """

    agents: "Agent[Any] | Sequence[Agent[Any]]" = ()
    """Spawnable children, keyed by ``Agent.name``. Empty = self-clone mode."""

    deliver: DeliverFn | None = None
    """``None`` (bounded): reports push into ``ctx.mailbox`` and leftover
    children are cancelled when the run ends. A callback (detached): every
    report goes through it and children may outlive the run."""

    run_child: RunChildFn | None = None
    """Replaces *how* a child executes. ``None`` runs it in-process
    (``Runner.run`` inheriting the parent's deps, tracer, and usage
    accumulator). An override receives a :class:`ChildSpec` and returns the
    child's :class:`~lovia.RunResult` — ``lovia.web`` uses this to route
    children through its run supervisor so they stream in the UI. Advanced:
    set it via :func:`lovia.web.wire_subagents` (which pairs it with
    ``deliver``) rather than by hand — an override with bounded-mode teardown
    would orphan, not cancel, its children."""

    max_concurrent: int = 4
    """Spawn cap; at capacity ``spawn_subagent`` declines (no queueing)."""

    max_turns: int = 50
    """Turn bound forwarded to every child run (as in ``agent_as_tool``)."""

    budget: RunBudget | None = None
    """Per-child budget; copied per spawn so counters never accumulate."""

    max_result_chars: int | None = 16_000
    """Report-body cap (head + tail kept). A child's final answer is injected
    into the parent's context verbatim — this is the tripwire for runaway
    payloads, in the spirit of ``Agent.max_tool_output_chars``. ``None``
    disables."""

    instructions: str | None = None
    """``None`` picks the built-in text for the active mode; ``""`` omits;
    anything else replaces."""

    name: str = "subagents"

    # Strong refs to detached children so they survive run teardown (the same
    # idiom as Memory's background curation tasks). Object-level on purpose:
    # the per-run registry dies with the run, detached work must not.
    _detached: set[asyncio.Task[None]] = field(
        default_factory=set, init=False, repr=False
    )
    _catalog: "dict[str, Agent[Any]]" = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        from ..agent import Agent

        agents = (self.agents,) if isinstance(self.agents, Agent) else self.agents
        catalog: dict[str, Agent[Any]] = {}
        for agent in agents:
            if not agent.name:
                raise UserError(
                    "Subagents requires every child agent to have a name",
                    hint="The model picks children by Agent.name.",
                )
            if agent.name in catalog:
                raise UserError(
                    f"Subagents got two child agents named {agent.name!r}",
                    hint="Agent.name is the spawn key; make them unique.",
                )
            catalog[agent.name] = agent
        self._catalog = catalog

    async def setup(self) -> PluginInstance:
        registry = _Registry()
        bounded = self.deliver is None
        instructions: str | None
        if self.instructions is None:
            instructions = _INSTRUCTIONS_BOUNDED if bounded else _INSTRUCTIONS_DETACHED
        else:
            instructions = self.instructions or None

        async def aclose() -> None:
            if not bounded:
                return
            live = [r for r in registry.records.values() if not r.done]
            for rec in live:
                rec.token.cancel("parent run ended")
                if rec.task is not None:
                    # Hard-cancel: a child mid-tool can take arbitrarily long
                    # to stop cooperatively, and run teardown must not wait on
                    # it. Children hold no durable state, so this is safe.
                    rec.task.cancel()
            tasks = [r.task for r in live if r.task is not None]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        return PluginInstance(
            tools=[
                self._spawn_tool(registry),
                self._wait_tool(registry),
                self._send_tool(registry),
                self._cancel_tool(registry),
            ],
            view_injectors=[self._injector(registry, bounded=bounded)],
            instructions=instructions,
            aclose=aclose,
        )

    # -- spawn ----------------------------------------------------------- #

    def _resolve_child(self, name: str | None, ctx: RunContext[Any]) -> "Agent[Any]":
        if self._catalog:
            if name is None and len(self._catalog) == 1:
                return next(iter(self._catalog.values()))
            child = self._catalog.get(name or "")
            if child is None:
                known = ", ".join(sorted(self._catalog))
                raise UserError(
                    f"unknown subagent {name!r} (available: {known})",
                )
            return child
        # Self-clone mode: the current agent minus plugins (no recursive
        # spawning, no shared plugin state) and handoffs (a child must answer,
        # not transfer a conversation nobody is having with it).
        return ctx.agent.clone(name=f"{ctx.agent.name}-sub", plugins=[], handoffs=[])

    async def _drive(
        self, rec: _Record, child: "Agent[Any]", ctx: RunContext[Any]
    ) -> None:
        try:
            if self.run_child is not None:
                rec.result = await self.run_child(
                    ChildSpec(
                        id=rec.id,
                        agent=child,
                        prompt=rec.prompt,
                        token=rec.token,
                        parent_session_id=ctx.session_id,
                        max_turns=self.max_turns,
                        budget=replace(self.budget)
                        if self.budget is not None
                        else None,
                        mailbox=rec.mailbox,
                    )
                )
            else:
                rec.result = await self._run_inline(rec, child, ctx)
        except Exception as exc:
            rec.error = exc
        await self._finish(rec, ctx)

    async def _run_inline(
        self, rec: _Record, child: "Agent[Any]", ctx: RunContext[Any]
    ) -> "RunResult":
        # Local import: plugins must stay importable without the runner
        # (mirrors agent_as_tool's circular-import guard).
        from ..runner import Runner

        return await Runner.run(
            child,
            rec.prompt,
            context=ctx.context,
            max_turns=self.max_turns,
            budget=replace(self.budget) if self.budget is not None else None,
            cancel_token=rec.token,
            mailbox=rec.mailbox,
            # Join the parent's trace and fold usage into the parent's
            # accumulator (failed legs included) — as agent_as_tool does.
            tracer=ctx._tracer,
            _parent_usage=ctx.usage,
        )

    async def _finish(self, rec: _Record, ctx: RunContext[Any]) -> None:
        if rec.cancelled:
            return  # cancel_subagent already told the model; a report is noise
        rec.text = _render(rec, max_chars=self.max_result_chars)
        if self.deliver is not None:
            report = SubagentReport(
                id=rec.id,
                agent=rec.agent_name,
                prompt=rec.prompt,
                session_id=ctx.session_id,
                result=rec.result,
                error=rec.error,
                text=rec.text,
            )
            try:
                await self.deliver(report)
            except Exception:
                log.exception("subagent %s: deliver callback failed", rec.id)
        else:
            rec.mailbox_token = ctx.mailbox.push(rec.text)

    def _spawn_tool(self, registry: _Registry) -> Tool:
        properties: JsonObject = {
            "prompt": {
                "type": "string",
                "description": (
                    "The full, self-contained task for the subagent. It sees "
                    "nothing else — include every fact and constraint it needs, "
                    "and say what its final report should contain."
                ),
            }
        }
        required = ["prompt"]
        if self._catalog:
            properties["agent"] = {
                "type": "string",
                "enum": sorted(self._catalog),
                "description": "Which subagent to spawn.",
            }
            if len(self._catalog) > 1:
                required = ["agent", "prompt"]

        async def invoke(args: dict[str, Any], ctx: RunContext[Any]) -> str:
            prompt = str(args.get("prompt") or "").strip()
            if not prompt:
                return "Nothing spawned: the prompt was empty."
            live = registry.live()
            if len(live) >= self.max_concurrent:
                return (
                    f"At capacity ({len(live)} subagents running: "
                    f"{_running_summary(live)}). wait_subagents for one to "
                    "finish, or cancel one, then spawn again."
                )
            try:
                child = self._resolve_child(args.get("agent"), ctx)
            except UserError as exc:
                return str(exc)
            rec = _Record(
                id=registry.next_id(),
                agent_name=child.name,
                prompt=prompt,
                token=CancelToken(),
                mailbox=Mailbox(),
                started=time.monotonic(),
            )
            registry.records[rec.id] = rec
            rec.task = asyncio.create_task(self._drive(rec, child, ctx))
            if self.deliver is not None:
                self._detached.add(rec.task)
                rec.task.add_done_callback(self._detached.discard)
            return (
                f"Subagent {rec.id} ({rec.agent_name}) started in the "
                "background; its report will arrive as a message when it "
                "finishes. Keep working in the meantime."
            )

        return Tool(
            name="spawn_subagent",
            description=(
                "Start a background subagent on a self-contained task and "
                "return immediately with its id. The subagent runs while you "
                "continue working; its report arrives as a later message (or "
                "via wait_subagents)."
            ),
            parameters={
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
            invoke=invoke,
        )

    # -- wait / cancel ---------------------------------------------------- #

    def _wait_tool(self, registry: _Registry) -> Tool:
        @tool(
            name="wait_subagents",
            description=(
                "Wait for background subagents and collect their reports. "
                "Returns as soon as one of the targeted subagents has a "
                "result you have not seen, or when the timeout passes."
            ),
        )
        async def wait_subagents(
            ctx: RunContext[Any],
            ids: Annotated[
                list[str] | None,
                "Subagent ids to wait for; omit to wait on all of them.",
            ] = None,
            timeout_seconds: Annotated[
                int,
                "How long to wait before reporting back anyway (max 600).",
            ] = 60,
        ) -> str:
            if not registry.records:
                return "No subagents have been spawned."
            if ids:
                unknown = [i for i in ids if i not in registry.records]
                if unknown:
                    known = ", ".join(registry.records)
                    return (
                        f"Unknown subagent id(s) {', '.join(unknown)} (known: {known})."
                    )
                targets = [registry.records[i] for i in ids]
            else:
                targets = list(registry.records.values())

            deadline = time.monotonic() + min(
                max(timeout_seconds, 0), _MAX_WAIT_SECONDS
            )
            while True:
                fresh = [r for r in targets if r.done and not r.collected]
                if fresh:
                    return self._collect(fresh, targets, ctx)
                if all(r.done for r in targets):
                    return "All targeted subagents already finished and were reported."
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    running = [r for r in targets if not r.done]
                    return (
                        f"Timed out; still running: {_running_summary(running)}. "
                        "Keep working and check again, or cancel_subagent."
                    )
                if ctx.cancel_token.is_cancelled:
                    return "The run is being cancelled; stopped waiting."
                pending = [
                    r.task for r in targets if r.task is not None and not r.task.done()
                ]
                await asyncio.wait(
                    pending,
                    timeout=min(_POLL_SECONDS, remaining),
                    return_when=asyncio.FIRST_COMPLETED,
                )

        return wait_subagents

    def _collect(
        self, fresh: list[_Record], targets: list[_Record], ctx: RunContext[Any]
    ) -> str:
        parts: list[str] = []
        for rec in fresh:
            rec.collected = True
            if rec.cancelled:
                parts.append(f"{rec.id} was cancelled; no report.")
            elif rec.mailbox_token is not None and ctx.mailbox.remove(
                rec.mailbox_token
            ):
                # Withdrew the still-queued push: return the report directly
                # instead — it must reach the context exactly once.
                parts.append(rec.text)
            else:
                parts.append(
                    f"{rec.id} finished; its report was already delivered as a message."
                )
        running = [r for r in targets if not r.done]
        if running:
            parts.append(f"Still running: {_running_summary(running)}.")
        return "\n\n".join(parts)

    def _send_tool(self, registry: _Registry) -> Tool:
        @tool(
            name="send_to_subagent",
            description=(
                "Send a message to a running background subagent. It is "
                "injected as a user message at the subagent's next turn "
                "start — use it to add a forgotten constraint, narrow scope, "
                "or redirect work without restarting. Write it standalone: "
                "the subagent still sees nothing of this conversation."
            ),
        )
        async def send_to_subagent(
            ctx: RunContext[Any],
            id: Annotated[str, "The subagent id, as returned by spawn_subagent."],
            message: Annotated[str, "The message to deliver."],
        ) -> str:
            _ = ctx
            body = message.strip()
            if not body:
                return "Nothing sent: the message was empty."
            rec = registry.records.get(id.strip())
            if rec is None:
                known = ", ".join(registry.records) or "none"
                return f"No subagent {id!r} (known: {known})."
            if rec.done:
                return (
                    f"Subagent {rec.id} already finished; nothing to steer. "
                    "Spawn a new one instead."
                )
            if rec.token.is_cancelled:
                return f"Subagent {rec.id} is stopping; the message would be lost."
            rec.mailbox.push(body)
            return (
                f"Delivered to {rec.id}; it sees the message at its next "
                "turn start."
            )

        return send_to_subagent

    def _cancel_tool(self, registry: _Registry) -> Tool:
        @tool(
            name="cancel_subagent",
            description=(
                "Stop a background subagent. It halts at its next safe point "
                "and delivers no report."
            ),
        )
        async def cancel_subagent(
            ctx: RunContext[Any],
            id: Annotated[str, "The subagent id, as returned by spawn_subagent."],
        ) -> str:
            rec = registry.records.get(id.strip())
            if rec is None:
                known = ", ".join(registry.records) or "none"
                return f"No subagent {id!r} (known: {known})."
            if rec.done:
                return f"Subagent {rec.id} already finished."
            rec.token.cancel("cancelled by the parent agent")
            return (
                f"Subagent {rec.id} asked to stop; it halts at its next safe "
                "point and delivers no report."
            )

        return cancel_subagent

    # -- per-turn status -------------------------------------------------- #

    def _injector(self, registry: _Registry, *, bounded: bool) -> ViewInjector:
        def inject(ctx: RunContext[Any]) -> list[TranscriptEntry] | None:
            running = registry.live()
            if not running:
                return None
            lines = [f"Background subagents running: {_running_summary(running)}."]
            if bounded:
                lines.append(
                    "Do not finish your reply while they run — wait_subagents "
                    "or cancel_subagent first."
                )
            text = "<system-reminder>\n" + "\n".join(lines) + "\n</system-reminder>"
            return [InputEntry(role="user", content=text)]

        return inject


__all__ = ["ChildSpec", "DeliverFn", "RunChildFn", "SubagentReport", "Subagents"]
