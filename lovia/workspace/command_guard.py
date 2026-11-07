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

**Every argument is a claim.** The command word itself (``git``, ``cat``) is
an executable looked up on PATH, not a file, so it counts only when spelled
as a path (``./run.sh``); every other word is a ``read`` claim, bare names
included — ``cat id_rsa`` must meet the same credential rule as
``read_file("id_rsa")``, now that ``cat`` runs without a prompt. A denied
pattern that happens to equal a subcommand (``denied_paths=("build",)``
against ``npm run build``) therefore denies that command: stricter, never
looser, and a rare configuration.

**Writes are shell redirection, nothing else.** ``>``, ``>>``, ``&>`` mark
the shell's own writes. An argument a program treats as an output path
(``sort -o x``, ``dd of=/dev/x``, ``cp src /etc/hosts``) is lexically a
read, and this module does not try to know better: per-command option
semantics are unbounded (``-o x``, ``-ox``, ``--output=x``, ``--output x``,
…), and a table of them is only ever one spelling short. The policy closes
that gap the other way round — a command that *names* an output-looking
option is never allowed outright, whatever the spelling (see
``WorkspacePolicy.decide_command``) — and the consequence of the remaining
read/write blur is bounded: in ``coding`` (``read_outside="ask"``) an
outside write argument still stops at approval; only in ``trusted`` does it
pass unprompted, which is the point of ``trusted``. Mandatory write
enforcement is the job of a sandboxed executor (Seatbelt/bubblewrap) or an
isolated backend implementing the same session protocol.
"""

from __future__ import annotations

import os
import re
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
# A cmd.exe switch: one or two letters after a slash, optionally qualified
# (``/a:d``). Longer slash-words (``/Users/neo``) are paths.
_CMD_SWITCH = re.compile(r"/[A-Za-z]{1,2}(:\S*)?")

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


def _pathish(token: str, *, command: bool = False) -> str | None:
    """Return the path a word token appears to reference, or ``None``.

    Flags are skipped unless they carry a ``=value`` payload (``--out=/x``);
    a ``key=value`` token whose value looks rooted (``of=/dev/x``,
    ``FOO=/etc/x``) yields the value. URLs are never paths. Every other word
    is a claim, bare names included (``id_rsa``, ``Makefile``) — a relative
    false positive resolves inside the root and cannot escalate the decision
    (see module docstring). ``command=True`` is the command word of a
    segment: an executable, not a file, so it counts only when spelled as a
    path (``./run.sh``, ``/usr/bin/python``).
    """
    if token.startswith("-"):
        _, eq, rhs = token.partition("=")
        if not eq:
            return None
        token = rhs
    elif os.name == "nt" and _CMD_SWITCH.fullmatch(token):
        # ``dir /b``, ``del /s /q``, ``dir /a:d``: a cmd.exe switch, not a
        # path at the drive root.
        return None
    elif "=" in token:
        _, _, rhs = token.partition("=")
        if rhs.startswith(("/", "~", ".")) or os.path.isabs(rhs):
            token = rhs
    if not token or "://" in token:
        return None
    if command and not (
        "/" in token or os.sep in token or "." in token or token.startswith("~")
    ):
        return None
    return token


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
    # A shell starts a comment only at a word's start; shlex would otherwise
    # cut `input#tag` in half and lose everything after it.
    lexer.commenters = ""
    if os.name == "nt":
        lexer.escape = ""  # cmd.exe: a backslash separates path segments
    try:
        tokens = list(lexer)
    except ValueError:
        return []

    claims: list[tuple[FileOp, str]] = []
    pending: FileOp | None = None  # op of a redirect waiting for its target
    skip_next = False  # heredoc delimiter / herestring literal
    at_start = True  # the next word is a segment's command word

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
            else:
                at_start = True  # ; | || & && ( — a new segment starts
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
        if token.isdigit():
            continue  # an fd number (`2>&1`) or a count (`head -n 5`), never a file
        at_command = at_start
        # A leading `VAR=value` keeps the command position for the next word;
        # like the command word it is a claim only when its value is rooted.
        name, eq, _ = token.partition("=")
        at_start = at_command and bool(eq) and name.isidentifier()
        path = _pathish(token, command=at_command)
        if path is not None and not _is_pseudo_device(path):
            claims.append(("read", path))
    return claims
