"""WorkspacePolicy decision tables."""

from __future__ import annotations

import pytest

from lovia.workspace import CommandRule, PermissionDeniedError, WorkspacePolicy


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


# ---------------------------------------------------------------------------
# Path decisions
# ---------------------------------------------------------------------------


def test_check_path_denies_writes_when_readonly() -> None:
    policy = WorkspacePolicy.readonly()
    policy.check_path("notes.txt", write=False)
    with pytest.raises(PermissionDeniedError, match="read-only"):
        policy.check_path("notes.txt", write=True)


def test_denied_paths_block_reads_and_writes() -> None:
    policy = WorkspacePolicy(denied_paths=(".env*", "secrets/**"))
    policy.check_path("src/app.py", write=True)
    with pytest.raises(PermissionDeniedError):
        policy.check_path(".env", write=False)
    with pytest.raises(PermissionDeniedError):
        policy.check_path(".env.local", write=False)
    with pytest.raises(PermissionDeniedError):
        policy.check_path("secrets/prod/key.pem", write=False)


def test_presets() -> None:
    readonly = WorkspacePolicy.readonly()
    assert not readonly.allow_write and not readonly.allow_shell

    coding = WorkspacePolicy.coding(denied_paths=(".git/**",))
    assert coding.allow_write and coding.shell_default == "ask"
    assert coding.path_is_denied(".git/config")

    trusted = WorkspacePolicy.trusted(command_rules=(CommandRule("rm", "deny"),))
    assert trusted.shell_default == "allow"
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


def test_denied_paths_match_nested_files_and_directories() -> None:
    policy = WorkspacePolicy(denied_paths=(".env*", "secrets"))
    # A bare glob catches the dotfile at any depth, not just at the root.
    assert policy.path_is_denied("config/.env.local")
    with pytest.raises(PermissionDeniedError):
        policy.check_path("a/b/.env", write=False)
    # A bare directory name denies the dir and everything beneath it, anywhere.
    assert policy.path_is_denied("secrets")
    assert policy.path_is_denied("secrets/prod/key.pem")
    assert policy.path_is_denied("vendor/secrets/key")
    # Unrelated paths stay allowed.
    assert not policy.path_is_denied("src/app.py")


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
