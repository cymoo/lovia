"""Lexical path extraction from shell commands — the shell/file-policy bridge.

The file tools enforce the path ACL directly; a shell command can name the
same paths as free-form text. This module extracts the *path claims* a
command line appears to make — redirection targets as writes, path-looking
argument tokens as reads — so the session can judge them with the same
:meth:`~lovia.workspace.policy.WorkspacePolicy.decide_path` ACL and merge the
verdict with the static command rules (most restrictive wins).

Advisory by design
==================

This is a lexical guard, not a security boundary: it cannot see paths built
at runtime (``cat $(echo /etc/passwd)``), read by interpreters
(``python -c 'open(...)'``), or hidden behind ``eval``. Its failure mode is
deliberately one-sided:

* A **missed** path claim falls back to the static command rules — never
  *looser* than the rules alone.
* A **false** claim is almost always a relative token (``sed 's/a/b/'``),
  which resolves *inside* the workspace root where reads are always allowed —
  so it cannot escalate the decision. Only tokens that resolve outside the
  root or match a denied pattern change the verdict, and those are exactly
  the ones worth flagging.

**Read vs write is only distinguished for shell redirection.** Shell syntax
marks its own writes — ``>``, ``>>``, ``&>`` — and those become ``write``
claims. An *argument* that a program happens to treat as an output path
(``dd of=/dev/x``, ``cp src /etc/hosts``, ``tar -f out.tar``) is
indistinguishable from a read argument without per-command semantics, so
every non-redirection token is recorded as a ``read``. The consequence is
bounded: in ``coding`` (``read_outside="ask"``) an outside write argument
still stops at approval — it asks instead of denying, but does not slip
through; only in ``trusted`` (``read_outside="allow"``) does it pass
unprompted, which is the point of ``trusted`` (shell already defaults to
``allow`` there). Classifying such arguments as writes would demand a
per-command allow-list that is both unbounded and prone to false positives
(``--config=/etc/app.conf`` is a read). Mandatory write enforcement is the
job of a sandboxed executor (Seatbelt/bubblewrap) or an isolated backend
implementing the same session protocol.
"""

from __future__ import annotations

import shlex

from .policy import FileOp

__all__ = ["extract_path_claims"]

# Operator tokens produced by shlex(punctuation_chars=True). Redirections
# that write their target, the plain input redirection, and heredoc/herestring
# forms whose "target" is a delimiter or literal, not a path.
_REDIRECT_WRITE = {">", ">>", "&>", "&>>", ">&", ">|"}
_REDIRECT_READ = {"<"}
_HEREDOC = {"<<", "<<-", "<<<"}
_PUNCTUATION = set("();<>|&")

# Shell-plumbing pseudo-devices, not data access: `2>/dev/null` or
# `< /dev/stdin` must never escalate a decision (under coding,
# write_outside="deny" would otherwise turn every `2>/dev/null` into a hard
# deny). Other /dev paths still count — a redirect onto /dev/disk0 *should*
# trip the ACL.
_PSEUDO_DEVICES = {
    "/dev/null",
    "/dev/zero",
    "/dev/stdin",
    "/dev/stdout",
    "/dev/stderr",
    "/dev/tty",
    "/dev/random",
    "/dev/urandom",
}


def _is_operator(token: str) -> bool:
    return bool(token) and all(ch in _PUNCTUATION for ch in token)


def _is_pseudo_device(token: str) -> bool:
    return token in _PSEUDO_DEVICES or token.startswith("/dev/fd/")


def _pathish(token: str) -> str | None:
    """Return the path a word token appears to reference, or ``None``.

    Flags are skipped unless they carry a ``=value`` payload (``--out=/x``);
    a ``key=value`` token whose value looks rooted (``of=/dev/x``,
    ``FOO=/etc/x``) yields the value. URLs are never paths. A token counts as
    path-looking when it contains a ``/`` or a ``.`` or starts with ``~`` —
    deliberately generous, because a relative false positive resolves inside
    the root and cannot escalate the decision (see module docstring); bare
    words like ``install`` stay exempt so a denied *pattern* can't
    accidentally match a subcommand name.
    """
    if token.startswith("-"):
        _, eq, rhs = token.partition("=")
        if not eq:
            return None
        token = rhs
    elif "=" in token:
        _, _, rhs = token.partition("=")
        if rhs.startswith(("/", "~", ".")):
            token = rhs
    if not token or "://" in token:
        return None
    if "/" in token or "." in token or token.startswith("~"):
        return token
    return None


def extract_path_claims(command: str) -> list[tuple[FileOp, str]]:
    """Extract ``(op, path)`` claims from one shell command line.

    Redirection targets are ``write`` claims (``<`` sources are ``read``);
    fd duplications (``2>&1``) and pseudo-devices (``/dev/null``, ...) are
    ignored; every other path-looking word token is a ``read`` claim
    (executing a binary is treated as reading it). Returns ``[]`` when the
    command cannot be tokenized (unbalanced quotes), leaving the decision to
    the static command rules alone.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return []

    claims: list[tuple[FileOp, str]] = []
    pending: FileOp | None = None  # op of a redirect waiting for its target
    skip_next = False  # heredoc delimiter / herestring literal

    for token in tokens:
        if _is_operator(token):
            pending = None
            skip_next = False
            if token in _HEREDOC:
                skip_next = True
            elif token in _REDIRECT_WRITE:
                pending = "write"
            elif token in _REDIRECT_READ:
                pending = "read"
            continue
        if skip_next:
            skip_next = False
            continue
        if pending is not None:
            op, pending = pending, None
            # `2>&1` tokenizes as `2`, `>&`, `1`: a pure-digit (or `-`)
            # target is an fd duplication, not a file.
            if not (token.isdigit() or token == "-" or _is_pseudo_device(token)):
                claims.append((op, token))
            continue
        path = _pathish(token)
        if path is not None and not _is_pseudo_device(path):
            claims.append(("read", path))
    return claims
