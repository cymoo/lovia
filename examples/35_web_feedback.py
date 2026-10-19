"""Thumbs up / down on a served agent's answers, kept by your own route.

Run::

    pip install -e .[web]
    python examples/35_web_feedback.py
    # open http://127.0.0.1:8000, ask something, rate the answer, then
    # open http://127.0.0.1:8000/feedback/report

``web_feedback/feedback.js`` adds two kinds of buttons to the messages'
footers with ``lovia.registerMessageAction``:

- **👍 / 👎 on answers.** A rating is stored under the chat and the answer's
  ``ref`` — the id the server gives each user message (a reply carries its
  question's), ``"<run_id>:<n>"``. Unlike the message's position, a ref is
  stable: it survives reloads, and a rewound-away message's ref never comes
  back, so an edited question or a regenerated answer gets a fresh one and old
  ratings never attach to new text. A ref arrives when the answer's run
  finishes; until then the buttons don't show.
- **Quote** on every message: puts it in the message box as a quote. It needs
  no ref, so it works on any message.

lovia stores no feedback: the two routes below are the whole backend. Put them
behind the same sign-in as the chat in a real app (``auth=`` guards lovia's
own routes, not yours).
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel

from lovia import Agent, enable_logging, model_from_env
from lovia.web import SURFACE_NOTE, ChatUI, create_app, serve

load_dotenv()
MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset

DB = Path(".lovia") / "feedback.db"


class Feedback(BaseModel):
    session_id: str
    ref: str
    rating: Literal["up", "down"] | None  # None takes a rating back
    text: str = ""  # the answer as rated — it may be rewound out of the chat later


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """A connection per request (FastAPI runs these routes on a thread pool),
    committed when the block succeeds."""
    db = sqlite3.connect(DB)
    try:
        with db:
            yield db
    finally:
        db.close()


def add_feedback_routes(app: FastAPI) -> None:
    DB.parent.mkdir(exist_ok=True)
    with connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS feedback (session_id TEXT, ref TEXT,"
            " rating TEXT, text TEXT, at REAL, PRIMARY KEY (session_id, ref))"
        )

    @app.post("/feedback")
    def rate(fb: Feedback) -> dict[str, bool]:
        with connect() as db:
            if fb.rating is None:
                db.execute(
                    "DELETE FROM feedback WHERE session_id = ? AND ref = ?",
                    (fb.session_id, fb.ref),
                )
            else:
                db.execute(
                    "INSERT OR REPLACE INTO feedback VALUES (?, ?, ?, ?, ?)",
                    (fb.session_id, fb.ref, fb.rating, fb.text, time.time()),
                )
        return {"ok": True}

    @app.get("/feedback")
    def ratings(session_id: str) -> dict[str, str]:
        """One chat's ratings, ref → rating, so the page can show them."""
        with connect() as db:
            rows = db.execute(
                "SELECT ref, rating FROM feedback WHERE session_id = ?", (session_id,)
            )
            return dict(rows.fetchall())

    @app.get("/feedback/report")
    def report() -> list[dict[str, object]]:
        keys = ("session_id", "ref", "rating", "text")
        with connect() as db:
            rows = db.execute(
                f"SELECT {', '.join(keys)} FROM feedback ORDER BY at DESC"
            )
            return [dict(zip(keys, row)) for row in rows.fetchall()]


UI = ChatUI(
    assets=Path(__file__).parent / "web_feedback",
    scripts=["feedback.js"],
    styles=["feedback.css"],
    empty_title="Rate the answers",
    empty_description="Hover an answer for 👍 / 👎; ratings outlive reloads and rewinds.",
    empty_examples=[
        "Explain HTTP caching in three sentences.",
        "Write a haiku about tea.",
    ],
    messages={
        "en": {
            "acme.up": "Good answer",
            "acme.down": "Bad answer",
            "acme.quote": "Quote in a reply",
            "acme.thanks": "Thanks — noted.",
            "acme.failed": "Couldn't save the rating",
        },
        "zh": {
            "acme.up": "回答得好",
            "acme.down": "回答得不好",
            "acme.quote": "引用到回复",
            "acme.thanks": "谢谢，已记录。",
            "acme.failed": "评分保存失败",
        },
    },
)


def main() -> None:
    enable_logging()
    agent = Agent(
        name="assistant",
        instructions="Answer briefly and plainly.\n\n" + SURFACE_NOTE,
        model=MODEL,
    )
    app = create_app(agent, title="Rated answers", ui=UI)
    add_feedback_routes(app)
    serve(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
