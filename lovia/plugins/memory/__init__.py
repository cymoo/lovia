"""Memory plugin package: tiered, cross-session memory for an agent.

Public surface: the :class:`Memory` plugin (hot Notes + cold Archive), the
hot-tier :class:`NotesStore` seam, the cold-tier retrieval seam
(:class:`Doc` / :class:`Hit` / :class:`Index`) with its stdlib backends
(:class:`KeywordIndex`, :class:`VectorIndex`, :class:`HybridIndex`), and the
:class:`Embedder` seam with an OpenAI-compatible default. See ``plugin.py``
for the design narrative.
"""

from .index import Doc, Fusable, Hit, HybridIndex, Index, KeywordIndex
from .plugin import FileNotesStore, Memory, NotesStore
from .vector import Embedder, OpenAIEmbedder, VectorIndex

__all__ = [
    "Doc",
    "Embedder",
    "FileNotesStore",
    "Fusable",
    "Hit",
    "HybridIndex",
    "Index",
    "KeywordIndex",
    "Memory",
    "NotesStore",
    "OpenAIEmbedder",
    "VectorIndex",
]
