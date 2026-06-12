"""Workspace permission policy.

A :class:`WorkspacePolicy` decides what the built-in workspace tools may do:

* **Path rules** (``denied_paths``, ``allow_write``) are enforced inside the
  session, so every file operation — including ones from custom tools that
  use the session directly — is gated.
* **Command rules** decide whether a shell command runs freely (``allow``),
  goes through the human-approval channel (``ask``), or is rejected outright
  (``deny``).

Honesty note: command rules are a *policy gate*, not a security boundary.
On the local backend an allowed command runs as the host user and can do
anything that user can. Hard isolation requires a sandboxed backend (e.g. a
container); the session protocol is abstracted so one can be added without
touching the tools.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Literal

from .errors import PermissionDeniedError

Decision = Literal["allow", "ask", "deny"]

# Most-restrictive-wins ordering for compound commands.
_SEVERITY = {"allow": 0, "ask": 1, "deny": 2}

# Splits a shell command on control operators so each segment is judged on
# its own: `git status && rm -rf /` must not ride on a `git status` allow
# rule. Quoting is intentionally ignored — a quoted `;` may cause a harmless
# extra split, which can only make the decision stricter, never looser.
_COMMAND_SPLIT = re.compile(r"(?:\|\||&&|[;|&])")


@dataclass(frozen=True)
class CommandRule:
    """One shell-command rule: a word-boundary prefix and a decision.

    ``pattern`` matches when the command starts with the same
    whitespace-separated words: ``"git push"`` matches ``git push origin``
    but not ``git pushx``.
    """

    pattern: str
    action: Decision


@dataclass(frozen=True)
class WorkspacePolicy:
    """What the workspace tools are allowed to do.

    Attributes:
        allow_write: When False every write/edit is rejected (read-only).
        allow_shell: When False the shell tool is excluded and every command
            decision is ``deny``.
        shell_default: Decision for commands no rule matches.
        command_rules: Evaluated first-match-wins per command segment;
            compound commands (``;``, ``&&``, ``||``, ``|``) take the most
            restrictive decision across their segments.
        denied_paths: ``fnmatch`` globs over workspace-relative paths that
            file tools may neither read nor write (e.g. ``".env*"``,
            ``"secrets/**"``).
    """

    allow_write: bool = True
    allow_shell: bool = True
    shell_default: Decision = "ask"
    command_rules: tuple[CommandRule, ...] = ()
    denied_paths: tuple[str, ...] = ()

    # ------------------------------------------------------------------ #
    # Presets
    # ------------------------------------------------------------------ #

    @classmethod
    def readonly(cls, *, denied_paths: tuple[str, ...] = ()) -> "WorkspacePolicy":
        """Read-only file access; no writes, no shell."""
        return cls(
            allow_write=False,
            allow_shell=False,
            shell_default="deny",
            denied_paths=denied_paths,
        )

    @classmethod
    def coding(
        cls,
        *,
        command_rules: tuple[CommandRule, ...] = (),
        denied_paths: tuple[str, ...] = (),
    ) -> "WorkspacePolicy":
        """Full file access; shell commands require approval by default."""
        return cls(
            shell_default="ask",
            command_rules=command_rules,
            denied_paths=denied_paths,
        )

    @classmethod
    def trusted(
        cls,
        *,
        command_rules: tuple[CommandRule, ...] = (),
        denied_paths: tuple[str, ...] = (),
    ) -> "WorkspacePolicy":
        """Full file access; shell commands run without approval by default."""
        return cls(
            shell_default="allow",
            command_rules=command_rules,
            denied_paths=denied_paths,
        )

    # ------------------------------------------------------------------ #
    # Decisions
    # ------------------------------------------------------------------ #

    def decide_command(self, command: str) -> Decision:
        """Return the policy decision for one shell command line.

        The command is split on shell control operators and each segment is
        judged independently; the most restrictive decision wins, so an
        ``allow`` rule can never whitelist a compound command that smuggles
        in something stricter.
        """
        if not self.allow_shell:
            return "deny"
        segments = [seg.strip() for seg in _COMMAND_SPLIT.split(command)]
        segments = [seg for seg in segments if seg]
        if not segments:
            return self.shell_default
        decision: Decision = "allow"
        for segment in segments:
            verdict = self._decide_segment(segment)
            if _SEVERITY[verdict] > _SEVERITY[decision]:
                decision = verdict
            if decision == "deny":
                break
        return decision

    def _decide_segment(self, segment: str) -> Decision:
        words = segment.split()
        for rule in self.command_rules:
            pattern_words = rule.pattern.split()
            if words[: len(pattern_words)] == pattern_words:
                return rule.action
        return self.shell_default

    def check_path(self, rel_path: str, *, write: bool) -> None:
        """Raise :class:`PermissionDeniedError` if ``rel_path`` is off-limits.

        ``rel_path`` is a normalized workspace-relative POSIX path.
        """
        if write and not self.allow_write:
            raise PermissionDeniedError(
                "Workspace is read-only.",
                hint="Use mode='coding' (or allow_write=True) to enable writes.",
            )
        for pattern in self.denied_paths:
            if fnmatch(rel_path, pattern):
                raise PermissionDeniedError(
                    f"Path {rel_path!r} is denied by workspace policy ({pattern!r}).",
                )

    def path_is_denied(self, rel_path: str) -> bool:
        """Non-raising variant of the denied-path check (used by listings)."""
        return any(fnmatch(rel_path, pattern) for pattern in self.denied_paths)


__all__ = ["CommandRule", "Decision", "WorkspacePolicy"]
