"""WorkspacePolicy decision tables."""

from __future__ import annotations

from pathlib import Path

import pytest

from lovia.workspace import CommandRule, PathRule, Verdict, WorkspacePolicy
from lovia.workspace.policy import _path_matches, merge_verdicts


# ---------------------------------------------------------------------------
# Command decisions
# ---------------------------------------------------------------------------


def test_default_decision_applies_without_rules() -> None:
    assert WorkspacePolicy(shell_default="ask").decide_command("ls").action == "ask"
    assert WorkspacePolicy(shell_default="allow").decide_command("ls").action == "allow"


def test_allow_shell_false_denies_everything() -> None:
    policy = WorkspacePolicy(allow_shell=False, shell_default="allow")
    assert policy.decide_command("echo hi").action == "deny"


def test_rule_prefix_matches_on_word_boundary() -> None:
    policy = WorkspacePolicy(
        shell_default="ask",
        command_rules=(CommandRule("git push", "deny"), CommandRule("git", "allow")),
    )
    assert policy.decide_command("git status").action == "allow"
    assert policy.decide_command("git push origin main").action == "deny"
    # "git pushx" is not "git push" — falls through to the "git" rule.
    assert policy.decide_command("git pushx").action == "allow"
    assert policy.decide_command("gitx status").action == "ask"


def test_first_matching_rule_wins() -> None:
    policy = WorkspacePolicy(
        command_rules=(CommandRule("rm", "deny"), CommandRule("rm -i", "allow")),
    )
    assert policy.decide_command("rm -i file").action == "deny"


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
    assert policy.decide_command(command).action == "deny"


def test_compound_of_allowed_segments_is_allowed() -> None:
    policy = WorkspacePolicy(
        shell_default="deny",
        command_rules=(CommandRule("echo", "allow"), CommandRule("ls", "allow")),
    )
    assert policy.decide_command("echo hi && ls -la").action == "allow"


def test_unmatched_segment_falls_back_to_default() -> None:
    policy = WorkspacePolicy(
        shell_default="ask", command_rules=(CommandRule("echo", "allow"),)
    )
    assert policy.decide_command("echo hi; curl example.com").action == "ask"


def test_empty_command_uses_default() -> None:
    assert WorkspacePolicy(shell_default="ask").decide_command("   ").action == "ask"


def test_fd_redirections_do_not_split_segments() -> None:
    # `2>&1` / `>&2` / `&>` belong to one segment: without the splitter's
    # lookarounds the stray `1` would drop to shell_default and an allowed
    # `git status 2>&1` would suddenly ask.
    policy = WorkspacePolicy(
        shell_default="ask", command_rules=(CommandRule("git", "allow"),)
    )
    assert policy.decide_command("git status 2>&1").action == "allow"
    assert policy.decide_command("git status >&2").action == "allow"
    assert policy.decide_command("git status &> out.log").action == "allow"
    # A real background `&` still separates segments.
    assert policy.decide_command("git status & curl x").action == "ask"


# ---------------------------------------------------------------------------
# Verdicts: reasons and grant scopes
# ---------------------------------------------------------------------------


def test_ask_rule_verdict_carries_reason_and_pattern_scope() -> None:
    policy = WorkspacePolicy(
        shell_default="allow",
        command_rules=(
            CommandRule("git push", "ask", "Pushes to a remote; not undoable."),
            CommandRule("sudo", "ask", grant=False),
            CommandRule("docker", "ask"),
        ),
    )
    pushed = policy.decide_command("git push origin main")
    assert pushed == Verdict(
        "ask", "Pushes to a remote; not undoable.", "shell:git push"
    )
    # grant=False: still asks, but offers nothing to remember.
    assert policy.decide_command("sudo ls") == Verdict(
        "ask", "'sudo' requires approval", None
    )
    # No author reason: a generic one is derived from the pattern.
    assert policy.decide_command("docker ps").reason == "'docker' requires approval"


