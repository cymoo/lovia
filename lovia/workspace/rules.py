"""Rule bundles the workspace presets are built from.

Each bundle is a plain tuple of :class:`~lovia.workspace.policy.CommandRule` /
:class:`~lovia.workspace.policy.PathRule`, so a deployer composes a policy the
same way the presets do — ``Workspace.local(root, baseline=DANGEROUS_COMMANDS +
PROTECTED_CONFIG)`` drops the credential rules, ``WorkspacePolicy(command_rules=
mine + IRREVERSIBLE + READ_ONLY)`` skips the dev tools.

Platform split: the shell on Windows is ``cmd.exe`` (the local backend spawns
through ``COMSPEC``), whose commands and ``/x`` switches are its own, so each
command bundle comes as a cross-platform part plus one small part per host
shell, joined for *this* host under the short name. Same-named commands are
not the same command (``fc`` compares files in cmd and re-runs history in
bash), hence the split rather than one merged list.

Honesty note, as for the policy itself: these rules judge what the model
*writes*, not what the code it runs does. Allowing ``pytest`` is trusting the
project's own code to execute on the host — the premise of a coding agent,
and the reason the baseline is about the model's own hand, not a sandbox.
"""

from __future__ import annotations

import os

from .policy import CommandRule, PathRule

_NT = os.name == "nt"

# ---------------------------------------------------------------------------
# Green: inspection that prints no file contents, and the dev tools a coding
# agent lives in.
#
# The line is "prints no file contents", not "reads nothing": the path guard
# is lexical, and every content reader has its own way to reach a file it
# never names on the command line (`grep -r`, `sort --files0-from`, `file
# -f`, `git diff --no-index`, a glob) — a list of those is only ever one
# program short. Names, sizes, times and exit codes (`ls`, `du`, `stat`,
# `test`, `git status`) leak nothing the baseline guards; reading contents is
# `read_file`, which judges the file it opens, or a command the user is
# asked about. Git's own commands print *tracked* content (`git show`,
# `git log -p`): a secret committed to the repository is reachable through
# any git command and is repository hygiene, not the baseline's hand. The
# exception is `git diff`, which outside a repository diffs the two paths
# it is given, recursively, as if `--no-index` were spelled — so it is as
# gray as `cat`.
# ---------------------------------------------------------------------------

READ_ONLY_COMMON: tuple[CommandRule, ...] = tuple(
    CommandRule(p, "allow")
    for p in (
        "git status",
        "git log",
        "git show",
        "git blame",
        "git rev-parse",
        "git ls-files",
    )
)

READ_ONLY_POSIX: tuple[CommandRule, ...] = tuple(
    CommandRule(p, "allow")
    for p in (
        "ls",
        "pwd",
        "echo",
        "which",
        "stat",
        "du",
        "df",
        # Not `date`: GNU `date -f FILE` reads a file and echoes each line
        # it cannot parse — `-f.env` escapes the guard (the `now` tool tells
        # the time).
        "sleep",
        "test",
        "tree",
    )
)

READ_ONLY_CMD: tuple[CommandRule, ...] = tuple(
    CommandRule(p, "allow") for p in ("dir", "where", "echo", "cd", "tree")
)

READ_ONLY: tuple[CommandRule, ...] = READ_ONLY_COMMON + (
    READ_ONLY_CMD if _NT else READ_ONLY_POSIX
)
"""Inspection commands that change nothing and print no file contents:
allowed in ``coding``. ``cat`` & co. are gray — ``read_file`` is the reader."""

DEV_TOOLS: tuple[CommandRule, ...] = tuple(
    CommandRule(p, "allow")
    for p in (
        "pytest",
        "python -m pytest",
        "ruff check",
        "ruff format",
        "mypy",
        "npm test",
        "npx tsc",
        "cargo test",
        "cargo check",
        "cargo build",
        "go test",
        "go vet",
        "go build",
    )
)
"""Test, lint and build commands: allowed in ``coding``. They run the
project's own code — see the module docstring."""

# ---------------------------------------------------------------------------
# Red: not undoable, or reaching beyond the workspace. Asked in ``coding``.
# ``grant=False`` where the prefix is a whole world (``sudo``, ``docker``).
# ---------------------------------------------------------------------------

IRREVERSIBLE_COMMON: tuple[CommandRule, ...] = (
    CommandRule("git push", "ask", "Pushes commits to a remote."),
    CommandRule(
        "git reset", "ask", "Discards working-tree changes.", flags=("--hard",)
    ),
    CommandRule("git clean", "ask", "Deletes untracked files."),
    CommandRule("git branch", "ask", "Force-deletes a branch.", flags=("-D",)),
    CommandRule("gh pr create", "ask", "Opens a pull request."),
    CommandRule("gh release", "ask", "Publishes a release."),
    CommandRule("npm publish", "ask", "Publishes a package."),
    CommandRule("twine upload", "ask", "Publishes a package."),
    CommandRule("cargo publish", "ask", "Publishes a package."),
    CommandRule("sudo", "ask", "Runs as root.", grant=False),
    CommandRule("ssh", "ask", "Opens a remote shell.", grant=False),
    CommandRule("scp", "ask", "Copies files to or from a remote host.", grant=False),
    CommandRule("rsync", "ask", "Syncs files to or from a remote host.", grant=False),
    CommandRule("docker", "ask", "Controls containers.", grant=False),
    CommandRule("kubectl", "ask", "Controls a cluster.", grant=False),
)

