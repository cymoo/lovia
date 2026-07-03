"""WorkspacePolicy decision tables."""

from __future__ import annotations

from pathlib import Path

import pytest

from lovia.workspace import CommandRule, PathRule, WorkspacePolicy
from lovia.workspace.policy import _path_matches, merge_decisions


# ---------------------------------------------------------------------------
# Command decisions
# ---------------------------------------------------------------------------


def test_default_decision_applies_without_rules() -> None:
    assert WorkspacePolicy(shell_default="ask").decide_command("ls") == "ask"
    assert WorkspacePolicy(shell_default="allow").decide_command("ls") == "allow"


def test_allow_shell_false_denies_everything() -> None:
    policy = WorkspacePolicy(allow_shell=False, shell_default="allow")
    assert policy.decide_command("echo hi") == "deny"


def test_rule_prefix_matches_on_word_boundary() -> None:
    policy = WorkspacePolicy(
        shell_default="ask",
        command_rules=(CommandRule("git push", "deny"), CommandRule("git", "allow")),
    )
    assert policy.decide_command("git status") == "allow"
    assert policy.decide_command("git push origin main") == "deny"
    # "git pushx" is not "git push" — falls through to the "git" rule.
    assert policy.decide_command("git pushx") == "allow"
    assert policy.decide_command("gitx status") == "ask"


def test_first_matching_rule_wins() -> None:
    policy = WorkspacePolicy(
        command_rules=(CommandRule("rm", "deny"), CommandRule("rm -i", "allow")),
    )
    assert policy.decide_command("rm -i file") == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "git status; rm -rf /",
        "git status && rm -rf /",
        "git status || rm -rf /",
        "git status | rm -rf /",
        "git status & rm -rf /",
    ],
)
def test_compound_commands_take_most_restrictive_decision(command: str) -> None:
    policy = WorkspacePolicy(
        shell_default="ask",
        command_rules=(CommandRule("git", "allow"), CommandRule("rm -rf", "deny")),
    )
    assert policy.decide_command(command) == "deny"


def test_compound_of_allowed_segments_is_allowed() -> None:
    policy = WorkspacePolicy(
        shell_default="deny",
        command_rules=(CommandRule("echo", "allow"), CommandRule("ls", "allow")),
    )
    assert policy.decide_command("echo hi && ls -la") == "allow"


def test_unmatched_segment_falls_back_to_default() -> None:
    policy = WorkspacePolicy(
        shell_default="ask", command_rules=(CommandRule("echo", "allow"),)
    )
    assert policy.decide_command("echo hi; curl example.com") == "ask"


def test_empty_command_uses_default() -> None:
    assert WorkspacePolicy(shell_default="ask").decide_command("   ") == "ask"


def test_fd_redirections_do_not_split_segments() -> None:
    # `2>&1` / `>&2` / `&>` belong to one segment: without the splitter's
    # lookarounds the stray `1` would drop to shell_default and an allowed
    # `git status 2>&1` would suddenly ask.
    policy = WorkspacePolicy(
        shell_default="ask", command_rules=(CommandRule("git", "allow"),)
    )
    assert policy.decide_command("git status 2>&1") == "allow"
    assert policy.decide_command("git status >&2") == "allow"
    assert policy.decide_command("git status &> out.log") == "allow"
    # A real background `&` still separates segments.
    assert policy.decide_command("git status & curl x") == "ask"


# ---------------------------------------------------------------------------
# Path decisions (the ACL)
# ---------------------------------------------------------------------------


def test_inside_root_reads_always_allowed_writes_follow_write() -> None:
    policy = WorkspacePolicy(write="deny")
    assert (
        policy.decide_path(rel="notes.txt", abs_posix="/ws/notes.txt", op="read")
        == "allow"
    )
    assert (
        policy.decide_path(rel="notes.txt", abs_posix="/ws/notes.txt", op="write")
        == "deny"
    )


def test_outside_root_follows_outside_defaults() -> None:
    policy = WorkspacePolicy(read_outside="ask", write_outside="deny")
    assert policy.decide_path(rel=None, abs_posix="/etc/hosts", op="read") == "ask"
    assert policy.decide_path(rel=None, abs_posix="/etc/hosts", op="write") == "deny"


