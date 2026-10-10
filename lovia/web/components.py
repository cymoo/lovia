"""UI components: a tool result the chat UI renders as UI instead of text.

A tool opts in by returning :func:`ui_result`'s envelope as its output. The
bundled page (``static/js/components.js``) hands the envelope's props to the
renderer registered for its ``kind`` — built-in ones, or yours, loaded via
``ChatUI(scripts=[...])`` and registered with
``window.lovia.registerRenderer(kind, render)``. See
``docs/design/web-ui-components.md``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..exceptions import UserError

ENVELOPE_KEY = "lovia_ui"
# Mirrored by KIND_RE in static/js/components.js.
_KIND_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
# "props omitted" — distinct from None, a valid (JSON null) prop.
_UNSET: Any = object()


def ui_result(kind: str, props: Any = _UNSET, *, from_args: bool | str = False) -> str:
    """Wrap a component as a tool output the chat UI renders as ``kind``.

    Pass the data inline as ``props`` (a tool that *computes* it — the model
    sees it too, as the tool output), or point at the call's own arguments
    with ``from_args``: ``True`` for the whole arguments object, a field name
    for one top-level field. The latter suits a presentational tool whose
    arguments already are the data, so it isn't echoed back a second time::

        @tool
        def top_products(n: int = 10) -> str:
            rows = query_top_products(n)
            return ui_result("table", {"columns": ["sku", "units"], "rows": rows})

    Props reach the browser as untrusted data: a renderer must write text with
    ``textContent`` (or sanitize), never as raw HTML.
    """
    if not isinstance(kind, str) or not _KIND_RE.fullmatch(kind):
        raise UserError(
            f"invalid UI component kind: {kind!r}",
            hint="use 1-64 chars of [a-z0-9_.-], starting with a letter, e.g. 'table'",
        )
    has_ref = from_args is not False
    if (props is _UNSET) == (not has_ref):
        raise UserError(
            "ui_result needs exactly one of `props` and `from_args`",
            hint="pass the data as props, or from_args=True / a field name to "
            "render the call's own arguments",
        )
    if has_ref and not (
        from_args is True or (isinstance(from_args, str) and from_args)
    ):
        raise UserError(
            f"invalid from_args: {from_args!r}",
            hint="pass True (the whole arguments object) or a top-level field name",
        )
    body: dict[str, Any] = {"kind": kind}
    if has_ref:
        body["from_args"] = from_args
    else:
        body["props"] = props
    try:
        # allow_nan=False: NaN/Infinity aren't JSON — the browser can't parse them.
        return json.dumps({ENVELOPE_KEY: body}, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise UserError(
            f"ui_result props are not JSON-serializable: {exc}",
            hint="convert values to plain dict/list/str/number/bool/None "
            "first (finite numbers only — use None for NaN)",
        ) from exc
