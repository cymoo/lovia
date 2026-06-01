"""Mutable state passed between runner phases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..handoff import _HandoffSignal
from ..messages import AssistantTurn
from ..transcript import TranscriptEntry


@dataclass
class TurnState:
    """Scratch space populated while one model turn is streamed and handled."""

    assistant: AssistantTurn | None = None
    turn_entries: list[TranscriptEntry] | None = None
    handoff_signal: _HandoffSignal | None = None
    final_via_tool: Any = None
