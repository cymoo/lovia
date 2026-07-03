"""Workspace permission policy.

A :class:`WorkspacePolicy` decides what the built-in workspace tools may do.
Both file paths and shell commands are judged with the same three-valued
vocabulary — ``allow`` runs freely, ``ask`` routes through the human-approval
channel, ``deny`` is rejected outright:

* **Path decisions** (:meth:`WorkspacePolicy.decide_path`) are an ordered ACL:
  ``denied_paths`` first, then ``path_rules`` (first match wins), then the
  defaults — inside the workspace root reads are always allowed and writes
  follow ``write``; outside the root ``read_outside`` / ``write_outside``
  apply. Paths are judged *after* symlink resolution, so a symlink is exactly
  as accessible as its target — there is no separate symlink special case.
* **Command decisions** (:meth:`WorkspacePolicy.decide_command`) combine the
  static word-boundary prefix ``command_rules`` with the optional
  ``command_decider`` hook. The session additionally extracts path-looking
  tokens from commands and judges them with the same path ACL (see
  :mod:`lovia.workspace.command_guard`).

Honesty note: on the local backend these decisions are a *policy gate*, not a
security boundary. Command analysis is lexical, so command substitution,
``eval``, or an interpreter one-liner can still smuggle work past the rules;
and an allowed command runs as the host user. Hard isolation requires a
sandboxed executor or backend (e.g. a container); the protocols are
abstracted so one can be added without touching the tools.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable, Literal

Decision = Literal["allow", "ask", "deny"]
FileOp = Literal["read", "write"]

# A per-segment command decider: given one command segment it returns a
# Decision to override the static rules, or None to fall through to them. A
# lightweight, complete extension point — the static command_rules are
# prefix-only by design, so route anything richer (regex, allow-lists,
# context-aware checks) through this hook.
CommandDecider = Callable[[str], "Decision | None"]

# Most-restrictive-wins ordering for compound commands.
_SEVERITY = {"allow": 0, "ask": 1, "deny": 2}

# Splits a shell command on control operators so each segment is judged on
# its own: `git status && rm -rf /` must not ride on a `git status` allow
# rule. Newlines count as separators too (a shell runs each line), closing a
# bypass where `allowed\nrm -rf /` would ride on the first line's rule.
# A single `&` splits only as a backgrounding operator: `&` inside the
# fd-duplication forms `2>&1` / `>&2` / `<&0` (preceded by `<`/`>`) or the
# redirect shorthands `&>` / `&>>` (followed by `>`) is part of a redirection,
# not a separator — without the lookarounds `git status 2>&1` would be split
# into `git status 2>` + `1` and the stray `1` would drop to shell_default.
# Quoting is intentionally ignored — a quoted separator may cause a harmless
# extra split, which can only make the decision stricter, never looser.
_COMMAND_SPLIT = re.compile(
    r"""
      \|\|            # logical or
    | &&              # logical and
    | [;\n\r]         # statement separators
    | \|(?!\|)        # pipe
    | (?<![<>])&(?![&>])  # background job, but not >& / <& / &> / &&
    """,
    re.VERBOSE,
)


def _path_matches(rel_path: str, pattern: str) -> bool:
    """Best-effort gitignore-style match of a workspace-relative POSIX path.

    A pattern containing ``/`` is matched against the whole path (and its
    children, so a directory pattern denies everything beneath it);
    ``fnmatch``'s ``*`` already spans ``/``. A bare pattern (no ``/``) matches
    the basename of the path *or any ancestor segment*, so ``".env*"`` catches
    ``"sub/.env"`` and a denied directory name denies everything under it.
    """
    pat = pattern.rstrip("/")
    if not pat:
        return False
    if "/" in pat:
        return fnmatch(rel_path, pat) or fnmatch(rel_path, pat + "/*")
    return any(fnmatch(segment, pat) for segment in rel_path.split("/"))


def _abs_matches(abs_posix: str, pattern: str) -> bool:
    """Match a resolved absolute POSIX path against a ``/``- or ``~``-pattern.

    Like the relative match, a pattern also covers everything beneath it, so
    ``"~/notes"`` grants/denies the whole subtree.
    """
    pat = pattern
    if pat.startswith("~"):
        try:
            pat = Path(pat).expanduser().as_posix()
        except RuntimeError:
            # No resolvable home directory: the pattern cannot match any
            # resolved absolute path, so treat it as a non-match rather than
            # crash every decision.
            return False
    pat = pat.rstrip("/")
    if not pat:
        return False
    return fnmatch(abs_posix, pat) or fnmatch(abs_posix, pat + "/*")


def _pattern_matches(pattern: str, *, rel: str | None, abs_posix: str) -> bool:
    """Dispatch one ACL pattern by its form.

    ``/``- or ``~``-prefixed patterns match the resolved absolute path.
    Patterns containing ``/`` are workspace-relative (they never match
    outside the root). A *bare* pattern names a file or directory wherever
    it appears — inside or outside the root — so ``denied_paths=(".env*",)``
    still bites when ``read_outside`` is permissive.
    """
    if pattern.startswith(("/", "~")):
        return _abs_matches(abs_posix, pattern)
    if rel is not None:
        return _path_matches(rel, pattern)
    if "/" not in pattern.rstrip("/"):
        return _path_matches(abs_posix.lstrip("/"), pattern)
    return False


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
class PathRule:
    """One path-ACL rule: a pattern, a decision, and the ops it covers.

    ``pattern`` starting with ``/`` or ``~`` is matched against the resolved
    absolute path (and its subtree). A pattern containing ``/`` is
    workspace-relative and never matches outside the root. A *bare* pattern
    (``".env*"``, ``"secrets"``) names a file or directory anywhere — inside
    or outside the root (see :func:`_pattern_matches`). ``ops`` restricts the
    rule to reads and/or writes — accepts any iterable of ``"read"`` /
    ``"write"``.
    """

    pattern: str
    action: Decision
    ops: frozenset[FileOp] = frozenset({"read", "write"})

    def __post_init__(self) -> None:
        # Accept any iterable for ergonomics (("read",), {"write"}, ...).
        if not isinstance(self.ops, frozenset):
            object.__setattr__(self, "ops", frozenset(self.ops))


@dataclass(frozen=True)
class WorkspacePolicy:
    """What the workspace tools are allowed to do.

    Attributes:
        write: Decision for writes *inside* the workspace root. Reads inside
            the root are always allowed — a workspace the agent cannot read
            is pointless.
        read_outside: Decision for reads outside the root (reached via an
            absolute path, ``~``, ``..`` or a symlink target).
        write_outside: Decision for writes outside the root.
        path_rules: Ordered ACL consulted before the defaults above;
            first match wins. See :class:`PathRule`.
        denied_paths: Sugar for the common case — patterns that neither
            reads nor writes may touch, checked before ``path_rules``.
            Same pattern language as :class:`PathRule`.
        allow_shell: When False the shell tool is excluded and every command
            decision is ``deny``.
        shell_default: Decision for commands no rule matches.
        command_rules: Evaluated first-match-wins per command segment;
            compound commands (``;``, ``&&``, ``||``, ``|``, ``&``, newlines)
            take the most restrictive decision across their segments.
        command_decider: optional per-segment hook consulted *before* the
            static ``command_rules``; return a :data:`Decision` to override or
            ``None`` to fall through. See :data:`CommandDecider`.
    """

    write: Decision = "allow"
    read_outside: Decision = "deny"
    write_outside: Decision = "deny"
    path_rules: tuple[PathRule, ...] = ()
    denied_paths: tuple[str, ...] = ()
    allow_shell: bool = True
    shell_default: Decision = "ask"
    command_rules: tuple[CommandRule, ...] = ()
    command_decider: "CommandDecider | None" = None

    def __post_init__(self) -> None:
        # ``allow_shell=False`` already forces every command to ``deny`` in
        # :meth:`decide_command`, so a permissive ``shell_default`` alongside it
        # is a contradiction. Normalize it to ``"deny"`` (rather than raise) so
        # the object's fields can't misrepresent what the policy actually does —
        # e.g. ``WorkspacePolicy(allow_shell=False)`` no longer reads as if it
        # would ``ask``. Frozen dataclass, hence ``object.__setattr__``.
        if not self.allow_shell and self.shell_default != "deny":
            object.__setattr__(self, "shell_default", "deny")

    # ------------------------------------------------------------------ #
    # Presets
    # ------------------------------------------------------------------ #

    @classmethod
    def readonly(
        cls,
        *,
        denied_paths: tuple[str, ...] = (),
        path_rules: tuple[PathRule, ...] = (),
    ) -> "WorkspacePolicy":
        """Read the workspace only: no writes anywhere, no shell."""
        return cls(
            write="deny",
            read_outside="deny",
            write_outside="deny",
            path_rules=path_rules,
            denied_paths=denied_paths,
            allow_shell=False,
            shell_default="deny",
        )

    @classmethod
    def coding(
        cls,
        *,
        command_rules: tuple[CommandRule, ...] = (),
        denied_paths: tuple[str, ...] = (),
        path_rules: tuple[PathRule, ...] = (),
        command_decider: "CommandDecider | None" = None,
    ) -> "WorkspacePolicy":
        """Full workspace access; anything outside asks for approval or is denied.

        Reads outside the root require approval, writes outside are denied,
        and shell commands ask by default — the Claude-Code-like posture:
        free inside the project, human-in-the-loop beyond it.
        """
        return cls(
            write="allow",
            read_outside="ask",
            write_outside="deny",
            path_rules=path_rules,
            denied_paths=denied_paths,
            shell_default="ask",
            command_rules=command_rules,
            command_decider=command_decider,
        )

    @classmethod
    def trusted(
        cls,
        *,
        command_rules: tuple[CommandRule, ...] = (),
        denied_paths: tuple[str, ...] = (),
        path_rules: tuple[PathRule, ...] = (),
        command_decider: "CommandDecider | None" = None,
    ) -> "WorkspacePolicy":
        """Read anywhere, write the workspace; shell runs without approval.

        Writes outside the root still ask — a cheap safety valve, since the
        approval channel exists anyway.
        """
        return cls(
            write="allow",
            read_outside="allow",
            write_outside="ask",
            path_rules=path_rules,
            denied_paths=denied_paths,
            shell_default="allow",
            command_rules=command_rules,
            command_decider=command_decider,
        )

    # ------------------------------------------------------------------ #
    # Decisions
    # ------------------------------------------------------------------ #

    def decide_path(self, *, rel: str | None, abs_posix: str, op: FileOp) -> Decision:
        """Return the ACL decision for one resolved path.

        ``rel`` is the workspace-relative POSIX path when the resolved path
        is inside the root, ``None`` when it is outside; ``abs_posix`` is the
        resolved absolute POSIX path in either case. Evaluation order:
        ``denied_paths`` → ``path_rules`` (first match wins) → defaults.
        """
        for pattern in self.denied_paths:
            if _pattern_matches(pattern, rel=rel, abs_posix=abs_posix):
                return "deny"
        for rule in self.path_rules:
            if op in rule.ops and _pattern_matches(
                rule.pattern, rel=rel, abs_posix=abs_posix
            ):
                return rule.action
        if rel is not None:
            return "allow" if op == "read" else self.write
        return self.read_outside if op == "read" else self.write_outside

    def decide_command(self, command: str) -> Decision:
        """Return the static-rule decision for one shell command line.

        The command is split on shell control operators and each segment is
        judged independently; the most restrictive decision wins, so an
        ``allow`` rule can never whitelist a compound command that smuggles
        in something stricter.

        This judges the *command words* only. Path-looking tokens inside the
        command are additionally judged against the path ACL by the session
        (``WorkspaceSession.decide_command``), which merges both verdicts.

        Best-effort by design: segmentation is lexical, so command
        substitution (``$(...)``, backticks), ``eval``, or an interpreter
        one-liner can still hide work from the rules. This is a policy gate,
        not a security boundary — see the module docstring.
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
        if self.command_decider is not None:
            verdict = self.command_decider(segment)
            # Ignore anything that isn't a valid Decision (incl. None): fall
            # through to the static rules rather than crash on a bad hook.
            if verdict in ("allow", "ask", "deny"):
                return verdict
        words = segment.split()
        for rule in self.command_rules:
            pattern_words = rule.pattern.split()
            if words[: len(pattern_words)] == pattern_words:
                return rule.action
        return self.shell_default


def merge_decisions(*decisions: Decision) -> Decision:
    """Combine decisions, most restrictive wins."""
    result: Decision = "allow"
    for decision in decisions:
        if _SEVERITY[decision] > _SEVERITY[result]:
            result = decision
    return result


__all__ = [
    "CommandDecider",
    "CommandRule",
    "Decision",
    "FileOp",
    "PathRule",
    "WorkspacePolicy",
    "merge_decisions",
]
