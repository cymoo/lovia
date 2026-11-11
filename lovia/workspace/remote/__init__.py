"""Workspaces that live somewhere else: a running container, an SSH host.

The session engine runs where the files are — the unchanged
``LocalWorkspaceSession``, started from source this side sends down the pipe
— and a proxy here forwards every call to it. The far side needs
``python3`` >= 3.10 and nothing else: no lovia, no pip, nothing written to
its disk.

* :mod:`.host` — :class:`RemoteHost`: the connection, and what losing it means.
* :mod:`.session` — the proxy the tools talk to.
* :mod:`.config` — :class:`RemoteWorkspace`, what ``Agent.workspace`` takes.
* :mod:`.bundle`, :mod:`.serve`, :mod:`.wire` — what is sent, what runs
  there, and the lines in between.
"""

from .config import RemoteWorkspace
from .host import RemoteHost
from .session import RemoteWorkspaceSession

__all__ = ["RemoteHost", "RemoteWorkspace", "RemoteWorkspaceSession"]