IRREVERSIBLE_POSIX: tuple[CommandRule, ...] = (
    CommandRule("rm", "ask", "Deletes a directory tree.", flags=("-r",)),
    CommandRule("rm", "ask", "Deletes a directory tree.", flags=("-R",)),
    CommandRule("rm", "ask", "Deletes a directory tree.", flags=("--recursive",)),
)

IRREVERSIBLE_CMD: tuple[CommandRule, ...] = (
    CommandRule("del", "ask", "Deletes files recursively.", flags=("/s",)),
    CommandRule("rd", "ask", "Deletes a directory tree.", flags=("/s",)),
    CommandRule("rmdir", "ask", "Deletes a directory tree.", flags=("/s",)),
    CommandRule("runas", "ask", "Runs as another user.", grant=False),
    CommandRule("net user", "ask", "Changes user accounts.", grant=False),
    CommandRule("reg", "ask", "Edits the registry.", grant=False),
)

IRREVERSIBLE: tuple[CommandRule, ...] = IRREVERSIBLE_COMMON + (
    IRREVERSIBLE_CMD if _NT else IRREVERSIBLE_POSIX
)
"""Commands that are hard to undo or leave the workspace: asked in ``coding``."""

# ---------------------------------------------------------------------------
# The baseline: in force under every preset, merged most-restrictive with the
# normal rules, so it can only tighten — and never granted away.
# ---------------------------------------------------------------------------

_FORCE_PUSH = "Force-pushes rewrite remote history."
_ROOT_RM = "Deletes the filesystem root or the home directory."

DANGEROUS_COMMANDS_COMMON: tuple[CommandRule, ...] = (
    CommandRule("git push", "deny", _FORCE_PUSH, flags=("--force",)),
    CommandRule("git push", "deny", _FORCE_PUSH, flags=("-f",)),
    CommandRule("mkfs", "deny", "Formats a disk."),
    CommandRule("shutdown", "deny", "Shuts the host down."),
    CommandRule("reboot", "deny", "Reboots the host."),
)

# Spelled out rather than parsed: the rule says exactly which spellings it
# stops. ``rm -- -rf /`` or ``rm -rf "/"`` are not among them, by design.
DANGEROUS_COMMANDS_POSIX: tuple[CommandRule, ...] = tuple(
    CommandRule(f"rm {flags} {target}", "deny", _ROOT_RM)
    for flags in ("-rf", "-fr", "-r -f", "-f -r")
    for target in ("/", "/*", "~", "~/")
)

DANGEROUS_COMMANDS_CMD: tuple[CommandRule, ...] = (
    CommandRule("format", "deny", "Formats a disk."),
    CommandRule("diskpart", "deny", "Repartitions disks."),
    CommandRule("rd /s /q C:\\", "deny", "Deletes the system drive."),
    CommandRule("rd /s /q \\", "deny", "Deletes the current drive root."),
)

DANGEROUS_COMMANDS: tuple[CommandRule, ...] = DANGEROUS_COMMANDS_COMMON + (
    DANGEROUS_COMMANDS_CMD if _NT else DANGEROUS_COMMANDS_POSIX
)
"""Commands with no legitimate agent use: denied under every preset."""

CREDENTIAL_PATHS: tuple[PathRule, ...] = tuple(
    PathRule(p, "deny", reason="Credential store.")
    for p in (
        "~/.ssh",
        "~/.aws",
        "~/.gnupg",
        "~/.netrc",
        "~/.config/gh",
        "*.pem",
        "id_rsa*",
    )
)
"""Credential stores: neither read nor written under any preset."""

PROTECTED_CONFIG: tuple[PathRule, ...] = tuple(
    PathRule(
        p,
        "ask",
        ops=frozenset({"write"}),
        grant=False,
        reason=f"Writes to {p} change how tools behave.",
    )
    for p in (".git", ".github/workflows", ".vscode", ".husky", ".lovia")
)
"""Repository and tool configuration: every write asks, none is remembered."""

ENV_FILES: tuple[PathRule, ...] = (
    PathRule(
        "**/.env*",
        "ask",
        ops=frozenset({"read"}),
        reason="Environment files usually hold secrets.",
    ),
)
"""Reading a ``.env`` inside the workspace asks (and bulk walks skip it).
Part of the baseline like every other protection, so it can only tighten:
reads only, workspace-relative (``**/`` never reaches outside the root).
To let an agent read them: ``baseline=DANGEROUS_COMMANDS + CREDENTIAL_PATHS
+ PROTECTED_CONFIG``."""

DEFAULT_BASELINE: tuple[PathRule | CommandRule, ...] = (
    DANGEROUS_COMMANDS + CREDENTIAL_PATHS + PROTECTED_CONFIG + ENV_FILES
)
"""What ``WorkspacePolicy.baseline`` holds unless told otherwise."""

__all__ = [
    "CREDENTIAL_PATHS",
    "DANGEROUS_COMMANDS",
    "DANGEROUS_COMMANDS_CMD",
    "DANGEROUS_COMMANDS_COMMON",
    "DANGEROUS_COMMANDS_POSIX",
    "DEFAULT_BASELINE",
    "DEV_TOOLS",
    "ENV_FILES",
    "IRREVERSIBLE",
    "IRREVERSIBLE_CMD",
    "IRREVERSIBLE_COMMON",
    "IRREVERSIBLE_POSIX",
    "PROTECTED_CONFIG",
    "READ_ONLY",
    "READ_ONLY_CMD",
    "READ_ONLY_COMMON",
    "READ_ONLY_POSIX",
]
