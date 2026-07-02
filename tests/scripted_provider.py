"""Compatibility shim — the scripted provider now lives at :mod:`lovia.testing`."""

from lovia.testing import ScriptedProvider, call, text

__all__ = ["ScriptedProvider", "call", "text"]
