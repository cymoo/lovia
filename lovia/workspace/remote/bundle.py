"""The engine's source, packed to run on a far side that has no lovia.

A remote host starts ``python3 -c BOOT`` over its transport and writes
:func:`bundle` down the pipe. ``BOOT`` reads it, serves the modules from
memory (nothing touches the far disk), and enters :mod:`.serve`. Both ends
therefore run the very same source — there is no far-side version to drift.

:data:`ENGINE` is the whole of what runs there, and every file in it imports
only the standard library; ``tests/workspace/test_engine_stdlib.py`` holds
that line.
"""

from __future__ import annotations

import json
import zlib
from functools import lru_cache
from pathlib import Path

__all__ = ["BOOT", "ENGINE", "bundle"]

# Paths relative to the ``lovia`` package. The packages in between are sent
# empty: the real ``lovia/__init__.py`` would pull in everything else.
ENGINE = (
    "approvals.py",
    "exceptions.py",
    "workspace/command_guard.py",
    "workspace/errors.py",
    "workspace/local.py",
    "workspace/paths.py",
    "workspace/policy.py",
    "workspace/rules.py",
    "workspace/types.py",
    "workspace/remote/serve.py",
    "workspace/remote/wire.py",
)

# The far side's first words, passed as ``python3 -c``. Kept to what cannot
# be shipped: the version check, and loading the rest. It reads fd 0 with
# os.read on purpose — a buffered reader would swallow protocol bytes that
# follow the bundle.
BOOT = r"""
import os,sys,zlib,json,importlib.abc,importlib.util
if sys.version_info<(3,10):
    sys.stderr.write("lovia: the remote workspace needs python3 >= 3.10, found %s\n"%sys.version.split()[0]);sys.exit(3)
def rd(n):
    b=b""
    while len(b)<n:
        c=os.read(0,n-len(b))
        if not c:sys.exit(4)
        b+=c
    return b
h=b""
while not h.endswith(b"\n"):h+=rd(1)
src=json.loads(zlib.decompress(rd(int(h))))
class L(importlib.abc.MetaPathFinder,importlib.abc.Loader):
    def find_spec(s,n,p=None,t=None):
        if n in src:return importlib.util.spec_from_loader(n,s,is_package=any(k.startswith(n+".") for k in src))
    def exec_module(s,m):exec(compile(src[m.__name__],"<lovia>/"+m.__name__,"exec"),m.__dict__)
sys.meta_path.insert(0,L())
from lovia.workspace.remote.serve import main;main()
"""


@lru_cache(maxsize=1)
def bundle() -> bytes:
    """``<length>\\n`` followed by the zlib-compressed module table."""
    root = Path(__file__).resolve().parents[2]
    modules: dict[str, str] = {}
    for rel in ENGINE:
        name = "lovia." + rel[: -len(".py")].replace("/", ".")
        modules[name] = (root / rel).read_text("utf-8")
        # Every package on the way down, empty.
        parts = name.split(".")[:-1]
        for depth in range(1, len(parts) + 1):
            modules.setdefault(".".join(parts[:depth]), "")
    packed = zlib.compress(json.dumps(modules).encode("utf-8"))
    return f"{len(packed)}\n".encode("ascii") + packed
