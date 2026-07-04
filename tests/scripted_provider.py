"""Compatibility shim — the scripted provider now lives at :mod:`lovia.testing`."""

from lovia.testing import ScriptedProvider, batch, call, text

__all__ = ["ScriptedProvider", "batch", "call", "text"]
