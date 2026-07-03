"""Validation in the ``Workspace.local`` factory (``lovia.workspace.workspace``).

The factory turns a ``mode`` preset (optionally refined) or an explicit
``policy`` into a :class:`LocalWorkspace`; these cover the mutually-exclusive
argument checks and the unknown-mode guard.
"""

from __future__ import annotations

import pytest

from lovia.exceptions import UserError
from lovia.workspace import CommandRule, LocalWorkspace, Workspace, WorkspacePolicy


def test_explicit_policy_builds_workspace(tmp_path) -> None:
    ws = Workspace.local(str(tmp_path), policy=WorkspacePolicy.coding())
    assert isinstance(ws, LocalWorkspace)


def test_policy_and_preset_refinements_are_mutually_exclusive(tmp_path) -> None:
    with pytest.raises(UserError, match="either policy="):
        Workspace.local(
            str(tmp_path),
            policy=WorkspacePolicy.coding(),
            denied_paths=(".env",),
        )


def test_readonly_mode_rejects_command_rules(tmp_path) -> None:
    with pytest.raises(UserError, match="readonly"):
        Workspace.local(
            str(tmp_path),
            mode="readonly",
            command_rules=(CommandRule("rm", "deny"),),
        )


def test_unknown_mode_is_rejected(tmp_path) -> None:
    with pytest.raises(UserError, match="Unknown workspace mode"):
        Workspace.local(str(tmp_path), mode="bogus")  # type: ignore[arg-type]


def test_readable_writable_shorthands_expand_to_rules(tmp_path) -> None:
    ws = Workspace.local(str(tmp_path), readable=("~/docs",), writable=("/srv/out",))
    rules = ws.policy.path_rules
    # writable first (read+write), then readable (read-only).
    assert rules[0].pattern == "/srv/out" and rules[0].ops == frozenset(
        {"read", "write"}
    )
    assert rules[1].pattern == "~/docs" and rules[1].ops == frozenset({"read"})
    assert (
        ws.policy.decide_path(rel=None, abs_posix="/srv/out/f.txt", op="write")
        == "allow"
    )


def test_shorthands_conflict_with_explicit_policy(tmp_path) -> None:
    with pytest.raises(UserError, match="not both"):
        Workspace.local(
            str(tmp_path),
            policy=WorkspacePolicy.coding(),
            readable=("~/docs",),
        )


def test_readonly_mode_accepts_readable_grants(tmp_path) -> None:
    ws = Workspace.local(str(tmp_path), mode="readonly", readable=("/srv/ref",))
    assert ws.policy.write == "deny"
    # The grant opens exactly its scope; everything else outside stays denied.
    assert (
        ws.policy.decide_path(rel=None, abs_posix="/srv/ref/doc.md", op="read")
        == "allow"
    )
    assert (
        ws.policy.decide_path(rel=None, abs_posix=tmp_path.parent.as_posix(), op="read")
        == "deny"
    )
