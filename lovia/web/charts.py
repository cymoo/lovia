"""``show_chart``: charts in the chat UI, as an Apache ECharts option.

A presentational UI component (see :mod:`lovia.web.components`): the model
writes the option as the call's argument, the server checks it just enough to
hand the model a retry signal, and the bundled page renders it from the
arguments (``static/js/charts.js``, which also sanitizes it). Passing the
option through — instead of a lovia chart schema — means every ECharts chart
type works with no code here, in a format models already know.

Off by default in ``lovia web`` (``"charts": true`` in config.json); add it to
your own agent with ``tools=[show_chart]``.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from ..exceptions import ToolError
from ..tools.base import tool
from .components import ui_result

__all__ = ["SERIES_TYPES", "show_chart"]

# Mirrored by SERIES_TYPES in static/js/charts.js (a test keeps them equal).
# Left out: `custom` (needs a renderItem function), `map` / geo `lines` (need
# registered map data), `pictorialBar` (image symbols).
SERIES_TYPES = frozenset(
    {
        "line",
        "bar",
        "pie",
        "scatter",
        "effectScatter",
        "radar",
        "tree",
        "treemap",
        "sunburst",
        "boxplot",
        "candlestick",
        "heatmap",
        "parallel",
        "funnel",
        "gauge",
        "sankey",
        "graph",
        "themeRiver",
    }
)
MAX_OPTION_BYTES = 64 * 1024


@tool
def show_chart(
    option: Annotated[
        dict[str, Any],
        "An Apache ECharts option object (JSON only — no functions).",
    ],
) -> str:
    """Show the user an interactive chart, drawn from an Apache ECharts option.

    Use it when numbers compare or trend better as a picture. The chart is the
    answer — don't repeat its data as a table; comment on what stands out.
    Prefer `dataset.source` rows over per-series data, e.g.:

    {"title": {"text": "Revenue"}, "tooltip": {"trigger": "axis"},
     "legend": {}, "dataset": {"source": [["month", "2025", "2026"],
     ["Jan", 120, 150], ["Feb", 132, 170]]}, "xAxis": {"type": "category"},
     "yAxis": {}, "series": [{"type": "bar"}, {"type": "line"}]}

    Series types: line, bar, pie, scatter, effectScatter, radar, tree,
    treemap, sunburst, boxplot, candlestick, heatmap, parallel, funnel,
    gauge, sankey, graph, themeRiver.
    """
    series = option.get("series")
    items = [series] if isinstance(series, dict) else series
    if not isinstance(items, list) or not items:
        raise ToolError(
            "option.series is missing or empty",
            hint='add at least one series, e.g. "series": [{"type": "bar"}]',
        )
    for i, s in enumerate(items):
        kind = s.get("type") if isinstance(s, dict) else None
        if kind not in SERIES_TYPES:
            raise ToolError(
                f"option.series[{i}].type {kind!r} is not supported",
                hint="use one of: " + ", ".join(sorted(SERIES_TYPES)),
            )
    size = len(json.dumps(option, ensure_ascii=False).encode())
    if size > MAX_OPTION_BYTES:
        raise ToolError(
            f"the option is {size // 1024} KiB; the limit is {MAX_OPTION_BYTES // 1024} KiB",
            hint="aggregate or sample the data before charting it",
        )
    return ui_result("chart", from_args="option")