def test_deny_rule_verdict_carries_reason_and_no_scope() -> None:
    policy = WorkspacePolicy(command_rules=(CommandRule("rm -rf", "deny"),))
    verdict = policy.decide_command("rm -rf /")
    assert verdict.action == "deny" and verdict.scope is None
    assert verdict.reason == "'rm -rf' is denied by workspace policy"


def test_gray_command_asks_without_scope() -> None:
    verdict = WorkspacePolicy(shell_default="ask").decide_command("make deploy")
    assert verdict == Verdict("ask", "no workspace rule allows this command", None)


def test_compound_scope_survives_only_a_single_ask() -> None:
    policy = WorkspacePolicy(
        shell_default="allow",
        command_rules=(
            CommandRule("git push", "ask"),
            CommandRule("npm publish", "ask"),
        ),
    )
    # One ask among allows: its scope is the grant on offer.
    assert policy.decide_command("git push && echo ok").scope == "shell:git push"
    # Two asks: granting one must not cover the other -> nothing to remember.
    merged = policy.decide_command("git push && npm publish")
    assert merged.action == "ask" and merged.scope is None


def test_command_decider_may_return_a_verdict() -> None:
    policy = WorkspacePolicy(
        shell_default="allow",
        command_decider=lambda seg: (
            Verdict("ask", "custom", "shell:x") if seg.startswith("x") else None
        ),
    )
    assert policy.decide_command("x run") == Verdict("ask", "custom", "shell:x")
    assert policy.decide_command("y run").action == "allow"


def test_path_rule_ask_offers_its_pattern_as_scope() -> None:
    policy = WorkspacePolicy(
        path_rules=(
            PathRule(".env*", "ask", reason="Environment files usually hold secrets."),
            PathRule(".git", "ask", ops={"write"}, grant=False),
        )
    )
    env = policy.decide_path(rel=".env", abs_posix="/ws/.env", op="read")
    assert env == Verdict(
        "ask", "Environment files usually hold secrets.", "read:.env*"
    )
    git = policy.decide_path(rel=".git/config", abs_posix="/ws/.git/config", op="write")
    assert git.action == "ask" and git.scope is None


def test_outside_default_ask_offers_the_directory_as_scope() -> None:
    policy = WorkspacePolicy(read_outside="ask")
    verdict = policy.decide_path(rel=None, abs_posix="/etc/hosts", op="read")
    assert verdict.action == "ask" and verdict.scope == "read:/etc"
    assert "outside the workspace root" in verdict.reason
    # The home directory collapses to ~ in both the key and the reason.
    home = Path.home().resolve().as_posix()
    verdict = policy.decide_path(rel=None, abs_posix=f"{home}/notes/a.md", op="read")
    assert verdict.scope == "read:~/notes" and "~/notes" in verdict.reason


def test_inside_write_ask_offers_the_relative_directory() -> None:
    policy = WorkspacePolicy(write="ask")
    top = policy.decide_path(rel="notes.txt", abs_posix="/ws/notes.txt", op="write")
    nested = policy.decide_path(
        rel="src/app.py", abs_posix="/ws/src/app.py", op="write"
    )
    assert top.scope == "write:." and nested.scope == "write:src"
    assert top.reason == "writes inside the workspace require approval"


def test_denied_path_verdict_names_the_pattern() -> None:
    policy = WorkspacePolicy(denied_paths=(".env*",))
    verdict = policy.decide_path(rel=".env", abs_posix="/ws/.env", op="read")
    assert verdict.action == "deny" and "'.env*'" in verdict.reason


# ---------------------------------------------------------------------------
# flags=, wrappers, the baseline and the bundles
# ---------------------------------------------------------------------------


