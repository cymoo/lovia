"""Optional, opt-in built-in tools and helpers.

Nothing here is imported automatically — every submodule is loaded on demand.
Pick the ones you need::

    from lovia.builtins.http import http_fetch
    from lovia.builtins.fs import FileSystem
    from lovia.builtins.shell import Shell

Convention: stateful helpers are classes that expose ``.tool()`` (single
tool) or ``.tools()`` (multiple tools). Stateless helpers are module-level
:class:`~lovia.tools.Tool` instances.

A few submodules pull additional third-party libraries (e.g. ``ddgs`` for
:mod:`lovia.builtins.search`). Install them via the optional extra::

    pip install "lovia[tools]"
"""
