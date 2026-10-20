"""UI components: a tool result the chat UI renders as UI instead of text.

A tool opts in by returning :func:`ui_result`'s envelope as its output. The
bundled page (``static/js/components.js``) hands the envelope's props to the
renderer registered for its ``kind`` — built-in ones, or yours, loaded via
``ChatUI(scripts=[...])`` and registered with
``window.lovia.registerRenderer(kind, {render, snapshot})``. Bulk data stays
out of the model's context with :func:`put_ui_data`. See
``docs/design/web-ui-components.md`` and ``docs/design/web-component-data.md``.
"""

from __future__ import annotations

import contextvars
import json
import re
from typing import TYPE_CHECKING, Any

from ..exceptions import ToolError, UserError

if TYPE_CHECKING:
    from .store import ChatStore

ENVELOPE_KEY = "lovia_ui"
# Mirrored by KIND_RE in static/js/components.js.
_KIND_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
# Mirrored by REF_RE in static/js/components.js; ChatStore.put_ui_data mints them.
_REF_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
# "props omitted" — distinct from None, a valid (JSON null) prop.
_UNSET: Any = object()

MAX_UI_DATA_BYTES = 8 * 1024 * 1024

# The store and session a tool's put_ui_data writes to. Set where the web app
# runs every run (RunSupervisor._run); tool tasks inherit it, since
# create_task copies the context.
UI_DATA_SINK: contextvars.ContextVar[tuple[ChatStore, str] | None] = (
    contextvars.ContextVar("lovia_web_ui_data_sink", default=None)
)


async def put_ui_data(value: Any) -> str | None:
    """Keep a component's full props out of the model's context; return a ref.

    The value is stored with the current chat — and deleted with it — for
    the page to fetch. Pass the ref as ``ui_result(..., data=ref)``, with a
    preview as ``props``: what the model reads, and what the page shows if
    the data is gone::

        @tool
        async def query(sql: str) -> str:
            columns, rows = await run_sql(sql)
            ref = await put_ui_data({"columns": columns, "rows": rows})
            preview = {"columns": columns, "rows": rows[:20], "row_count": len(rows)}
            return ui_result("table", preview, data=ref)

    Outside a ``lovia web`` run (a script, an eval), or once the chat has
    been deleted, there is no chat to keep it in: returns None, and
    ``ui_result(..., data=None)`` is a plain envelope — the preview.
    """
    sink = UI_DATA_SINK.get()
    if sink is None:
        return None
    payload = _check_json_size(
        value,
        what="UI data",
        limit=MAX_UI_DATA_BYTES,
        hint="aggregate, or return fewer rows",
    )
    store, session_id = sink
    return await store.put_ui_data(session_id, payload)


def ui_result(
    kind: str,
    props: Any = _UNSET,
    *,
    from_args: bool | str = False,
    data: str | None = None,
) -> str:
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

    ``data`` is a ref from :func:`put_ui_data`: the page draws the stored
    props instead, and ``props`` only when they are gone (``None`` props: a
    notice alone). ``data=None`` adds nothing.

    Props reach the browser as untrusted data: a renderer must write text with
    ``textContent`` (or sanitize), never as raw HTML.

    Keep the envelope under the agent's ``max_tool_output_chars`` (200K chars
    by default): a longer output is truncated, which breaks its JSON, and the
    card falls back to showing it as text.
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
    if data is not None:
        if has_ref:
            raise UserError(
                "ui_result: `data` goes with `props`, not `from_args`",
                hint="pass the preview as props and the put_ui_data ref as data",
            )
        if not isinstance(data, str) or not _REF_RE.fullmatch(data):
            raise UserError(
                f"invalid UI data ref: {data!r}",
                hint="pass the ref put_ui_data returned",
            )
    body: dict[str, Any] = {"kind": kind}
    if has_ref:
        body["from_args"] = from_args
    else:
        body["props"] = props
    if data is not None:
        body["data"] = data
    try:
        # allow_nan=False: NaN/Infinity aren't JSON — the browser can't parse them.
        return json.dumps({ENVELOPE_KEY: body}, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise UserError(
            f"ui_result props are not JSON-serializable: {exc}",
            hint="convert values to plain dict/list/str/number/bool/None "
            "first (finite numbers only — use None for NaN)",
        ) from exc


def _check_json_size(value: Any, *, what: str, limit: int, hint: str) -> bytes:
    """``value`` as JSON bytes, or :class:`ToolError` unless finite and ≤ ``limit``.

    The shared check of the presentational tools (``show_chart``, ``show_table``),
    :func:`~lovia.web.validate_chart_option` and :func:`put_ui_data`: data the
    page will draw. ``hint`` says how to shrink it.
    """
    try:
        # Python parses NaN/Infinity out of a model's arguments, but the
        # browser's JSON.parse rejects them — the component would vanish.
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    except ValueError as exc:
        raise ToolError(
            f"the {what} contains NaN or Infinity",
            hint="use null for missing values",
        ) from exc
    except TypeError as exc:  # e.g. Decimal or datetime from a database row
        raise ToolError(
            f"the {what} is not JSON: {exc}",
            hint="convert values to str/number/bool/None first "
            "(Decimal → float, datetime → ISO string)",
        ) from exc
    if len(encoded) > limit:
        raise ToolError(
            f"the {what} is {len(encoded) // 1024} KiB; the limit is {limit // 1024} KiB",
            hint=hint,
        )
    return encoded