def test_flags_must_appear_anywhere_after_the_prefix() -> None:
    policy = WorkspacePolicy(
        shell_default="allow",
        baseline=(),
        command_rules=(
            CommandRule("git push", "deny", flags=("--force",)),
            CommandRule("git push", "deny", flags=("-f",)),
        ),
    )
    assert policy.decide_command("git push --force origin main").action == "deny"
    assert policy.decide_command("git push origin main --force").action == "deny"
    assert policy.decide_command("git push -fu origin").action == "deny"  # -f in -fu
    assert policy.decide_command("git push --force-with-lease").action == "allow"
    assert policy.decide_command("git push origin -- --force").action == "allow"
    assert policy.decide_command("git pushx --force").action == "allow"


def test_flags_are_flag_shaped_and_positional_words_stay_in_the_prefix() -> None:
    with pytest.raises(ValueError):
        CommandRule("python", "allow", flags=("pytest",))
    # The prefix form cannot be fooled by a different positional argument.
    policy = WorkspacePolicy(
        shell_default="ask",
        baseline=(),
        command_rules=(CommandRule("python -m pytest", "allow"),),
    )
    assert policy.decide_command("python -m pytest tests/").action == "allow"
    assert policy.decide_command("python -m pip install pytest").action == "ask"


def test_rule_key_keeps_two_flag_rules_apart_as_grant_scopes() -> None:
    policy = WorkspacePolicy(
        shell_default="allow",
        baseline=(),
        command_rules=(
            CommandRule("git push", "ask", flags=("--tags",)),
            CommandRule("git push", "ask", flags=("--delete",)),
        ),
    )
    assert policy.decide_command("git push --tags").scope == "shell:git push --tags"
    assert (
        policy.decide_command("git push --delete x").scope == "shell:git push --delete"
    )


def test_wrapped_commands_are_at_most_asked_and_never_grantable() -> None:
    policy = WorkspacePolicy(
        shell_default="allow",
        baseline=(CommandRule("git push", "deny", flags=("--force",)),),
        command_rules=(
            CommandRule("git status", "allow"),
            CommandRule("git push", "ask"),
        ),
    )
    for wrapped in (
        "FOO=1 git status",
        "env git status",
        "nice git status",
        "timeout 5 git status",
    ):
        verdict = policy.decide_command(wrapped)
        assert verdict.action == "ask" and "wraps the command" in verdict.reason, (
            wrapped
        )
    # An ask stays an ask but loses its scope: the wrapper is what changes
    # what the inner command runs.
    assert policy.decide_command("FOO=1 git push").scope is None
    # A deny still bites through the wrapper.
    assert policy.decide_command("env git push --force").action == "deny"
    # env with options is not a thin wrapper: the whole segment is gray.
    assert policy.decide_command("env -i git status").action == "allow"  # shell_default


def test_baseline_only_tightens() -> None:
    base = (
        CommandRule("rm -rf /", "deny"),
        PathRule(".env*", "ask"),
        PathRule(".git", "ask", ops=("write",)),
    )
    # A user allow rule, a permissive default and a decider cannot lift it.
    policy = WorkspacePolicy(
        shell_default="allow",
        baseline=base,
        command_rules=(CommandRule("rm", "allow"),),
        command_decider=lambda seg: "allow",
    )
    assert policy.decide_command("rm -rf /").action == "deny"
    assert policy.decide_command("rm -rf build").action == "allow"
    # A user deny is not loosened by a baseline ask.
    strict = WorkspacePolicy(baseline=base, denied_paths=(".env*",))
    assert (
        strict.decide_path(rel=".env", abs_posix="/ws/.env", op="read").action == "deny"
    )
    # readonly's write deny beats the baseline's write ask.
    ro = WorkspacePolicy.readonly(baseline=base)
    assert (
        ro.decide_path(rel=".git/x", abs_posix="/ws/.git/x", op="write").action
        == "deny"
    )
    # Off, or replaced.
    off = WorkspacePolicy(shell_default="allow", baseline=())
    assert off.decide_command("rm -rf /").action == "allow"


def test_baseline_ties_keep_the_users_reason() -> None:
    policy = WorkspacePolicy(command_rules=(CommandRule("rm -rf", "deny", "nope"),))
    assert policy.decide_command("rm -rf /").reason == "nope"


