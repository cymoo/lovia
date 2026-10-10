"""Render a tool result as an interactive component in the chat UI.

Run::

    pip install -e .[web]
    python examples/31_web_components.py
    # open http://127.0.0.1:8000 and ask "top 10 products in the EU"

The ``top_products`` tool returns ``ui_result("table", ...)`` — a UI envelope
— instead of text. ``examples/web_components/table.js`` registers a renderer
for kind ``table`` (sort by clicking a header, filter, "Ask" posts a follow-up
message), loaded through ``ChatUI(scripts=[...])``. The model still sees the
rows as the tool output, so it can talk about them.
"""

from __future__ import annotations

import random
from pathlib import Path

from dotenv import load_dotenv
from starlette.staticfiles import StaticFiles

from lovia import Agent, enable_logging, model_from_env, tool
from lovia.web import ChatUI, create_app, serve, ui_result

load_dotenv()
MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset

_rng = random.Random(7)
_PRODUCTS = [
    (
        f"SKU-{i:03d}",
        _rng.choice(["EU", "US", "APAC"]),
        _rng.randint(50, 5000),
        round(_rng.uniform(5, 300), 2),
    )
    for i in range(1, 61)
]


@tool
def top_products(n: int = 10, region: str | None = None) -> str:
    """Best-selling products by units, shown to the user as an interactive table.

    Args:
        n: How many products to list.
        region: Optional region filter: EU, US, or APAC.
    """
    rows = [p for p in _PRODUCTS if region is None or p[1] == region.upper()]
    rows = sorted(rows, key=lambda p: p[2], reverse=True)[:n]
    return ui_result(
        "table",
        {
            "title": f"Top {len(rows)} products"
            + (f" · {region.upper()}" if region else ""),
            "columns": ["sku", "region", "units", "price"],
            "rows": [list(p) for p in rows],
        },
    )


def main() -> None:
    enable_logging()
    agent = Agent(
        name="shop",
        instructions=(
            "You are a sales analyst. top_products already shows the user a "
            "table — don't repeat it; comment on what stands out instead."
        ),
        model=MODEL,
        tools=[top_products],
    )
    app = create_app(agent, ui=ChatUI(scripts=["ext/table.js"]))
    app.mount("/ext", StaticFiles(directory=Path(__file__).parent / "web_components"))
    serve(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
