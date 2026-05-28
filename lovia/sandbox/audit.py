"""Command auditing for sandbox ``run`` tools.

This module ships:

* :class:`AuditPolicy` — Protocol returning :data:`AuditVerdict` per command.
* :func:`default_audit_policy` — a *small* built-in policy (≤10 rules)
  catching the obvious foot-guns (rm -rf /, mkfs, dd, fork bomb, …). It is
  intentionally narrow — anything more belongs in user code.
* :func:`pass_through_policy` — a no-op policy; sensible Docker default.
* :class:`AuditToolPolicy` — a :class:`~lovia.tools.ToolPolicy` that wraps
  the sandbox ``run`` tool and consults the audit policy. Publishes each
  verdict on an asyncio queue so a UI can stream them live.

Design notes:

* Verdicts are deliberately three-valued (``pass``/``warn``/``block``) —
  ``warn`` allows the call but annotates the result, letting the LLM
  notice and (ideally) revise.
* The default rules are tiny because LLMs are increasingly good at
  refusing dangerous instructions on their own; we cover only the cases
  where a hallucinated command can wreck the host immediately.
* For untrusted code, the right answer is a real container — not a
  bigger regex list.
"""

from __future__ import annotations

import asyncio
import re
import weakref
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Protocol, runtime_checkable

from ..run_context import RunContext
from ..tools import ToolInvoker
from .errors import AuditBlocked
from .types import AuditVerdict

__all__ = [
    "AuditContext",
    "AuditPolicy",
    "AuditRecord",
    "AuditToolPolicy",
    "AuditStream",
    "default_audit_policy",
    "pass_through_policy",
]


# ---------------------------------------------------------------------------
# Policy surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditContext:
    """Information an :class:`AuditPolicy` may consult."""

    session_id: str | None
    agent_name: str
    tool_name: str
    user_context: Any = None


@dataclass(frozen=True)
class AuditDecision:
    """Bundles the verdict with an optional reason."""

    verdict: AuditVerdict
    reason: str = ""


@runtime_checkable
class AuditPolicy(Protocol):
    """Decides whether a sandbox command may run.

    Implementations are sync (cheap to call per tool invocation). Return
    an :class:`AuditDecision` or a bare :data:`AuditVerdict` string.
    """

    def __call__(
        self, cmd: str, ctx: AuditContext
    ) -> "AuditDecision | AuditVerdict": ...


# ---------------------------------------------------------------------------
# Built-in policies
# ---------------------------------------------------------------------------


_BLOCK_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*\s+(/(?:\s|$|\*)|~/?|/home\b|/root\b)"),
        "recursive delete of root/home",
    ),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "filesystem format"),
    (re.compile(r"\bdd\s+if="), "raw disk write via dd"),
    (re.compile(r":\(\)\s*\{[^}]*:\|:\&\s*\};:"), "fork bomb"),
    (
        re.compile(r"\b(curl|wget)\b[^|]*\|\s*(ba)?sh\b"),
        "pipe network payload into shell",
    ),
    (re.compile(r">+\s*/etc/"), "overwrite under /etc"),
    (re.compile(r">+\s*(/usr/bin/|/bin/|/sbin/)"), "overwrite system binary"),
    (re.compile(r"\b(LD_PRELOAD|LD_LIBRARY_PATH)\s*="), "dynamic linker hijack"),
    (re.compile(r"/dev/tcp/"), "bash internal network socket"),
    (re.compile(r"\bbase64\s+[^|]*-d[^|]*\|\s*(ba)?sh\b"), "base64-decoded shell exec"),
]


# Hygiene warnings. The point isn't safety — these commands run fine —
# but a warn verdict surfaces a short hint via stderr so the LLM notices
# and self-corrects on the next turn.
_PIP_INSTALL = re.compile(r"\b(?:pip|pip3)\s+install\b")
_VENV_HINT = re.compile(r"(\.venv|virtualenv\b|pipx\b|--user\b|conda\b|uv\s+pip\b)")
_NPM_GLOBAL = re.compile(r"\bnpm\s+(?:install|i|add)\b[^\n]*\s-g\b")


def _hygiene_decision(flat: str) -> AuditDecision | None:
    if _PIP_INSTALL.search(flat) and not _VENV_HINT.search(flat):
        return AuditDecision(
            "warn",
            "pip install without a venv — run "
            "`python -m venv .venv && .venv/bin/pip install …` first so "
            "deps stay scoped to this sandbox.",
        )
    if _NPM_GLOBAL.search(flat):
        return AuditDecision(
            "warn",
            "global npm install pollutes $HOME — install locally "
            "(`npm install <pkg>`) or use npx.",
        )
    return None


def default_audit_policy() -> AuditPolicy:
    """Return the built-in audit policy.

    Two kinds of rules:

    * 10 hard *block* rules for obvious foot-guns (rm -rf /, mkfs, …).
    * 2 *warn* rules nudging the LLM toward virtualenvs and against
      polluting ``HOME`` with global package installs.

    Anything more belongs in user code. The audit layer is not a security
    boundary — for untrusted code use a real container.
    """

    def policy(cmd: str, ctx: AuditContext) -> AuditDecision:
        flat = " ".join(cmd.split())
        for pattern, reason in _BLOCK_RULES:
            if pattern.search(flat):
                return AuditDecision("block", reason)
        warn = _hygiene_decision(flat)
        if warn is not None:
            return warn
        return AuditDecision("pass")

    return policy


def pass_through_policy() -> AuditPolicy:
    """Return a policy that always passes. Sensible Docker default."""

    def policy(cmd: str, ctx: AuditContext) -> AuditDecision:
        return AuditDecision("pass")

    return policy