def test_default_baseline_is_in_force_in_every_preset() -> None:
    from pathlib import Path as _P

    ssh = (_P.home() / ".ssh" / "id_rsa").resolve().as_posix()
    for policy in (
        WorkspacePolicy(),
        WorkspacePolicy.readonly(),
        WorkspacePolicy.coding(),
        WorkspacePolicy.trusted(),
    ):
        assert policy.decide_path(rel=None, abs_posix=ssh, op="read").action == "deny"
        git = policy.decide_path(
            rel=".git/config", abs_posix="/ws/.git/config", op="write"
        )
        assert git.action in ("ask", "deny") and git.scope is None
    for policy in (WorkspacePolicy.coding(), WorkspacePolicy.trusted()):
        assert policy.decide_command("git push origin main --force").action == "deny"
        assert policy.decide_command("mkfs /dev/sda").action == "deny"
    # The rule is a word prefix: ``mkfs.ext4`` is another word, and falls to
    # the preset's default (ask under coding, allow under trusted) — the
    # baseline stops the spellings it names, no more.
    assert WorkspacePolicy.coding().decide_command("mkfs.ext4 /dev/sda").action == "ask"


def test_coding_preset_composes_the_bundles_after_user_rules() -> None:
    policy = WorkspacePolicy.coding(command_rules=(CommandRule("pytest", "ask"),))
    assert policy.decide_command("pytest -q").action == "ask"  # user first
    assert policy.decide_command("ruff check .").action == "allow"  # DEV_TOOLS
    assert policy.decide_command("git status").action == "allow"  # READ_ONLY
    pushed = policy.decide_command("git push origin main")
    assert pushed.action == "ask" and pushed.scope == "shell:git push"  # IRREVERSIBLE
    assert policy.decide_command("sudo ls").scope is None  # grant=False
    assert policy.decide_command("make deploy").action == "ask"  # gray
    assert WorkspacePolicy.coding(shell_default="allow").decide_command(
        "make deploy"
    ).action == ("allow")
    # .env* asks at the preset level, and a user path rule lifts it.
    assert (
        policy.decide_path(rel=".env", abs_posix="/ws/.env", op="read").scope
        == "read:.env*"
    )
    lifted = WorkspacePolicy.coding(path_rules=(PathRule(".env*", "allow"),))
    assert (
        lifted.decide_path(rel=".env", abs_posix="/ws/.env", op="read").action
        == "allow"
    )


def test_env_files_rule_gates_reads_only() -> None:
    # The preset rule is about reading secrets; writes keep following the
    # mode, so it must not lift readonly's deny (or coding's outside deny)
    # to an ask that a direct session call would then let through.
    ro = WorkspacePolicy.readonly()
    assert ro.decide_path(rel=".env", abs_posix="/ws/.env", op="write").action == "deny"
    assert ro.decide_path(rel=".env", abs_posix="/ws/.env", op="read").action == "ask"
    coding = WorkspacePolicy.coding()
    assert (
        coding.decide_path(rel=None, abs_posix="/srv/app/.env", op="write").action
        == "deny"
    )
    assert coding.decide_path(rel=".env", abs_posix="/ws/.env", op="write").action == (
        "allow"
    )


def test_trusted_preset_interrupts_only_for_the_baseline() -> None:
    policy = WorkspacePolicy.trusted()
    assert policy.decide_command("git push origin main").action == "allow"
    assert policy.decide_command("sudo ls").action == "allow"
    assert policy.decide_command("git push --force").action == "deny"


def test_bundles_exist_for_both_host_shells() -> None:
    from lovia.workspace import rules

    assert any(r.pattern == "ls" for r in rules.READ_ONLY_POSIX)
    assert any(r.pattern == "dir" for r in rules.READ_ONLY_CMD)
    assert any(r.pattern == "rd" and r.flags == ("/s",) for r in rules.IRREVERSIBLE_CMD)
    assert any(r.pattern == "format" for r in rules.DANGEROUS_COMMANDS_CMD)
    assert all(r.action == "deny" for r in rules.CREDENTIAL_PATHS)
    assert all(r.grant is False and r.ops == {"write"} for r in rules.PROTECTED_CONFIG)


