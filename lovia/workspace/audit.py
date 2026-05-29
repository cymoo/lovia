"""Small command auditing layer for workspace ``bash`` tools."""

from __future__ import annotations

import asyncio
import re
import weakref
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, runtime_checkable

from ..run_context import RunContext
from ..tools import ToolInvoker
from .errors import AuditBlocked
from .types import AuditVerdict

__all__ = [
    "AuditContext",
    "AuditDecision",
    "AuditPolicy",
    "AuditRecord",
    "AuditStream",
    "AuditToolPolicy",
    "compose_policies",
    "default_audit_policy",
    "pass_through_policy",
    "rule_policy",
]


@dataclass(frozen=True)
class AuditContext:
    """Information an audit policy may consult."""

    session_id: str | None
    agent_name: str
    tool_name: str
    user_context: Any = None


@dataclass(frozen=True)
class AuditDecision:
    """Bundles a verdict with an optional reason."""

    verdict: AuditVerdict
    reason: str = ""


@runtime_checkable
class AuditPolicy(Protocol):
    """Decides whether a command may run."""

    def __call__(
        self, command: str, ctx: AuditContext
    ) -> AuditDecision | AuditVerdict: ...


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
_NPM_GLOBAL = re.compile(r"\bnpm\s+(?:install|i|add)\b[^\n]*\s-g\b")


def default_audit_policy() -> AuditPolicy:
    """Return the built-in policy for obvious local-workspace foot-guns."""

    def policy(command: str, ctx: AuditContext) -> AuditDecision:
        flat = " ".join(command.split())
        for pattern, reason in _BLOCK_RULES:
            if pattern.search(flat):
                return AuditDecision("block", reason)
        if _NPM_GLOBAL.search(flat):
            return AuditDecision(
                "warn",
                "global npm install pollutes the host HOME; prefer a local install or npx.",
            )
        return AuditDecision("pass")

    return policy


def pass_through_policy() -> AuditPolicy:
    """Return a policy that always passes."""

    def policy(command: str, ctx: AuditContext) -> AuditDecision:
        return AuditDecision("pass")

    return policy


@dataclass(frozen=True)
class AuditRecord:
    """One audit-stream entry."""

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
    """Fan-out async pub/sub for audit records."""

    def __init__(self, maxsize: int = 256) -> None:
        self._maxsize = maxsize
        self._subscribers: weakref.WeakSet[asyncio.Queue[AuditRecord]] = (
            weakref.WeakSet()
        )
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


def _coerce_decision(value: AuditDecision | AuditVerdict) -> AuditDecision:
    if isinstance(value, AuditDecision):
        return value
    return AuditDecision(value)


@dataclass
class AuditToolPolicy:
    """ToolPolicy that audits a workspace ``bash`` command."""

    policy: AuditPolicy
    stream: AuditStream | None = None
    cmd_arg: str = "command"

    async def __call__(
        self,
        invoke: ToolInvoker,
        args: dict[str, Any],
        ctx: RunContext,
    ) -> Any:
        import time

        command = str(args.get(self.cmd_arg, ""))
        audit_ctx = AuditContext(
            session_id=ctx.session_id,
            agent_name=getattr(ctx.agent, "name", "agent"),
            tool_name=getattr(ctx, "_tool_name", "bash"),
            user_context=ctx.context,
        )
        decision = _coerce_decision(self.policy(command, audit_ctx))
        record = AuditRecord(
            timestamp=time.time(),
            session_id=audit_ctx.session_id,
            agent_name=audit_ctx.agent_name,
            tool_name=audit_ctx.tool_name,
            command=command,
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
            warn_text = f"\n\naudit warning: {decision.reason or 'medium-risk command'}"
            if isinstance(result, dict):
                result = {
                    **result,
                    "stderr": str(result.get("stderr", "")) + warn_text,
                    "audit_warning": decision.reason,
                }
            elif isinstance(result, str):
                result += warn_text
        return result


def compose_policies(*policies: AuditPolicy) -> AuditPolicy:
    """Run policies in order; the first non-pass verdict wins."""

    def policy(command: str, ctx: AuditContext) -> AuditDecision:
        for item in policies:
            decision = _coerce_decision(item(command, ctx))
            if decision.verdict != "pass":
                return decision
        return AuditDecision("pass")

    return policy


RulePredicate = Callable[[str, AuditContext], AuditDecision | AuditVerdict | None]


def rule_policy(rules: Iterable[RulePredicate]) -> AuditPolicy:
    """Build a policy from a list of rule callables."""

    rules_list = list(rules)

    def policy(command: str, ctx: AuditContext) -> AuditDecision:
        for rule in rules_list:
            outcome = rule(command, ctx)
            if outcome is None:
                continue
            decision = _coerce_decision(outcome)
            if decision.verdict != "pass":
                return decision
        return AuditDecision("pass")

    return policy
