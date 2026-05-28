"""Optional, opt-in built-in tools.

Nothing here is imported automatically — every submodule is loaded on demand.
Pick the ones you need::

    from lovia.builtins.http import http_fetch
    from lovia.builtins.search import web_search
    from lovia.builtins.todo import TodoList

Filesystem and shell tools live in :mod:`lovia.sandbox` instead — they are
backed by a real sandbox abstraction (path traversal guard, audit policy,
process isolation hook) and ship as ``sandbox_tools()``.

A few submodules pull additional third-party libraries (e.g. ``ddgs`` for
:mod:`lovia.builtins.search`). Install them via the optional extra::

    pip install "lovia[tools]"
"""