def test_rule_bound_tells_rule_asks_from_default_asks() -> None:
    policy = WorkspacePolicy.coding()
    assert policy.rule_bound(rel=".env", abs_posix="/ws/.env", op="read")
    assert not policy.rule_bound(rel=None, abs_posix="/srv/data/a.txt", op="read")


# ---------------------------------------------------------------------------
# Path decisions (the ACL)
# ---------------------------------------------------------------------------


def test_inside_root_reads_always_allowed_writes_follow_write() -> None:
    policy = WorkspacePolicy(write="deny")
    assert (
        policy.decide_path(rel="notes.txt", abs_posix="/ws/notes.txt", op="read").action
        == "allow"
    )
    assert (
        policy.decide_path(
            rel="notes.txt", abs_posix="/ws/notes.txt", op="write"
        ).action
        == "deny"
    )


def test_outside_root_follows_outside_defaults() -> None:
    policy = WorkspacePolicy(read_outside="ask", write_outside="deny")
    assert (
        policy.decide_path(rel=None, abs_posix="/etc/hosts", op="read").action == "ask"
    )
    assert (
        policy.decide_path(rel=None, abs_posix="/etc/hosts", op="write").action
        == "deny"
    )


def test_denied_paths_win_over_everything() -> None:
    policy = WorkspacePolicy(
        denied_paths=(".env*", "secrets/**"),
        path_rules=(PathRule(".env", "allow"),),  # denied still wins
    )
    assert (
        policy.decide_path(rel=".env", abs_posix="/ws/.env", op="read").action == "deny"
    )
    assert (
        policy.decide_path(
            rel=".env.local", abs_posix="/ws/.env.local", op="read"
        ).action
        == "deny"
    )
    assert (
        policy.decide_path(
            rel="secrets/prod/key.pem", abs_posix="/ws/secrets/prod/key.pem", op="write"
        ).action
        == "deny"
    )
    assert (
        policy.decide_path(
            rel="src/app.py", abs_posix="/ws/src/app.py", op="write"
        ).action
        == "allow"
    )


def test_denied_paths_match_nested_files_and_directories() -> None:
    policy = WorkspacePolicy(denied_paths=(".env*", "secrets"))
    # A bare glob catches the dotfile at any depth, not just at the root.
    assert (
        policy.decide_path(
            rel="config/.env.local", abs_posix="/ws/config/.env.local", op="read"
        ).action
        == "deny"
    )
    # A bare directory name denies the dir and everything beneath it, anywhere.
    assert (
        policy.decide_path(
            rel="secrets/prod/key.pem", abs_posix="/ws/secrets/prod/key.pem", op="read"
        ).action
        == "deny"
    )
    assert (
        policy.decide_path(
            rel="vendor/secrets/key", abs_posix="/ws/vendor/secrets/key", op="read"
        ).action
        == "deny"
    )
    # Unrelated paths stay allowed.
    assert (
        policy.decide_path(
            rel="src/app.py", abs_posix="/ws/src/app.py", op="read"
        ).action
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
        policy.decide_path(rel=None, abs_posix="/opt/data/f.csv", op="read").action
        == "allow"
    )
    # The read-only rule does not cover writes; the broader deny does.
    assert (
        policy.decide_path(rel=None, abs_posix="/opt/data/f.csv", op="write").action
        == "deny"
    )
    assert (
        policy.decide_path(rel=None, abs_posix="/opt/other", op="read").action == "deny"
    )


def test_absolute_pattern_covers_subtree() -> None:
    policy = WorkspacePolicy(path_rules=(PathRule("/srv/share", "allow"),))
    assert (
        policy.decide_path(rel=None, abs_posix="/srv/share/a/b.txt", op="read").action
        == "allow"
    )
    assert (
        policy.decide_path(rel=None, abs_posix="/srv/shared", op="read").action
        == "deny"
    )


