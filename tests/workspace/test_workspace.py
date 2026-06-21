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
