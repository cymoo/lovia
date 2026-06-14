"""Plugins: declarative, additive capability bundles attached to an Agent.

A :class:`Plugin` contributes tools, system-prompt instructions, per-turn view
injectors, event observers (hooks), and input/output guardrails to a run. The
runner activates each once per run (and once per agent on a handoff) via the
async :meth:`Plugin.setup`, and tears down any resources via
:attr:`PluginInstance.aclose` when the run ends.

Built-in plugins live in this package — currently :func:`todos`.
"""

from __future__ import annotations

from .base import Plugin, PluginInstance, ViewInjector
from .skills import (
    LocalDirSkillSource,
    Skill,
    SkillFilter,
    SkillMetadata,
    SkillSource,
    Skills,
    SkillsError,
    skills,
)
from .todos import (
    Todo,
    TodoInput,
    TodoList,
    TodoStatus,
    render_todos,
    todos,
    todos_from_entries,
)

__all__ = [
    "Plugin",
    "PluginInstance",
    "ViewInjector",
    "LocalDirSkillSource",
    "Skill",
    "SkillFilter",
    "SkillMetadata",
    "SkillSource",
    "Skills",
    "SkillsError",
    "skills",
    "Todo",
    "TodoInput",
    "TodoList",
    "TodoStatus",
    "render_todos",
    "todos",
    "todos_from_entries",
]
