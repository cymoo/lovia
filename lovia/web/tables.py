"""``show_table``: interactive tables in the chat UI.

The ``table`` component's props are ``{"title"?, "columns": [str], "rows":
[[scalar]]}``; the bundled page (``static/js/tables.js``) sorts, filters,
pages, and exports them. ``show_table`` is the presentational tool — the model
writes the rows as the call's arguments. A tool that computes the rows (a
query, say) returns ``ui_result("table", props)`` instead, so the numbers
never pass through the model's output. The renderer ignores keys it doesn't
know, so such a tool may add model-facing extras (a row count, the SQL) to
the same props.

``lovia web``'s default agent has it; add it to your own agent with
``tools=[show_table]``.
"""

from __future__ import annotations

from typing import Annotated

from ..exceptions import ToolError
from ..tools.base import tool
from .components import _check_json_size, ui_result

__all__ = ["MAX_TABLE_BYTES", "show_table"]

MAX_TABLE_BYTES = 64 * 1024

Cell = str | int | float | bool | None


@tool
def show_table(
    columns: Annotated[list[str], "Column headings."],
    rows: Annotated[list[list[Cell]], "The rows, each with one value per column."],
    title: Annotated[str | None, "A short heading for the table."] = None,
) -> str:
    """Show the user an interactive table (sortable, filterable, CSV download).

    Use it for tabular answers longer than a few rows: comparisons, rankings,
    lists of records. Put numbers in as numbers, not strings, so they sort and
    align as numbers. The table is the answer — don't repeat it in Markdown;
    comment on what stands out. E.g.:

    {"title": "Largest cities", "columns": ["city", "country", "population"],
     "rows": [["Tokyo", "Japan", 37400068], ["Delhi", "India", 28514000]]}
    """
    if not columns:
        raise ToolError("columns is empty", hint="name at least one column")
    width = len(columns)
    for i, row in enumerate(rows):
        if len(row) != width:
            raise ToolError(
                f"rows[{i}] has {len(row)} values; there are {width} columns",
                hint="give every row one value per column, null for a missing one",
            )
    _check_json_size(
        {"columns": columns, "rows": rows, "title": title},
        what="table",
        limit=MAX_TABLE_BYTES,
        hint="show fewer rows (the top ones, or an aggregate) and say what was left out",
    )
    return ui_result("table", from_args=True)
