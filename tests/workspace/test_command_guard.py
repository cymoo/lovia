"""Lexical path-claim extraction from shell commands."""

from __future__ import annotations

from lovia.workspace.command_guard import extract_path_claims


def test_redirect_targets_are_write_claims() -> None:
    assert ("write", "~/.bashrc") in extract_path_claims("echo pwned > ~/.bashrc")
    assert ("write", "out.log") in extract_path_claims("make >> out.log")
    assert ("write", "all.log") in extract_path_claims("cmd &> all.log")


def test_input_redirect_is_a_read_claim() -> None:
    assert ("read", "in.csv") in extract_path_claims("sort < in.csv")


def test_fd_duplications_are_not_claims() -> None:
    assert extract_path_claims("git status 2>&1") == []
    assert extract_path_claims("echo hi >&2") == []


def test_pseudo_devices_are_not_claims() -> None:
    # `2>/dev/null` is shell plumbing, not file access — under coding,
    # write_outside="deny" must not turn it into a hard deny.
    assert extract_path_claims("cmd 2>/dev/null") == []
    assert extract_path_claims("cmd > /dev/null 2>&1") == []
    assert extract_path_claims("cat /dev/stdin") == []
    assert extract_path_claims("sort < /dev/fd/63") == []
    # ...but other device nodes still count as claims.
    assert extract_path_claims("cat /dev/disk0") == [("read", "/dev/disk0")]


def test_key_value_tokens_yield_rooted_values() -> None:
    assert extract_path_claims("dd of=/dev/disk0") == [("read", "/dev/disk0")]
    assert extract_path_claims("FOO=/etc/x cmd") == [("read", "/etc/x")]
    # A key=value whose value is not rooted keeps the whole token (harmless
    # relative claim), never misparses sed-style expressions.
    assert extract_path_claims("sed 's/a=b/c/'") == [("read", "s/a=b/c/")]


def test_pathish_arguments_are_read_claims() -> None:
    assert extract_path_claims("cat .env") == [("read", ".env")]
    assert ("read", "/etc/hosts") in extract_path_claims("cat /etc/hosts | head")
    assert ("read", "src/") in extract_path_claims("grep -r pattern src/")
    assert ("read", "~") in extract_path_claims("ls ~")


def test_bare_words_are_not_claims() -> None:
    # Subcommand names must not be mistaken for paths, or a denied pattern
    # like "build" would break `npm run build`.
    assert extract_path_claims("npm run build") == []
    assert extract_path_claims("git commit") == []


def test_urls_and_plain_flags_are_skipped() -> None:
    assert extract_path_claims("curl https://example.com/a.json") == []
    assert extract_path_claims("cc -I/usr/include main.c") == [("read", "main.c")]


def test_flag_with_value_yields_the_value() -> None:
    assert extract_path_claims("cmd --out=/var/log/x.log") == [
        ("read", "/var/log/x.log")
    ]


def test_heredoc_and_herestring_targets_are_skipped() -> None:
    assert extract_path_claims("cat << EOF") == []
    assert extract_path_claims("cat <<< hello") == []


def test_quoted_paths_survive_tokenization() -> None:
    assert extract_path_claims("cat '/tmp/my file.txt'") == [
        ("read", "/tmp/my file.txt")
    ]


def test_unbalanced_quotes_fall_back_to_no_claims() -> None:
    assert extract_path_claims("echo 'unclosed") == []


def test_claims_found_inside_substitutions() -> None:
    # Bonus strictness: tokens inside $() are still inspected.
    claims = extract_path_claims("echo $(cat /etc/passwd)")
    assert ("read", "/etc/passwd") in claims


def test_relative_false_positives_are_harmless_by_construction() -> None:
    # sed expressions look like paths; they resolve inside the root where
    # reads are always allowed, so claiming them cannot escalate a decision.
    assert extract_path_claims("sed 's/foo/bar/' f.txt") == [
        ("read", "s/foo/bar/"),
        ("read", "f.txt"),
    ]