# ---------------------------------------------------------------------------
# Audit stream (UI / observability)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditRecord:
    """One entry in the audit stream."""

    timestamp: float
    session_id: str | None
    agent_name: str
    tool_name: str
    command: str
    verdict: AuditVerdict
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": datetime.fromtimestamp(
                self.timestamp, tz=timezone.utc
            ).isoformat(),
            "session_id": self.session_id,
            "agent_name": self.agent_name,
            "tool_name": self.tool_name,
            "command": self.command,
            "verdict": self.verdict,
            "reason": self.reason,
        }


class AuditStream:
    """Fan-out async pub/sub for audit records.

    The web UI subscribes via :meth:`subscribe` (one queue per HTTP client);
    the :class:`AuditToolPolicy` writes via :meth:`publish`. Subscribers
    drop on overflow rather than blocking the policy.
    """

    def __init__(self, maxsize: int = 256) -> None:
        self._maxsize = maxsize
        # Weak refs so abandoned subscribers don't pin memory.
        self._subscribers: weakref.WeakSet[asyncio.Queue[AuditRecord]] = (
            weakref.WeakSet()
        )
        self._lock = asyncio.Lock()
        self._history: list[AuditRecord] = []
        self._history_cap = 200

    def history(self) -> list[AuditRecord]:
        return list(self._history)

    def subscribe(self) -> asyncio.Queue[AuditRecord]:
        q: asyncio.Queue[AuditRecord] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(q)
        return q

    def publish(self, record: AuditRecord) -> None:
        self._history.append(record)
        if len(self._history) > self._history_cap:
            del self._history[: len(self._history) - self._history_cap]
        for q in list(self._subscribers):
            try:
                q.put_nowait(record)
            except asyncio.QueueFull:  # noqa: PERF203
                pass


# ---------------------------------------------------------------------------
# ToolPolicy wrapping the run tool
# ---------------------------------------------------------------------------


def _coerce_decision(value: "AuditDecision | AuditVerdict") -> AuditDecision:
    if isinstance(value, AuditDecision):
        return value
    return AuditDecision(value)


@dataclass
class AuditToolPolicy:
    """A :class:`~lovia.tools.ToolPolicy` that audits sandbox ``run`` calls.

    Plug it into ``Tool.policies`` for any tool that executes commands.
    For each call:

    * **pass**  — the underlying tool runs unchanged.
    * **warn**  — the tool runs and the warning is appended to the result.
    * **block** — the tool is skipped and an :class:`AuditBlocked` is raised
      (the runner turns it into a model-visible error message).

    Every decision is published to ``stream`` for UI consumption.
    """

    policy: AuditPolicy
    stream: AuditStream | None = None
    # Name of the arg that carries the command string. Defaults to "cmd"
    # to match :func:`sandbox_tools`.
    cmd_arg: str = "cmd"

    async def __call__(
        self,
        invoke: ToolInvoker,
        args: dict[str, Any],
        ctx: RunContext,
    ) -> Any:
        import time

        cmd = str(args.get(self.cmd_arg, ""))
        audit_ctx = AuditContext(
            session_id=ctx.session_id,
            agent_name=ctx.agent.name,
            tool_name=getattr(ctx, "_tool_name", "run"),
            user_context=ctx.context,
        )
        decision = _coerce_decision(self.policy(cmd, audit_ctx))
        record = AuditRecord(
            timestamp=time.time(),
            session_id=audit_ctx.session_id,
            agent_name=audit_ctx.agent_name,
            tool_name=audit_ctx.tool_name,
            command=cmd,
            verdict=decision.verdict,
            reason=decision.reason,
        )
        if self.stream is not None:
            self.stream.publish(record)

        if decision.verdict == "block":
            raise AuditBlocked(
                f"Command blocked by audit policy: {decision.reason or 'no reason given'}.",
                hint="Try a safer alternative or adjust the AuditPolicy.",
            )

        result = await invoke(args, ctx)

        if decision.verdict == "warn":
            warn_text = (
                f"\n\n⚠️ audit warning: {decision.reason or 'medium-risk command'}"
            )
            if isinstance(result, dict):
                stderr = str(result.get("stderr", ""))
                result = {
                    **result,
                    "stderr": stderr + warn_text,
                    "audit_warning": decision.reason,
                }
            elif isinstance(result, str):
                result = result + warn_text
        return result


# Helper used by tests / wire helpers to build a policy from a list of rules.
def compose_policies(*policies: AuditPolicy) -> AuditPolicy:
    """Run policies in order; the first non-``pass`` verdict wins."""

    def policy(cmd: str, ctx: AuditContext) -> AuditDecision:
        for p in policies:
            decision = _coerce_decision(p(cmd, ctx))
            if decision.verdict != "pass":
                return decision
        return AuditDecision("pass")

    return policy


# Re-export so callers can wrap their own callables conveniently.
RulePredicate = Callable[[str, AuditContext], AuditDecision | AuditVerdict | None]


def rule_policy(rules: Iterable[RulePredicate]) -> AuditPolicy:
    """Build a policy from a list of (cmd, ctx) -> verdict callables."""
    rules_list = list(rules)

    def policy(cmd: str, ctx: AuditContext) -> AuditDecision:
        for rule in rules_list:
            outcome = rule(cmd, ctx)
            if outcome is None:
                continue
            decision = _coerce_decision(outcome)
            if decision.verdict != "pass":
                return decision
        return AuditDecision("pass")

    return policy


# Silence pyflakes/mypy for re-exported aliases used in docstrings.
_ = (Awaitable, field)
