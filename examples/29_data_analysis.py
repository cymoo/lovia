"""Data analysis agent: query a SQLite DB via shell and render a Chart.js report.

Seeds ``tmp/sales.db`` with two years of synthetic sales data, then lets the
agent query it freely via the workspace ``shell`` tool and write the chart report.

Run::

    python examples/29_data_analysis.py

Open ``tmp/report.html`` in a browser to view the charts.
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown

from lovia import Agent, Runner, events, model_from_env
from lovia.workspace import Workspace

load_dotenv()
MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset
console = Console()

# ── database setup ────────────────────────────────────────────────────────────


def _seed(db_path: Path) -> None:
    """Populate a file-based SQLite database with two years of sales data."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE products (
            id       INTEGER PRIMARY KEY,
            name     TEXT    NOT NULL,
            category TEXT    NOT NULL,
            price    REAL    NOT NULL
        );
        CREATE TABLE orders (
            id         INTEGER PRIMARY KEY,
            product_id INTEGER NOT NULL REFERENCES products(id),
            quantity   INTEGER NOT NULL,
            amount     REAL    NOT NULL,
            region     TEXT    NOT NULL,
            order_date TEXT    NOT NULL   -- YYYY-MM-DD
        );
    """)

    products = [
        (1, "Laptop Pro", "Electronics", 1299.00),
        (2, "Wireless Mouse", "Electronics", 29.99),
        (3, 'Monitor 27"', "Electronics", 349.00),
        (4, "USB-C Hub", "Electronics", 59.99),
        (5, "Standing Desk", "Furniture", 499.00),
        (6, "Office Chair", "Furniture", 299.00),
        (7, "Desk Lamp", "Furniture", 45.00),
        (8, "Notebook Set", "Stationery", 12.99),
        (9, "Ballpoint Pens", "Stationery", 4.99),
        (10, "Sticky Notes", "Stationery", 6.99),
    ]
    cur.executemany("INSERT INTO products VALUES (?,?,?,?)", products)

    rng = random.Random(42)
    regions = ["North", "South", "East", "West"]
    rows: list[tuple] = []
    oid = 1
    for year in (2023, 2024):
        for month in range(1, 13):
            for _ in range(rng.randint(18, 30)):
                pid = rng.randint(1, 10)
                qty = rng.randint(1, 5)
                price = products[pid - 1][3]
                rows.append(
                    (
                        oid,
                        pid,
                        qty,
                        round(price * qty, 2),
                        rng.choice(regions),
                        f"{year}-{month:02d}-{rng.randint(1, 28):02d}",
                    )
                )
                oid += 1
    cur.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


# ── agent ─────────────────────────────────────────────────────────────────────

INSTRUCTIONS = """\
You are a data analyst. The workspace root contains `sales.db` — a SQLite
database with two tables:

    products(id, name, category, price)
    orders(id, product_id, quantity, amount, region, order_date  -- YYYY-MM-DD)

Use `shell` to query the database (e.g. `python3 -c "import sqlite3, json; ..."`)
and `write_file` to write the final report.

Write a single self-contained `report.html` that:
- Loads Chart.js from CDN: https://cdn.jsdelivr.net/npm/chart.js
- Embeds all data and chart code inline — no external files.
- Includes a title, one chart per question, and a short summary below each.
- Uses clean, modern inline CSS.

Finish with a concise plain-text summary of your findings.
"""

QUESTION = (
    "Analyse the sales data and show: "
    "(1) monthly revenue trend comparing 2023 vs 2024 as a line chart, "
    "(2) revenue share by product category as a pie chart, "
    "(3) top-5 best-selling products by total revenue as a bar chart."
)


async def main() -> None:
    tmp = Path("tmp")
    tmp.mkdir(exist_ok=True)

    db_path = tmp / "sales.db"
    db_path.unlink(missing_ok=True)
    _seed(db_path)
    console.print("[bold]Database seeded.[/bold] Starting agent …\n")
    console.print(f"[dim]Question:[/dim] {QUESTION}\n")

    agent = Agent(
        name="data-analyst",
        instructions=INSTRUCTIONS,
        model=MODEL,
        workspace=Workspace.local("tmp", mode="trusted"),
    )

    text_buf = ""

    def _flush() -> None:
        nonlocal text_buf
        if text_buf:
            console.print(Markdown(text_buf))
            text_buf = ""

    handle = Runner.stream(agent, QUESTION, max_turns=12)
    async for ev in handle:
        if isinstance(ev, events.TextDelta):
            text_buf += ev.delta
        elif isinstance(ev, events.ToolCallStarted):
            _flush()
            # arguments is raw JSON — normalise whitespace for the one-line preview
            preview = " ".join(ev.call.arguments.split())[:120]
            console.print(f"\n[cyan]▶ {ev.call.name}[/cyan] {preview}")
        elif isinstance(ev, events.ToolCallCompleted):
            style = "red" if ev.is_error else "green"
            console.print(f"[{style}]✓[/{style}] {ev.call.name}")

    _flush()

    result = await handle.result()
    console.print(f"\n[dim]turns={result.turns}[/dim]")

    report = tmp / "report.html"
    if report.exists():
        console.print(f"\n[bold]Report saved:[/bold] [cyan]{report.resolve()}[/cyan]")
    else:
        console.print("\n[yellow]Warning: report.html was not written.[/yellow]")


if __name__ == "__main__":
    asyncio.run(main())
