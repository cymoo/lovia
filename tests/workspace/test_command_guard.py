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
    assert extract_path_claims("echo hi >&2") == [("read", "hi")]
    assert extract_path_claims("cmd <&0") == []
    # ...but `>123` writes a file called 123 (which may be a symlink to
    # anything): only after `>&` / `<&` is a number a descriptor.
    assert extract_path_claims("echo x >123") == [("read", "x"), ("write", "123")]
    assert extract_path_claims("echo x > 123") == [("read", "x"), ("write", "123")]


def test_a_number_before_a_redirect_is_claimed_as_a_read() -> None:
    # The lexer drops the spacing that tells `2>&1` from `cat 123 > out`, so
    # the number is claimed either way: `2` resolves inside the root and
    # changes nothing; `123` may be a symlink to a denied file.
    assert extract_path_claims("cat 123 > out") == [("read", "123"), ("write", "out")]
    assert extract_path_claims("git status 2>&1") == [("read", "status"), ("read", "2")]


def test_pseudo_devices_are_not_claims() -> None:
    # `2>/dev/null` is shell plumbing, not file access — under coding,
    # write_outside="deny" must not turn it into a hard deny.
    assert extract_path_claims("cmd 2>/dev/null") == [("read", "2")]
    assert extract_path_claims("cmd > /dev/null 2>&1") == [("read", "2")]
    assert extract_path_claims("cat /dev/stdin") == []
    assert extract_path_claims("sort < /dev/fd/63") == []
    # ...but other device nodes still count as claims.
    assert extract_path_claims("cat /dev/disk0") == [("read", "/dev/disk0")]


def test_write_arguments_are_recorded_as_reads_by_design() -> None:
    # Only shell *redirection* is classified as a write; an argument a program
    # treats as an output path is indistinguishable from a read argument
    # lexically, so it is recorded as a read (documented limitation). The
    # decision stays bounded: coding still asks on outside paths; trusted is
    # trusted. See the module docstring.
    assert extract_path_claims("cp src.txt /etc/hosts") == [
        ("read", "src.txt"),
        ("read", "/etc/hosts"),
    ]
    assert extract_path_claims("tar -f /backup/out.tar") == [
        ("read", "/backup/out.tar")
    ]
    # A redirection to the same place IS a write claim.
    assert extract_path_claims("cat src.txt > /etc/hosts") == [
        ("read", "src.txt"),
        ("write", "/etc/hosts"),
    ]


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


def test_bare_arguments_are_claims_but_command_words_are_not() -> None:
    # `cat id_rsa` must meet the credential rule `read_file("id_rsa")` meets,
    # so bare arguments are claims (a relative false positive resolves inside
    # the root and only ever tightens). The command word is an executable,
    # not a file — unless it is spelled as a path.
    assert extract_path_claims("cat id_rsa") == [("read", "id_rsa")]
    assert extract_path_claims("npm run build") == [("read", "run"), ("read", "build")]
    assert extract_path_claims("git commit") == [("read", "commit")]
    assert extract_path_claims("./run.sh now") == [
        ("read", "./run.sh"),
        ("read", "now"),
    ]
    # A new segment has its own command word; a leading assignment keeps it.
    assert extract_path_claims("ls | sort -r") == []
    assert extract_path_claims("FOO=1 cat id_rsa") == [("read", "id_rsa")]
    # A number is a name like any other (`cat 123` may be a symlink to a
    # denied file); the false claim on a count resolves inside the root.
    assert extract_path_claims("head -n 5 file") == [("read", "5"), ("read", "file")]
    assert extract_path_claims("cat 123") == [("read", "123")]
    assert extract_path_claims("cmd 1> out") == [("read", "1"), ("write", "out")]


def test_hash_inside_a_word_is_not_a_comment() -> None:
    # shlex would cut `input#tag` in half and drop the rest of the line; a
    # shell starts a comment only at a word's start.
    assert extract_path_claims("sort input#tag > out.txt") == [
        ("read", "input#tag"),
        ("write", "out.txt"),
    ]


def test_output_options_are_not_parsed_here() -> None:
    # The guard does not know which option a program writes through (the
    # spellings are unbounded); it records the value as a read like any
    # argument. The policy caps such segments at ask instead — see
    # test_policy's output-option tests.
    assert extract_path_claims("sort -o .git/config in.txt") == [
        ("read", ".git/config"),
        ("read", "in.txt"),
    ]
    assert extract_path_claims("git diff --output=.git/config") == [
        ("read", "diff"),
        ("read", ".git/config"),
    ]


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


def test_cmd_switches_are_not_path_claims(monkeypatch) -> None:
    # ``dir /b``: on Windows a slash-letter word is a switch, not a path at
    # the drive root (which would resolve outside the root and ask).
    from lovia.workspace import command_guard

    monkeypatch.setattr(command_guard.os, "name", "nt")
    assert extract_path_claims("dir /b") == []
    assert extract_path_claims("dir /a:d /s") == []
    assert ("read", "/Users/neo") in extract_path_claims("dir /Users/neo")
    monkeypatch.setattr(command_guard.os, "name", "posix")
    assert ("read", "/b") in extract_path_claims("ls /b")


def test_relative_false_positives_are_harmless_by_construction() -> None:
    # sed expressions look like paths; they resolve inside the root where
    # reads are always allowed, so claiming them cannot escalate a decision.
    assert extract_path_claims("sed 's/foo/bar/' f.txt") == [
        ("read", "s/foo/bar/"),
        ("read", "f.txt"),
    ]