def test_denied_paths_win_over_everything() -> None:
    policy = WorkspacePolicy(
        denied_paths=(".env*", "secrets/**"),
        path_rules=(PathRule(".env", "allow"),),  # denied still wins
    )
    assert policy.decide_path(rel=".env", abs_posix="/ws/.env", op="read") == "deny"
    assert (
        policy.decide_path(rel=".env.local", abs_posix="/ws/.env.local", op="read")
        == "deny"
    )
    assert (
        policy.decide_path(
            rel="secrets/prod/key.pem", abs_posix="/ws/secrets/prod/key.pem", op="write"
        )
        == "deny"
    )
    assert (
        policy.decide_path(rel="src/app.py", abs_posix="/ws/src/app.py", op="write")
        == "allow"
    )


def test_denied_paths_match_nested_files_and_directories() -> None:
    policy = WorkspacePolicy(denied_paths=(".env*", "secrets"))
    # A bare glob catches the dotfile at any depth, not just at the root.
    assert (
        policy.decide_path(
            rel="config/.env.local", abs_posix="/ws/config/.env.local", op="read"
        )
        == "deny"
    )
    # A bare directory name denies the dir and everything beneath it, anywhere.
    assert (
        policy.decide_path(
            rel="secrets/prod/key.pem", abs_posix="/ws/secrets/prod/key.pem", op="read"
        )
        == "deny"
    )
    assert (
        policy.decide_path(
            rel="vendor/secrets/key", abs_posix="/ws/vendor/secrets/key", op="read"
        )
        == "deny"
    )
    # Unrelated paths stay allowed.
    assert (
        policy.decide_path(rel="src/app.py", abs_posix="/ws/src/app.py", op="read")
        == "allow"
    )


def test_path_rules_first_match_wins_and_respects_ops() -> None:
    policy = WorkspacePolicy(
        read_outside="deny",
        path_rules=(
            PathRule("/opt/data", "allow", ops=("read",)),
            PathRule("/opt", "deny"),
        ),
    )
    assert (
        policy.decide_path(rel=None, abs_posix="/opt/data/f.csv", op="read") == "allow"
    )
    # The read-only rule does not cover writes; the broader deny does.
    assert (
        policy.decide_path(rel=None, abs_posix="/opt/data/f.csv", op="write") == "deny"
    )
    assert policy.decide_path(rel=None, abs_posix="/opt/other", op="read") == "deny"


def test_absolute_pattern_covers_subtree() -> None:
    policy = WorkspacePolicy(path_rules=(PathRule("/srv/share", "allow"),))
    assert (
        policy.decide_path(rel=None, abs_posix="/srv/share/a/b.txt", op="read")
        == "allow"
    )
    assert policy.decide_path(rel=None, abs_posix="/srv/shared", op="read") == "deny"


def test_denied_paths_accept_absolute_patterns() -> None:
    policy = WorkspacePolicy(read_outside="allow", denied_paths=("/etc/ssl/**",))
    # abs_posix is always resolved in practice (the session resolves before
    # deciding); the pattern is canonicalized to match, so a symlinked prefix
    # like macOS /etc -> /private/etc does not defeat the rule.
    denied = Path("/etc/ssl/private/key").resolve().as_posix()
    allowed = Path("/etc/hosts").resolve().as_posix()
    assert policy.decide_path(rel=None, abs_posix=denied, op="read") == "deny"
    assert policy.decide_path(rel=None, abs_posix=allowed, op="read") == "allow"


def test_absolute_pattern_matches_through_symlinked_prefix(tmp_path) -> None:
    # decide_path compares against a *resolved* absolute path; the pattern
    # must be resolved too, or a symlinked prefix silently defeats it
    # (the macOS /etc -> /private/etc class of bug).
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)  # /…/link -> /…/real
    policy = WorkspacePolicy(read_outside="allow", denied_paths=(f"{link}/**",))
    # A path reached via the real location must still be denied by the
    # link-spelled pattern.
    resolved = (real / "secret.key").resolve().as_posix()
    assert policy.decide_path(rel=None, abs_posix=resolved, op="read") == "deny"