def test_denied_paths_accept_absolute_patterns() -> None:
    policy = WorkspacePolicy(read_outside="allow", denied_paths=("/etc/ssl/**",))
    # abs_posix is always resolved in practice (the session resolves before
    # deciding); the pattern is canonicalized to match, so a symlinked prefix
    # like macOS /etc -> /private/etc does not defeat the rule.
    denied = Path("/etc/ssl/private/key").resolve().as_posix()
    allowed = Path("/etc/hosts").resolve().as_posix()
    assert policy.decide_path(rel=None, abs_posix=denied, op="read").action == "deny"
    assert policy.decide_path(rel=None, abs_posix=allowed, op="read").action == "allow"


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
    assert policy.decide_path(rel=None, abs_posix=resolved, op="read").action == "deny"


def test_bare_denied_patterns_match_outside_the_root_too() -> None:
    # With permissive outside reads (trusted), a bare pattern still names the
    # file anywhere — otherwise denied_paths=(".env*",) would be a no-op for
    # every path outside the workspace.
    policy = WorkspacePolicy(read_outside="allow", denied_paths=(".env*", "id_rsa"))
    assert (
        policy.decide_path(rel=None, abs_posix="/home/u/proj/.env", op="read").action
        == "deny"
    )
    assert (
        policy.decide_path(rel=None, abs_posix="/home/u/.ssh/id_rsa", op="read").action
        == "deny"
    )
    assert (
        policy.decide_path(rel=None, abs_posix="/home/u/notes.md", op="read").action
        == "allow"
    )
    # Workspace-relative patterns (containing "/") stay inside-only.
    scoped = WorkspacePolicy(read_outside="allow", denied_paths=("secrets/**",))
    assert (
        scoped.decide_path(rel=None, abs_posix="/srv/secrets/key", op="read").action
        == "allow"
    )
    assert (
        scoped.decide_path(
            rel="secrets/key", abs_posix="/ws/secrets/key", op="read"
        ).action
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
        coding.decide_path(
            rel=".git/config", abs_posix="/ws/.git/config", op="read"
        ).action
        == "deny"
    )

    trusted = WorkspacePolicy.trusted(command_rules=(CommandRule("rm", "deny"),))
    assert trusted.shell_default == "allow"
    assert trusted.read_outside == "allow" and trusted.write_outside == "ask"
    assert trusted.decide_command("rm -rf x").action == "deny"
    assert trusted.decide_command("echo ok").action == "allow"


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
    assert policy.decide_command("git status\nrm -rf /").action == "deny"


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
    assert policy.decide_command("danger --now").action == "deny"  # hook wins
    assert policy.decide_command("rm file").action == "ask"  # falls through to a rule
    assert (
        policy.decide_command("echo hi").action == "allow"
    )  # falls through to default
    # Most-restrictive-wins still composes with the hook across segments.
    assert policy.decide_command("echo hi && danger").action == "deny"


def test_command_decider_invalid_return_falls_through() -> None:
    # A hook returning a non-Decision must not crash; it falls through.
    policy = WorkspacePolicy(shell_default="ask", command_decider=lambda seg: "maybe")
    assert policy.decide_command("anything").action == "ask"


def test_merge_verdicts_most_restrictive_wins_and_keeps_its_reason() -> None:
    assert merge_verdicts() == Verdict("allow")
    assert merge_verdicts(Verdict("allow"), Verdict("ask", "why", "s")) == Verdict(
        "ask", "why", "s"
    )
    assert merge_verdicts(
        Verdict("ask", "a"), Verdict("deny", "no"), Verdict("allow")
    ) == Verdict("deny", "no")
    # Two asks: the scope is dropped and both reasons are kept.
    assert merge_verdicts(
        Verdict("ask", "a", "x"), Verdict("ask", "b", "y")
    ) == Verdict("ask", "a; b", None)


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
