"""Top-level ``lovia`` command-line dispatcher.

Deliberately tiny, with no argparse subparsers: each subcommand owns its
own argument parsing, so ``lovia web --help`` is exactly
``python -m lovia.web --help`` and future subcommands are one entry each.
"""

from __future__ import annotations

import sys

from . import __version__
from .exceptions import UserError

_USAGE = """\
usage: lovia <command> [options]

commands:
  web           launch the chat web UI (lovia web --help for its options)

options:
  -h, --help    show this help
  --version     show the version

examples:
  lovia web                                # serve on http://127.0.0.1:8000
  lovia web --port 9000 --model openai:gpt-5.5
"""


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(_USAGE, end="")
        return 0
    if argv[0] == "--version":
        print(f"lovia {__version__}")
        return 0
    command, rest = argv[0], argv[1:]
    if command == "web":
        try:
            from .web.__main__ import main as web_main
        except UserError as exc:  # the web extra is not installed
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return web_main(rest, prog="lovia web")
    print(f"error: unknown command {command!r}\n\n{_USAGE}", file=sys.stderr, end="")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