def test_bare_denied_patterns_match_outside_the_root_too() -> None:
    # With permissive outside reads (trusted), a bare pattern still names the
    # file anywhere — otherwise denied_paths=(".env*",) would be a no-op for
    # every path outside the workspace.
    policy = WorkspacePolicy(read_outside="allow", denied_paths=(".env*", "id_rsa"))
    assert (
        policy.decide_path(rel=None, abs_posix="/home/u/proj/.env", op="read") == "deny"
    )
    assert (
        policy.decide_path(rel=None, abs_posix="/home/u/.ssh/id_rsa", op="read")
        == "deny"
    )
    assert (
        policy.decide_path(rel=None, abs_posix="/home/u/notes.md", op="read") == "allow"
    )
    # Workspace-relative patterns (containing "/") stay inside-only.
    scoped = WorkspacePolicy(read_outside="allow", denied_paths=("secrets/**",))
    assert (
        scoped.decide_path(rel=None, abs_posix="/srv/secrets/key", op="read") == "allow"
    )
    assert (
        scoped.decide_path(rel="secrets/key", abs_posix="/ws/secrets/key", op="read")
        == "deny"
    )


def test_presets() -> None:
    readonly = WorkspacePolicy.readonly()
    assert readonly.write == "deny" and not readonly.allow_shell
    assert readonly.read_outside == "deny"

    coding = WorkspacePolicy.coding(denied_paths=(".git/**",))
    assert coding.write == "allow" and coding.shell_default == "ask"
    assert coding.read_outside == "ask" and coding.write_outside == "deny"
    assert (
        coding.decide_path(rel=".git/config", abs_posix="/ws/.git/config", op="read")
        == "deny"
    )

    trusted = WorkspacePolicy.trusted(command_rules=(CommandRule("rm", "deny"),))
    assert trusted.shell_default == "allow"
    assert trusted.read_outside == "allow" and trusted.write_outside == "ask"
    assert trusted.decide_command("rm -rf x") == "deny"
    assert trusted.decide_command("echo ok") == "allow"


# ---------------------------------------------------------------------------
# Correctness fixes + the command_decider hook
# ---------------------------------------------------------------------------


def test_newline_is_a_command_separator() -> None:
    # A newline splits segments too, so an allowed first line can't shelter a
    # denied second line (`git status\nrm -rf /`).
    policy = WorkspacePolicy(
        shell_default="ask",
        command_rules=(CommandRule("git", "allow"), CommandRule("rm -rf", "deny")),
    )
    assert policy.decide_command("git status\nrm -rf /") == "deny"


def test_command_decider_overrides_then_falls_through() -> None:
    def decider(segment: str):
        if segment.startswith("danger"):
            return "deny"
        return None  # fall through to static rules / default

    policy = WorkspacePolicy(
        shell_default="allow",
        command_rules=(CommandRule("rm", "ask"),),
        command_decider=decider,
    )
    assert policy.decide_command("danger --now") == "deny"  # hook wins
    assert policy.decide_command("rm file") == "ask"  # falls through to a rule
    assert policy.decide_command("echo hi") == "allow"  # falls through to default
    # Most-restrictive-wins still composes with the hook across segments.
    assert policy.decide_command("echo hi && danger") == "deny"


def test_command_decider_invalid_return_falls_through() -> None:
    # A hook returning a non-Decision must not crash; it falls through.
    policy = WorkspacePolicy(shell_default="ask", command_decider=lambda seg: "maybe")
    assert policy.decide_command("anything") == "ask"


def test_merge_decisions_most_restrictive_wins() -> None:
    assert merge_decisions() == "allow"
    assert merge_decisions("allow", "ask") == "ask"
    assert merge_decisions("ask", "deny", "allow") == "deny"


def test_path_rule_ops_accepts_any_iterable() -> None:
    rule = PathRule("~/x", "allow", ops=("read",))
    assert rule.ops == frozenset({"read"})


# ---------------------------------------------------------------------------
# _path_matches helper
# ---------------------------------------------------------------------------


def test_path_matches_bare_pattern_hits_any_segment() -> None:
    assert _path_matches("sub/.env", ".env*")
    assert _path_matches("a/secrets/b", "secrets")
    assert not _path_matches("src/app.py", ".env*")


def test_path_matches_slash_pattern_covers_subtree() -> None:
    assert _path_matches("build", "build")
    assert _path_matches("build/out.js", "build/*")


def test_path_matches_empty_pattern_never_matches() -> None:
    # A pattern that is only slashes normalizes to "" and must match nothing
    # (rather than matching every path).
    assert _path_matches("anything/at/all", "/") is False
    assert _path_matches("anything", "") is False
