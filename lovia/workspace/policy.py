"""Workspace permission policy.

A :class:`WorkspacePolicy` decides what the built-in workspace tools may do.
Both file paths and shell commands are judged with the same three-valued
vocabulary — ``allow`` runs freely, ``ask`` routes through the human-approval
channel, ``deny`` is rejected outright — and every decision comes back as a
:class:`~lovia.approvals.Verdict`: the action plus *why* (shown to the human
on ``ask``, told to the model on ``deny``) and the *scope* an "always allow"
grant would cover.

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

import os
import posixpath
import re
import shlex
from dataclasses import dataclass
from fnmatch import translate as fnmatch_translate
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Literal

from ..approvals import Decision, Verdict, as_verdict, merge_verdicts
from . import paths
from .paths import is_pseudo_device

FileOp = Literal["read", "write"]

# A per-segment command decider: given one command segment it returns a
# Decision or Verdict to override the static rules, or None to fall through
# to them. A lightweight, complete extension point — the static command_rules
# are prefix-only by design, so route anything richer (regex, allow-lists,
# context-aware checks) through this hook.
CommandDecider = Callable[[str], "Decision | Verdict | None"]

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


def _glob(name: str, pattern: str) -> bool:
    """``fnmatch``, ignoring case where the file system does."""
    fold = paths.FOLD_CASE
    head, match = _glob_matcher(pattern, fold)
    name = os.path.normcase(name.lower() if fold else name)
    return name.startswith(head) and match(name) is not None


@lru_cache(maxsize=1024)
def _glob_matcher(pattern: str, fold: bool) -> tuple[str, Callable[[str], Any]]:
    """``pattern`` prepared once for :func:`_glob`, which a bulk walk calls
    for every entry and every rule: normalized as ``fnmatch`` normalizes it,
    compiled, and its literal head — the text before the first wildcard,
    which every match starts with — so most names are turned away by a
    ``startswith`` before the regex runs."""
    pat = os.path.normcase(pattern.lower() if fold else pattern)
    head = re.split(r"[*?[]", pat, maxsplit=1)[0]
    return head, re.compile(fnmatch_translate(pat)).match


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
    if pat.startswith("**/"):
        # gitignore's ``**/foo``: foo at any depth, the root included — the
        # way to name a file anywhere *inside* the workspace without the
        # bare form's reach outside it.
        pat = pat[3:]
        return _path_matches(rel_path, pat) or _path_matches(rel_path, "*/" + pat)
    if "/" in pat:
        return _glob(rel_path, pat) or _glob(rel_path, pat + "/*")
    return any(_glob(segment, pat) for segment in rel_path.split("/"))


@lru_cache(maxsize=1024)
def _canonical_pattern(pattern: str) -> str | None:
    """Expand ``~`` and canonicalize an absolute-style pattern's real prefix.

    ``decide_path`` compares against a *resolved* absolute path, so the
    pattern must be resolved too or a symlinked prefix silently defeats it
    (macOS ``/etc`` → ``/private/etc``; a symlinked home). ``Path.resolve``
    follows only components that exist, leaving glob segments (``*``, ``**``)
    untouched — ``/etc/ssl/**`` → ``/private/etc/ssl/**``. Cached because
    patterns are static and this runs per entry during list/grep. Returns
    ``None`` when the pattern is empty or has no resolvable home.
    """
    pat = pattern
    if pat.startswith("~"):
        try:
            pat = Path(pat).expanduser().as_posix()
        except RuntimeError:
            return None
    try:
        pat = Path(pat).resolve().as_posix()
    except OSError:
        pass
    pat = pat.rstrip("/")
    return pat or None


def _abs_matches(abs_posix: str, pattern: str) -> bool:
    """Match a resolved absolute POSIX path against an absolute or ``~`` pattern.

    Like the relative match, a pattern also covers everything beneath it, so
    ``"~/notes"`` grants/denies the whole subtree.
    """
    pat = _canonical_pattern(pattern)
    if pat is None:
        return False
    return _glob(abs_posix, pat) or _glob(abs_posix, pat + "/*")


def _pattern_matches(pattern: str, *, rel: str | None, abs_posix: str) -> bool:
    """Dispatch one ACL pattern by its form.

    ``/``- or ``~``-prefixed patterns, and anything else the host calls
    absolute (Windows ``C:/data``, ``C:\\data``, UNC), match the resolved
    absolute path. Patterns containing ``/`` are workspace-relative (they
    never match outside the root). A *bare* pattern names a file or directory
    wherever it appears — inside or outside the root — so
    ``denied_paths=(".env*",)`` still bites when ``read_outside`` is
    permissive.
    """
    if pattern.startswith(("/", "~")) or os.path.isabs(pattern):
        return _abs_matches(abs_posix, pattern)
    if rel is not None:
        return _path_matches(rel, pattern)
    if "/" not in pattern.rstrip("/"):
        return _path_matches(abs_posix.lstrip("/"), pattern)
    return False


def _tilde(abs_posix: str) -> str:
    """Collapse the home directory to ``~`` for display and grant keys."""
    try:
        home = Path.home().resolve().as_posix().rstrip("/")
    except (RuntimeError, OSError):
        return abs_posix
    if abs_posix == home:
        return "~"
    if home and abs_posix.startswith(home + "/"):
        return "~" + abs_posix[len(home) :]
    return abs_posix


def _dir_of(rel: str | None, abs_posix: str) -> str:
    """The directory an "always allow" for a default-rule path decision covers.

    The target itself when it is a directory (``list_files('~/notes')``),
    else its parent — ``decide_path`` is static otherwise, but a file's
    parent and a directory's parent are different grants.
    """
    target = rel if rel is not None else abs_posix
    if os.path.isdir(abs_posix):
        chosen = target
    else:
        chosen = posixpath.dirname(target)
    if rel is not None:
        return chosen or "."
    return _tilde(chosen or "/")


@dataclass(frozen=True)
class CommandRule:
    """One shell-command rule: a word-boundary prefix and a decision.

    ``pattern`` matches when the command starts with the same
    whitespace-separated words: ``"git push"`` matches ``git push origin``
    but not ``git pushx``. ``flags`` are words that must *also* appear,
    anywhere in the segment — ``CommandRule("git push", "deny",
    flags=("--force",))`` stops ``git push origin main --force`` too. A
    one-letter short flag (``-f``) is satisfied by a short-flag group that
    contains its letter (``-fu``); anything longer (``--force``,
    ``-Recurse``, ``/s``) must appear as that exact word, and words after a
    bare ``--`` never count. Flags are flag-shaped words only; positional
    arguments stay in the prefix, so ``python -m pytest`` cannot be written
    as a rule that also matches ``python -m pip install pytest``.

    ``reason`` is what the human sees when the rule asks (and the model when
    it denies); left empty, a generic one is derived from the pattern.
    ``grant=False`` withholds the "always allow" option for an ``ask`` rule —
    set it where the pattern is so broad (``sudo``, ``ssh``) that remembering
    it would mean remembering everything.
    """

    pattern: str
    action: Decision
    reason: str = ""
    grant: bool = True
    flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for flag in self.flags:
            if not _is_flag(flag):
                raise ValueError(
                    f"CommandRule flags must be flag-shaped (-x, --xx, /x), got {flag!r}"
                )

    @property
    def key(self) -> str:
        """The rule as a grant scope: pattern plus flags, so two ``ask``
        rules on one prefix (``git push --tags`` / ``--delete``) stay apart."""
        return " ".join((self.pattern, *self.flags))

    def matches(self, words: list[str]) -> bool:
        pattern_words = self.pattern.split()
        if words[: len(pattern_words)] != pattern_words:
            return False
        rest = words[len(pattern_words) :]
        return all(_has_flag(rest, flag) for flag in self.flags)


def _is_flag(word: str) -> bool:
    return len(word) > 1 and (word[0] in "-/") and not word.startswith("---")


def _has_flag(words: list[str], flag: str) -> bool:
    short = len(flag) == 2 and flag[0] == "-"  # -f, not --f or /s
    for word in words:
        if word == "--":
            return False
        if word == flag:
            return True
        if (
            short
            and len(word) > 2
            and word[0] == "-"
            and word[1] != "-"
            and flag[1] in word[1:]
        ):
            return True
    return False


# Leading words a shell runs the real command *through*: the shell's own
# builtins for it (``command``, ``exec``, ``builtin``; ``call`` in cmd.exe)
# and the coreutils that run a command unchanged. They are stripped before
# the rules look at a segment, and a segment that had any is never allowed
# outright: ``PATH=…``, ``LD_PRELOAD=…`` change which program runs, so an
# allow rule for the inner command cannot vouch for the wrapped one — but a
# deny rule for it still bites (``env git push --force``). Interpreters
# (``bash -c``, ``xargs``, ``python -c``) are not wrappers: they run what
# they are given as *data*, which the rules never see (module docstring).
_THIN_WRAPPERS = frozenset(
    {"command", "exec", "builtin", "env", "nice", "nohup", "time", "call"}
)

# Words that head a segment without being the program that runs: a wrapper
# left in place because it takes options (``env -i``, ``nice -n 1``,
# ``timeout --foreground 5``), or a ``for`` / ``case`` / ``select`` header. A
# grant for the word would cover whatever follows: no scope.
_NOT_A_PROGRAM = _THIN_WRAPPERS | {"timeout", "for", "case", "select"}

# The same principle for arguments: an allow rule vouches for a command over
# *literal* arguments, which the path guard can judge one by one. Anything
# else about a word the guard cannot see, so the segment is never allowed
# outright (a false hit costs one question):
#
# * a word that looks like an output option — the command may write wherever
#   it points (``sort -o x``, ``-ox``, ``-ro x``, ``'-ox'``, ``--output=x``;
#   a table of spellings is only ever one short, so: a long option starting
#   ``--out`` / ``--log-file`` / ``--junit``, or a short-option group with an
#   ``o`` first, last or in at most three letters — see :func:`_short_output`)
#   — unless it points at a pseudo-device (``curl -o /dev/null``);
# * a glob, variable or substitution — the shell turns it into paths the
#   guard never sees (``cat *`` reads ``id_rsa``; ``cat $F``; ``$(…)``), or
#   into a program it never sees (``$CMD``, ``./tool*``), which a grant for
#   the word as spelled would then cover. Quoted against the shell, the same
#   characters are literal (``find . -name '*.py'``, ``awk '{print $2}'``,
#   ``echo "done?"``) and the word is an argument like any other. A program
#   that globs a quoted pattern itself still names its directory to the path
#   guard (``'~/.ssh/*'``).
#
# Each check is a heuristic with false hits, so its reason is shown only when
# it changes the outcome — an allowed command that now asks. A command that
# asks anyway just loses its "always allow" offer, silently: the card's
# reason line is for what the person should weigh, not for the guard's
# bookkeeping.
_OUTPUT_LIKE = ("--out", "--log-file", "--junit")
# cmd.exe expands `%VAR%` and strips the `^` escape (`type .e^nv` is `.env`).
_EXPANSION_CHARS = frozenset("*?[{$`" + ("%^" if os.name == "nt" else ""))


def _mask_literal(command: str) -> str | None:
    """``command`` as the shell reads its plain quoting: each character it
    takes literally that would otherwise expand or end a command turned into
    ``x`` — inside single quotes, a glob, brace or separator inside double
    quotes (``$`` still expands there), and one escaped by a backslash. Only
    such characters change, never a quote or a blank, so the result splits
    and lexes like the command, place for place: split it to find where the
    command really ends, lex it to see what really expands.

    None when the line holds what gives its quotes another meaning: a
    here-document (unquoted ``<<``: quotes in its body quote nothing), a
    comment (an unquoted ``#`` that starts a word: a quote in it opens
    nothing), or a nested context (``$(``, ``${`` or a backtick outside
    single quotes, which start quoting of their own). A closed list, by
    design: on such a line nothing is guessed, and no grant is offered.

    Windows' cmd.exe has no single quotes and expands ``%VAR%`` inside double
    ones: there the command is its own reading, as it always was."""
    if os.name == "nt":
        return command
    out: list[str] = []
    quote = ""
    escaped = False
    word_start = True  # the shell starts a comment only where a word starts
    for i, c in enumerate(command):
        if escaped:
            escaped, word_start = False, False
            out.append("x" if c in _LITERAL_TELLING else c)
        elif quote == "'":
            quote = "" if c == "'" else quote
            out.append("x" if c in _LITERAL_TELLING else c)
        elif c == "\\":
            escaped = True
            out.append(c)
        elif c == "`" or command.startswith(("$(", "${"), i):
            return None
        elif quote == '"':
            quote = "" if c == '"' else quote
            out.append("x" if c in _GLOBS_AND_SEPARATORS else c)
        elif (c == "#" and word_start) or command.startswith("<<", i):
            return None
        else:
            quote = c if c in "'\"" else ""
            out.append(c)
            word_start = c in _WORD_BREAKS
    return "".join(out)


def _segments(command: str, reading: str | None = None) -> list[tuple[str, str]]:
    """``command``'s commands with their stretch of ``reading`` (default: the
    command itself), cut where ``reading`` has a control operator — the two
    strings share every character's place, so the cuts fit both."""
    reading = command if reading is None else reading
    cuts = [0]
    for match in _COMMAND_SPLIT.finditer(reading):
        cuts += [match.start(), match.end()]
    cuts.append(len(command))
    pairs = (
        (command[a:b].strip(), reading[a:b].strip())
        for a, b in zip(cuts[::2], cuts[1::2])
    )
    return [(seg, read) for seg, read in pairs if seg]


# Characters whose literal use matters to the split or to expansion.
_SEPARATORS = frozenset("|&;\n\r")
_LITERAL_TELLING = _EXPANSION_CHARS | _SEPARATORS
_GLOBS_AND_SEPARATORS = frozenset("*?[{") | _SEPARATORS
# What ends a word, so that a `#` after it starts a comment.
_WORD_BREAKS = frozenset(" \t\r\n;&|()<>")


def _uncertain_word(
    segment: list[str], command: list[str], masked: list[str] | None = None
) -> str | None:
    """The first word an allow rule cannot vouch for, with why, or None.

    ``segment`` is every word as lexed — the shell expands everywhere, a
    redirect target included, so expansion is judged before any syntax is
    dropped; ``masked`` is the same words lexed from :func:`_mask_literal`'s
    segment, so a quoted ``'*.py'`` is not taken for a glob. ``command`` is
    what is left after the syntax, whose arguments alone can be options.
    """
    seen = masked if masked is not None and len(masked) == len(segment) else segment
    for word, judged in zip(segment, seen):
        if any(c in judged for c in _EXPANSION_CHARS):
            return f"'{word}' is expanded by the shell"
    args = command[1:]
    for i, word in enumerate(args):
        if word == "--":
            break  # ends options
        target = _output_target(word, args[i + 1 : i + 2])
        if target is not None and not is_pseudo_device(target):
            return f"'{word}' may be an output option"
    return None


def _output_target(word: str, following: list[str]) -> str | None:
    """Where ``word`` would write if it is an output option, or None if it
    does not look like one. ``""`` when it would but names nothing."""
    name, eq, value = word.partition("=")
    if name.startswith("--"):
        if not name.startswith(_OUTPUT_LIKE):
            return None
        rest = ""
    elif name.startswith("-"):
        match = _LETTERS.match(name, 1)
        letters = match.group() if match else ""
        if not _short_output(letters):
            return None
        rest = name[2 + letters.index("o") :]
    else:
        return None
    if eq:
        return value
    return rest or (following[0] if following else "")


# A single-dash word's option letters; what follows the first non-letter is
# a value glued on (`-xo.git/config`, `-o/dev/null`).
_LETTERS = re.compile(r"[A-Za-z]*")


def _short_output(letters: str) -> bool:
    """Whether a single-dash group of option letters may hold an ``-o``
    taking a value: ``o`` first (``-o``, ``-ox``), last (``-sLo x``,
    ``-xo.git/config``), or in a group too short to be a long option
    (``-rox``). Not ``-connect`` or ``-noout`` (openssl). An all-letter miss
    (``-xofile``) names a bare file in the working directory; every
    protected path holds a ``.``, ``/`` or ``~``, which ends the letters."""
    if "o" not in letters:
        return False
    return letters[0] == "o" or letters[-1] == "o" or len(letters) <= 3


def _words(segment: str) -> list[str]:
    """A segment's words as the shell sees them: quotes removed, so
    ``'-o.git/config'`` is the option it is; operators as words of their
    own (``(git`` is ``(`` then ``git``, ``2>&1`` is ``2``, ``>&``, ``1``);
    no comment stripping, because a shell starts a comment only at a word's
    start and shlex would cut ``input#tag`` in half. Unbalanced quotes fall
    back to whitespace splitting (the guard gives up on them the same way)."""
    lexer = shlex.shlex(segment, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    if os.name == "nt":
        lexer.escape = ""  # cmd.exe: a backslash separates path segments
    try:
        return list(lexer)
    except ValueError:
        return segment.split()


# What the shell keeps for itself in the spellings a model writes:
# redirections and their targets (``cmd >/dev/null 2>&1``), parentheses
# (``(cd x && ls)``) and a control-flow word before the command (``if cmd``,
# ``! cmd``). They name no program, so they are dropped and the command's
# own verdict is the segment's: ``(ls)`` is ``ls``, ``! git push --force``
# meets the deny. That is the whole of it — a rule stops the spellings it
# names after this, and nothing cleverer: a ``case`` pattern or a quoted
# ``)`` is read as it comes, and a model bent on hiding a command has
# ``bash -c`` anyway (module docstring). Under ``coding`` every spelling the
# rules miss still asks, with the spelling on the card.
_SYNTAX_WORDS = frozenset("! { } if then else elif fi while until do done esac".split())
_OPERATOR_CHARS = frozenset("()<>&|;")


def _is_redirect(word: str) -> bool:
    return bool(word) and set(word) <= _OPERATOR_CHARS and ("<" in word or ">" in word)


def _program_words(lexed: list[str]) -> list[str]:
    """The words the program receives: the segment minus redirections with
    their targets, parentheses, and the control-flow words before it."""
    words: list[str] = []
    skip = False
    for i, word in enumerate(lexed):
        if skip:
            skip = False  # a redirection's target
        elif _is_redirect(word):
            skip = True
        elif word.isdigit() and i + 1 < len(lexed) and _is_redirect(lexed[i + 1]):
            pass  # the fd of `2>&1`
        elif not set(word) <= {"(", ")"}:
            words.append(word)
    while words and words[0] in _SYNTAX_WORDS:
        words = words[1:]
    return words


def _strip_wrappers(lexed: list[str]) -> tuple[list[str], list[str]]:
    """Return ``(command words, wrappers)``: what the program receives once
    the shell's own syntax and any wrappers are gone, and the wrappers —
    syntax is not reported, since it changes nothing about what runs."""
    words = _program_words(lexed)
    stripped: list[str] = []
    while words:
        head = words[0]
        name, eq, _ = head.partition("=")
        if eq and name.isidentifier():  # VAR=value
            stripped.append(head)
            words = words[1:]
        elif head == "timeout" and len(words) > 2 and words[1][:1].isdigit():
            stripped.extend(words[:2])
            words = words[2:]
        elif head in _THIN_WRAPPERS and len(words) > 1 and not words[1].startswith("-"):
            stripped.append(head)
            words = words[1:]
        else:
            break
    return words, stripped


@dataclass(frozen=True)
class PathRule:
    """One path-ACL rule: a pattern, a decision, and the ops it covers.

    An absolute ``pattern`` (``/srv``, on Windows ``C:/data``) or one starting
    with ``~`` is matched against the resolved absolute path (and its
    subtree). A pattern containing ``/`` is workspace-relative and never
    matches outside the root. A *bare* pattern
    (``".env*"``, ``"secrets"``) names a file or directory anywhere — inside
    or outside the root (see :func:`_pattern_matches`). ``ops`` restricts the
    rule to reads and/or writes — accepts any iterable of ``"read"`` /
    ``"write"``. ``reason`` and ``grant`` as on :class:`CommandRule`.
    """

    pattern: str
    action: Decision
    ops: frozenset[FileOp] = frozenset({"read", "write"})
    reason: str = ""
    grant: bool = True

    def __post_init__(self) -> None:
        # Accept any iterable for ergonomics (("read",), {"write"}, ...).
        if not isinstance(self.ops, frozenset):
            object.__setattr__(self, "ops", frozenset(self.ops))


def _rule_verdict(
    action: Decision, reason: str, pattern: str, scopes: tuple[str, ...]
) -> Verdict:
    """A rule hit as a verdict: the author's reason or a derived one, and the
    rule's pattern as grant scope when it asks."""
    if action == "ask":
        return Verdict("ask", reason or f"'{pattern}' requires approval", scopes)
    if action == "deny":
        return Verdict("deny", reason or f"'{pattern}' is denied by workspace policy")
    return Verdict("allow", reason)


@dataclass(frozen=True)
class WorkspacePolicy:
    """What the workspace tools are allowed to do."""

    write: Decision = "allow"
    """Decision for writes *inside* the workspace root. Reads inside the
    root are always allowed — a workspace the agent cannot read is
    pointless."""

    read_outside: Decision = "deny"
    """Decision for reads outside the root (reached via an absolute path,
    ``~``, ``..`` or a symlink target)."""

    write_outside: Decision = "deny"
    """Decision for writes outside the root."""

    path_rules: tuple[PathRule, ...] = ()
    """Ordered ACL consulted before the defaults above; first match wins.
    See :class:`PathRule`."""

    denied_paths: tuple[str, ...] = ()
    """Sugar for the common case — patterns that neither reads nor writes
    may touch, checked before ``path_rules``. Same pattern language as
    :class:`PathRule`."""

    allow_shell: bool = True
    """When ``False`` the shell tool is excluded and every command decision
    is ``deny``."""

    shell_default: Decision = "ask"
    """Decision for commands no rule matches."""

    command_rules: tuple[CommandRule, ...] = ()
    """Evaluated first-match-wins per command segment; compound commands
    (``;``, ``&&``, ``||``, ``|``, ``&``, newlines) take the most restrictive
    decision across their segments."""

    command_decider: "CommandDecider | None" = None
    """Per-segment hook consulted *before* the static ``command_rules``:
    return a :data:`Decision` / :class:`Verdict` to override or ``None`` to
    fall through. See :data:`CommandDecider`."""

    baseline: tuple["PathRule | CommandRule", ...] | None = None
    """Rules in force whatever the preset: judged on their own and merged
    most-restrictive with everything above, so they can only tighten — a
    user rule, the ``command_decider`` or a remembered approval never
    loosens them. Unlike the ordered rules above, *every* matching baseline
    rule counts (most restrictive wins): the baseline is a set of
    protections, and composing bundles in either order never lets one
    shadow another. Every *protection* lives here, so none can be undone by
    accident; the user layer above holds what *grants*. ``None`` (the
    default) resolves to :data:`lovia.workspace.rules.DEFAULT_BASELINE`
    (dangerous commands denied, credential stores denied, writes to
    repository/tool configuration asked, ``.env`` reads asked); ``()``
    switches it off; a bundle of your own replaces it."""

    def __post_init__(self) -> None:
        # ``allow_shell=False`` already forces every command to ``deny`` in
        # :meth:`decide_command`, so a permissive ``shell_default`` alongside it
        # is a contradiction. Normalize it to ``"deny"`` (rather than raise) so
        # the object's fields can't misrepresent what the policy actually does —
        # e.g. ``WorkspacePolicy(allow_shell=False)`` no longer reads as if it
        # would ``ask``. Frozen dataclass, hence ``object.__setattr__``.
        if not self.allow_shell and self.shell_default != "deny":
            object.__setattr__(self, "shell_default", "deny")
        if self.baseline is None:
            # Resolved here, not as the field default: the bundles are built
            # from these very dataclasses, so ``rules`` imports this module.
            from .rules import DEFAULT_BASELINE

            object.__setattr__(self, "baseline", DEFAULT_BASELINE)

    @property
    def _baseline_paths(self) -> tuple[PathRule, ...]:
        return tuple(r for r in self.baseline or () if isinstance(r, PathRule))

    @property
    def _baseline_commands(self) -> tuple[CommandRule, ...]:
        return tuple(r for r in self.baseline or () if isinstance(r, CommandRule))

    # ------------------------------------------------------------------ #
    # Presets
    # ------------------------------------------------------------------ #

    # The presets are rule bundles (``lovia.workspace.rules``) composed in a
    # fixed order: the caller's rules first (first match wins, so they win),
    # then the preset's own. The baseline sits apart from that order.

    @classmethod
    def readonly(
        cls,
        *,
        denied_paths: tuple[str, ...] = (),
        path_rules: tuple[PathRule, ...] = (),
        baseline: tuple["PathRule | CommandRule", ...] | None = None,
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
            baseline=baseline,
        )

    @classmethod
    def coding(
        cls,
        *,
        command_rules: tuple[CommandRule, ...] = (),
        denied_paths: tuple[str, ...] = (),
        path_rules: tuple[PathRule, ...] = (),
        command_decider: "CommandDecider | None" = None,
        baseline: tuple["PathRule | CommandRule", ...] | None = None,
        shell_default: Decision = "ask",
    ) -> "WorkspacePolicy":
        """Full workspace access; reads anywhere, writes outside denied.

        Reads outside the root are allowed: what is worth protecting there —
        credential stores, ``.env`` files — is the baseline's to deny or ask,
        whatever the preset, and a question for every config file a task
        looks at (``/etc/nginx``, a sibling repository) only trains people
        to click Approve. Writes outside are denied.
        Shell: the hard-to-undo commands ask with a reason (``IRREVERSIBLE``),
        everything else follows ``shell_default`` — ``ask`` unless you choose
        the kits-style ``"allow"``. The preset vouches for *no* command: the
        path guard cannot audit a program's options, so which commands run
        unprompted is the deployer's call (``command_rules=READ_ONLY +
        DEV_TOOLS`` opts in, your rules first) or the user's, per chat, on the
        approval card.
        """
        from .rules import IRREVERSIBLE

        return cls(
            write="allow",
            read_outside="allow",
            write_outside="deny",
            path_rules=path_rules,
            denied_paths=denied_paths,
            shell_default=shell_default,
            command_rules=tuple(command_rules) + IRREVERSIBLE,
            command_decider=command_decider,
            baseline=baseline,
        )

    @classmethod
    def trusted(
        cls,
        *,
        command_rules: tuple[CommandRule, ...] = (),
        denied_paths: tuple[str, ...] = (),
        path_rules: tuple[PathRule, ...] = (),
        command_decider: "CommandDecider | None" = None,
        baseline: tuple["PathRule | CommandRule", ...] | None = None,
        shell_default: Decision = "allow",
    ) -> "WorkspacePolicy":
        """Read and write anywhere; shell runs without approval.

        Only the baseline interrupts here (the "all automatic" posture).
        Writes outside the root are allowed like everything else: an ask
        there stopped the file tools and the shell's own redirections
        (``> /tmp/out``), while ``tee``, ``cp`` or any program wrote the
        same file unasked — a prompt that protected nothing.
        """
        return cls(
            write="allow",
            read_outside="allow",
            write_outside="allow",
            path_rules=path_rules,
            denied_paths=denied_paths,
            shell_default=shell_default,
            command_rules=command_rules,
            command_decider=command_decider,
            baseline=baseline,
        )

    # ------------------------------------------------------------------ #
    # Decisions
    # ------------------------------------------------------------------ #

    def decide_path(self, *, rel: str | None, abs_posix: str, op: FileOp) -> Verdict:
        """Return the ACL verdict for one resolved path.

        ``rel`` is the workspace-relative POSIX path when the resolved path
        is inside the root, ``None`` when it is outside; ``abs_posix`` is the
        resolved absolute POSIX path in either case. Evaluation order:
        ``denied_paths`` → ``path_rules`` (first match wins) → defaults,
        merged most-restrictive with the ``baseline``. A default-rule ``ask``
        offers the target's directory as grant scope (``"read:~/notes"``); a
        rule hit offers the rule's pattern.
        """
        verdict = self._decide_path(rel=rel, abs_posix=abs_posix, op=op)
        base = self._path_rule_verdict(
            self._baseline_paths, rel=rel, abs_posix=abs_posix, op=op, every=True
        )
        # Normal verdict first: on a tie its reason (the deployer's own rule)
        # is the one worth repeating.
        return merge_verdicts(verdict, base) if base is not None else verdict

    def decide_argument(self, *, rel: str | None, abs_posix: str) -> Verdict:
        """The verdict for a path a shell command names as an argument.

        The ACL reads it as a read, but the program may write it (``ruff
        format ../repo``, ``pytest --basetemp=/x``) and the guard cannot tell
        which. So where outside reads are allowed by default but outside
        writes are not (``coding``), an outside argument asks — the session
        asks this only of a command that would otherwise run unasked."""
        verdict = self.decide_path(rel=rel, abs_posix=abs_posix, op="read")
        if (
            rel is None
            and verdict.action == "allow"
            and self.write_outside != "allow"
            and not self.rule_bound(rel=rel, abs_posix=abs_posix, op="read")
        ):
            where = _dir_of(None, abs_posix)
            return Verdict(
                "ask", f"'{where}' is outside the workspace.", (f"read:{where}",)
            )
        return verdict

    def rule_bound(self, *, rel: str | None, abs_posix: str, op: FileOp) -> bool:
        """Whether a *rule* (``denied_paths``, ``path_rules`` or the baseline)
        decides this path, as opposed to the root/outside defaults. A bulk
        walk under an already-approved outside directory lets the default
        ``ask`` through — the approval covered the subtree — but never a
        rule's."""
        if any(
            _pattern_matches(p, rel=rel, abs_posix=abs_posix) for p in self.denied_paths
        ):
            return True
        return any(
            self._path_rule_verdict(rules, rel=rel, abs_posix=abs_posix, op=op)
            is not None
            for rules in (self.path_rules, self._baseline_paths)
        )

    @staticmethod
    def _path_rule_verdict(
        rules: tuple[PathRule, ...],
        *,
        rel: str | None,
        abs_posix: str,
        op: FileOp,
        every: bool = False,
    ) -> Verdict | None:
        """The first matching rule's verdict — or, with ``every`` (the
        baseline), all matching rules merged most-restrictive."""
        hits: list[Verdict] = []
        for rule in rules:
            if op in rule.ops and _pattern_matches(
                rule.pattern, rel=rel, abs_posix=abs_posix
            ):
                scopes = (f"{op}:{rule.pattern}",) if rule.grant else ()
                hits.append(
                    _rule_verdict(rule.action, rule.reason, rule.pattern, scopes)
                )
                if not every:
                    break
        return merge_verdicts(*hits) if hits else None

    def _decide_path(self, *, rel: str | None, abs_posix: str, op: FileOp) -> Verdict:
        for pattern in self.denied_paths:
            if _pattern_matches(pattern, rel=rel, abs_posix=abs_posix):
                return Verdict("deny", f"'{pattern}' is off-limits by workspace policy")
        hit = self._path_rule_verdict(
            self.path_rules, rel=rel, abs_posix=abs_posix, op=op
        )
        if hit is not None:
            return hit
        verb = "reads" if op == "read" else "writes"
        if rel is not None:
            if op == "read" or self.write == "allow":
                return Verdict("allow")
            if self.write == "ask":
                # No reason: "this write needs approval" is the ask itself.
                return Verdict("ask", "", (f"write:{_dir_of(rel, abs_posix)}",))
            return Verdict("deny", "the workspace is read-only")
        action = self.read_outside if op == "read" else self.write_outside
        where = _dir_of(None, abs_posix)
        if action == "allow":
            return Verdict("allow")
        if action == "ask":
            return Verdict(
                "ask", f"Outside the workspace root ({where}).", (f"{op}:{where}",)
            )
        return Verdict(
            "deny", f"{verb} outside the workspace root are not permitted ({where})"
        )

    def decide_command(self, command: str) -> Verdict:
        """Return the static-rule verdict for one shell command line.

        The command is split on shell control operators and each segment is
        judged independently; the most restrictive decision wins, so an
        ``allow`` rule can never whitelist a compound command that smuggles
        in something stricter (see :func:`~lovia.approvals.merge_verdicts`
        for how the grant scope survives the merge).

        This judges the *command words* only. Path-looking tokens inside the
        command are additionally judged against the path ACL by the session
        (``WorkspaceSession.decide_command``), which merges both verdicts.

        Best-effort by design: segmentation reads plain quoting only (see
        :func:`_mask_literal`), so command substitution (``$(...)``,
        backticks), ``eval``, or an interpreter one-liner can still hide work
        from the rules. This is a policy gate, not a security boundary — see
        the module docstring.
        """
        if not self.allow_shell:
            return Verdict("deny", "the shell is disabled for this workspace")
        # The command where the shell ends its commands — not at a quoted `|`
        # (`jq '.[] | .name'`, `git commit -m "a; b"`) — read on the masked
        # line, cut on the command at the same places. Every grant on offer
        # then names a program at a real command's start.
        masked = _mask_literal(command)
        reading = masked if masked is not None else command
        segments = _segments(command, reading)
        if not segments:
            return self._default_command_verdict([])
        verdict = merge_verdicts(*(self._decide_segment(s, m) for s, m in segments))
        if masked is None and verdict.scopes:
            # A line read as spelled has its pieces judged, but a piece is no
            # program to remember: approve it once (silently, as for any
            # other withheld grant).
            verdict = Verdict("ask", verdict.reason)
        # What the lexical cut used to refuse stays refused — a user's deny
        # rule, the baseline, a decider, over every spelled-out piece
        # (`bash -c 'x; rm -rf /; true'`). It only tightens: none of its asks
        # or grants reach the person.
        spelled = _segments(command) if masked is not None else segments
        if [s for s, _ in spelled] != [s for s, _ in segments]:
            refusals = [
                v
                for v in (self._decide_segment(s, s) for s, _ in spelled)
                if v.action == "deny"
            ]
            if refusals:
                verdict = merge_verdicts(verdict, *refusals)
        return verdict

    def _decide_segment(self, segment: str, masked: str) -> Verdict:
        lexed = _words(segment)
        words, stripped = _strip_wrappers(lexed)
        # The words as the shell reads them: a quoted '*' is literal.
        uncertain = _uncertain_word(lexed, words, _words(masked))
        if not words:
            # Syntax alone (`fi`, `done`, `}`) runs nothing — unless the
            # shell expands something in it.
            return Verdict("ask", uncertain) if uncertain else Verdict("allow")
        verdict, by_rule = self._decide_words(segment, words)
        if stripped or uncertain:
            # The wrapper (or the word) is what neither an allow *rule*
            # nor a grant for the command can vouch for: a rule's allow is
            # capped at ask, and an ask loses its scope. A default of allow
            # (``trusted``) is not a rule vouching for anything — it is the
            # choice not to judge commands — and is left alone.
            why = f"'{' '.join(stripped)}' wraps the command" if stripped else uncertain
            if by_rule and verdict.action == "allow":
                verdict = Verdict("ask", f"{why}, so it needs approval")
            elif verdict.action == "ask":
                # It asked anyway: only the "always allow" offer goes, and
                # the guard's why stays off the card (see _OUTPUT_LIKE).
                verdict = Verdict("ask", verdict.reason)
        base = self._command_rule_verdict(self._baseline_commands, words, every=True)
        return merge_verdicts(verdict, base) if base is not None else verdict

    def _decide_words(self, segment: str, words: list[str]) -> tuple[Verdict, bool]:
        """The verdict and whether a static rule produced it (the decider saw
        the whole segment itself; the default vouches for nothing)."""
        if self.command_decider is not None:
            raw = self.command_decider(segment)
            if raw is not None:
                try:
                    return as_verdict(raw), False
                except TypeError:
                    # Fall through to the static rules rather than crash on a
                    # bad hook — never run unjudged, never abort the run.
                    pass
        hit = self._command_rule_verdict(self.command_rules, words)
        if hit is not None:
            return hit, True
        return self._default_command_verdict(words), False

    @staticmethod
    def _command_rule_verdict(
        rules: tuple[CommandRule, ...], words: list[str], *, every: bool = False
    ) -> Verdict | None:
        """As :meth:`_path_rule_verdict`, for command rules."""
        hits: list[Verdict] = []
        for rule in rules:
            if rule.matches(words):
                scopes = (f"shell:{rule.key}",) if rule.grant else ()
                hits.append(_rule_verdict(rule.action, rule.reason, rule.key, scopes))
                if not every:
                    break
        return merge_verdicts(*hits) if hits else None

    def _default_command_verdict(self, words: list[str]) -> Verdict:
        if self.shell_default == "ask":
            # The grant on offer is the command word, exactly as shown
            # ("always allow `ls` in this chat"); finer than that is a rule.
            # Words the guard cannot vouch for strip it (_decide_segment), as
            # does a head that is not the program (_NOT_A_PROGRAM). No reason:
            # nothing about the command stands out — it is just not on an
            # allow list, which is what being asked already says.
            scopes = (
                (f"shell:{words[0]}",)
                if words and words[0] not in _NOT_A_PROGRAM
                else ()
            )
            return Verdict("ask", "", scopes)
        if self.shell_default == "deny":
            return Verdict("deny", "shell commands are denied by workspace policy")
        return Verdict("allow")


__all__ = [
    "CommandDecider",
    "CommandRule",
    "Decision",
    "FileOp",
    "PathRule",
    "Verdict",
    "WorkspacePolicy",
    "merge_verdicts",
]
