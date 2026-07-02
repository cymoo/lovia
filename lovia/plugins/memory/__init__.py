"""Memory plugin package: tiered, cross-session memory for an agent.

Public surface: the :class:`Memory` plugin (hot Notes + cold searchable tier),
the cold-tier retrieval seam (:class:`Doc` / :class:`Hit` / :class:`Index`)
with its stdlib backends, and the hot-tier :class:`NotesStore` seam. See
``plugin.py`` for the full design narrative.
"""

from .index import Doc, Fusable, Hit, HybridIndex, Index, KeywordIndex
from .plugin import (
    ArchiveHit,
    ArchiveStore,
    FileNotesStore,
    Memory,
    NotesStore,
    SQLiteArchiveStore,
)

__all__ = [
    "ArchiveHit",
    "ArchiveStore",
    "Doc",
    "FileNotesStore",
    "Fusable",
    "Hit",
    "HybridIndex",
    "Index",
    "KeywordIndex",
    "Memory",
    "NotesStore",
    "SQLiteArchiveStore",
]
