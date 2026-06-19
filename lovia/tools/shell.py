"""Backward-compatible re-export for the workspace ``shell`` tool.

The shell tool is workspace-scoped, so its definition now lives in
:mod:`lovia.workspace.tools`. It remains importable from here (and from
:mod:`lovia.tools`) for compatibility; new code should prefer
``Agent(workspace=Workspace.local(...))``, which adds it automatically when
the policy allows shell.
"""

from __future__ import annotations

from ..workspace.tools import shell

__all__ = ["shell"]
