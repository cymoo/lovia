"""Regenerate the Web UI screenshots in ``docs/images/``.

Runs a real ``lovia web`` (the default agent, coding workspace, ``--trusted``)
against a scripted OpenAI-compatible stub built into this file, so no API key is
spent and nothing is nondeterministic. The tools really execute: the agent plans
with ``todo_write``, reads ``sales.csv``, writes and runs ``analyze.py``, and
writes ``report.md``, which is then opened in the Files panel. The workspace,
config, and chat DB live in a temp dir; only the images are written::

    python3 scripts/web_ui_screenshot.py      # -> docs/images/web-ui{,-dark}.webp

The driver needs ``playwright`` with Chromium (``pip install playwright &&
playwright install chromium``); no other package. The server runs under
``--python`` (default: the repo's ``.venv``), which needs ``lovia[web]``.
Re-run after UI changes; if a selector moved, the step that timed out says which.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
VIEWPORT = {"width": 1600, "height": 960}
SCALE = 1.5  # 2400x1440 output: sharp on retina at docs/README widths
QUALITY = 0.88  # WebP quality; small text stays crisp in both themes

PROMPT = (
    "Analyze sales.csv: which region grew fastest from Q2 to Q3? "
    "Write a short report to report.md."
)
TITLE = "Q3 regional sales analysis"
FOLLOWUPS = [
    "What drove West's growth in July?",
    "Break the results down by product line",
    "Draft a Q4 forecast from this trend",
]
# Earlier chats for the sidebar: (id, title, age in hours).
HISTORY = [
    ("s-tickets", "Summarize this week's support tickets", 22),
    ("s-billing", "Refactor the billing export script", 29),
    ("s-okr", "Draft Q4 OKR review notes", 55),
    ("s-churn", "Explain the churn model features", 110),
    ("s-onboard", "Onboarding checklist for new analysts", 146),
]

SALES_CSV = "month,region,revenue\n" + "".join(
    f"2026-{m:02d},{region},{revenue}\n"
    for m, row in zip(
        range(4, 10),
        [
            (182400, 141200, 203900, 118600),
            (176800, 146900, 198300, 127400),
            (189100, 150300, 207600, 134900),
            (191500, 152800, 201200, 149300),
            (186200, 158400, 205700, 163800),
            (194700, 161900, 210400, 177200),
        ],
    )
    for region, revenue in zip(("North", "South", "East", "West"), row)
)

ANALYZE_PY = """\
import csv
from collections import defaultdict

totals = defaultdict(lambda: [0, 0])  # region -> [Q2, Q3]
with open("sales.csv") as f:
    for row in csv.DictReader(f):
        quarter = 0 if int(row["month"][5:]) <= 6 else 1
        totals[row["region"]][quarter] += int(row["revenue"])

print(f"{'region':<8}{'Q2':>10}{'Q3':>10}{'growth':>9}")
for region, (q2, q3) in sorted(totals.items(), key=lambda kv: kv[1][1] / kv[1][0], reverse=True):
    print(f"{region:<8}{q2:>10,}{q3:>10,}{(q3 - q2) / q2:>+9.1%}")
q2, q3 = (sum(v[i] for v in totals.values()) for i in (0, 1))
print(f"{'Total':<8}{q2:>10,}{q3:>10,}{(q3 - q2) / q2:>+9.1%}")
"""

# The figures below are what ANALYZE_PY prints for SALES_CSV; keep them in sync.
REPORT_MD = """\
# Q3 2026 Regional Sales Review

Q3 revenue was **$2.15M**, up **8.9%** from Q2 ($1.98M).

| Region | Q2 | Q3 | Growth |
|---|---:|---:|---:|
| West | $380,900 | $490,300 | **+28.7%** |
| South | $438,400 | $473,100 | +7.9% |
| North | $548,300 | $572,400 | +4.4% |
| East | $609,800 | $617,300 | +1.2% |

## Highlights

- **West is the growth engine.** Revenue rose every month, from $118.6k in April \
to $177.2k in September, and West delivered 62% of the quarter's total increase.
- **East is flat.** It is still the largest region but grew only 1.2%, and July \
dipped below June.
- **South is steady** at +7.9%, with a new monthly high in each month of Q3.

## Next steps

1. Find out what changed in West in July: new reps, pricing, or a large account.
2. Review East's pipeline before Q4 planning.
"""

FINAL = (
    "I analyzed `sales.csv` and saved the write-up to `report.md`.\n\n"
    "- **Total:** Q3 revenue was $2.15M, up **8.9%** from Q2.\n"
    "- **Fastest growth:** **West, +28.7%**. It grew every month and delivered "
    "62% of the quarter's increase.\n"
    "- **Watch:** East is the largest region but grew only 1.2%.\n\n"
    "The report has the full table, highlights, and suggested next steps."
)

PLAN = [
    ("Inspect sales.csv", "Inspecting sales.csv"),
    ("Compute Q2 vs Q3 growth by region", "Computing growth by region"),
    ("Write the findings to report.md", "Writing report.md"),
]


def todos(done: int) -> dict[str, Any]:
    items = [
        {
            "content": content,
            "status": "completed"
            if i < done
            else "in_progress"
            if i == done
            else "pending",
            "active_form": active,
        }
        for i, (content, active) in enumerate(PLAN)
    ]
    return {"name": "todo_write", "args": {"todos": items}}


# One entry per model turn of the main run, picked by how many tool results the
# request already carries. ``reason`` streams as reasoning, ``text`` as the reply.
TURNS: list[dict[str, Any]] = [
    {
        "reason": "The user wants Q2 vs Q3 growth by region plus a written report. "
        "Plan the steps, inspect the data, compute the totals with a script, "
        "then write report.md.",
        "tool": todos(0),
    },
    {"tool": {"name": "read_file", "args": {"path": "sales.csv"}}},
    {"tool": todos(1)},
    {
        "tool": {
            "name": "write_file",
            "args": {"path": "analyze.py", "content": ANALYZE_PY},
        }
    },
    {"tool": {"name": "shell", "args": {"command": "python3 analyze.py"}}},
    {"tool": todos(2)},
    {
        "tool": {
            "name": "write_file",
            "args": {"path": "report.md", "content": REPORT_MD},
        }
    },
    {"tool": todos(3)},
    {"text": FINAL},
]

MEMORY_DIGEST = json.dumps(
    {
        "facts": [],
        "stale": [],
        "summary": "Analyzed sales.csv: Q3 revenue up 8.9% on Q2; West grew fastest "
        "(+28.7%). Wrote report.md.",
    }
)


# ---- stub model ------------------------------------------------------------------


def pick_turn(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Answer the side calls by their prompts; walk TURNS for the main run."""
    text = json.dumps(messages)
    if "facts: new durable facts" in text:  # the Memory plugin's run digest
        return {"text": MEMORY_DIGEST}
    if "3-6 word title" in text:
        return {"text": TITLE}
    if "follow-up" in text.lower():
        return {"text": "\n".join(FOLLOWUPS)}
    done = sum(1 for m in messages if m.get("role") == "tool")
    return TURNS[done] if done < len(TURNS) else {"text": "Done."}


class StubModel(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    call_seq = 0

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        self._json({"data": [{"id": "glm-5.2", "context_window": 200_000}]})

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
        messages = body.get("messages", [])
        turn = pick_turn(messages)
        call = None
        if tool := turn.get("tool"):
            StubModel.call_seq += 1
            call = {
                "id": f"call_{StubModel.call_seq}",
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "arguments": json.dumps(tool["args"]),
                },
            }
        # Plausible, growing token counts so the UI's usage readouts aren't zero.
        usage = {
            "prompt_tokens": 2400 + 380 * len(messages),
            "completion_tokens": 40 + len(turn.get("text", "")) // 4,
        }
        finish = "tool_calls" if call else "stop"
        if not body.get("stream"):
            message: dict[str, Any] = {"role": "assistant", "content": turn.get("text")}
            if call:
                message["tool_calls"] = [call]
            self._json(
                {
                    "id": "chatcmpl-stub",
                    "object": "chat.completion",
                    "model": "glm-5.2",
                    "choices": [
                        {"index": 0, "message": message, "finish_reason": finish}
                    ],
                    "usage": usage,
                }
            )
            return
        try:
            self._stream(turn, call, finish, usage)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream(
        self,
        turn: dict[str, Any],
        call: dict[str, Any] | None,
        finish: str,
        usage: dict[str, int],
    ) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("connection", "close")
        self.end_headers()

        def send(chunk: dict[str, Any]) -> None:
            chunk.update(
                id="chatcmpl-stub", object="chat.completion.chunk", model="glm-5.2"
            )
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.flush()

        def delta(d: dict[str, Any], reason: str | None = None) -> None:
            send({"choices": [{"index": 0, "delta": d, "finish_reason": reason}]})

        for key, field in (("reason", "reasoning_content"), ("text", "content")):
            text = turn.get(key) or ""
            for i in range(0, len(text), 12):
                delta({field: text[i : i + 12]})
                time.sleep(0.02)
        if call:
            delta({"tool_calls": [{"index": 0, **call}]})
        delta({}, finish)
        send({"choices": [], "usage": usage})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


# ---- setup -----------------------------------------------------------------------


def seed(db: str) -> None:
    """Write HISTORY into the chat DB. Runs under the server's interpreter."""
    import asyncio
    import sqlite3

    from lovia.transcript import AssistantTextEntry, InputEntry
    from lovia.web.store import ChatStore

    async def write() -> None:
        store = ChatStore.sqlite(db)
        for sid, title, _ in HISTORY:
            entries = [
                InputEntry(role="user", content=title),
                AssistantTextEntry(content="Done."),
            ]
            await store.session.append(sid, entries, run_id=f"run-{sid}")
            await store.upsert(sid, agent="lovia", title=title)

    asyncio.run(write())
    now = time.time()
    with sqlite3.connect(db) as conn:  # upsert stamps "now"; backdate for the sidebar
        conn.executemany(
            "UPDATE chat_sessions SET created_at = ?, updated_at = ? WHERE id = ?",
            [
                (now - hours * 3600, now - hours * 3600, sid)
                for sid, _, hours in HISTORY
            ],
        )


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def default_python() -> str:
    for rel in (".venv/bin/python", ".venv/Scripts/python.exe"):
        if (REPO / rel).exists():
            return str(REPO / rel)
    return sys.executable


def wait_ready(url: str, proc: subprocess.Popen[bytes], log: Path) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            sys.exit(f"lovia web exited early:\n{log.read_text()}")
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except OSError:
            time.sleep(0.3)
    sys.exit(f"lovia web did not come up at {url}:\n{log.read_text()}")


def to_webp(page: Any, png: bytes) -> bytes:
    """Encode through Chromium's canvas, so the driver needs no image library."""
    url = page.evaluate(
        """async ([b64, q]) => {
            const img = new Image();
            img.src = 'data:image/png;base64,' + b64;
            await img.decode();
            const c = document.createElement('canvas');
            c.width = img.naturalWidth;
            c.height = img.naturalHeight;
            c.getContext('2d').drawImage(img, 0, 0);
            return c.toDataURL('image/webp', q);
        }""",
        [base64.b64encode(png).decode(), QUALITY],
    )
    if not url.startswith("data:image/webp"):
        sys.exit("this Chromium cannot encode WebP")
    return base64.b64decode(url.split(",", 1)[1])


def shoot(url: str, out: Path) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(
            viewport=VIEWPORT, device_scale_factor=SCALE, locale="en-US"
        )
        page.goto(url, wait_until="domcontentloaded")  # SSE never lets the net idle
        page.click("#new-chat")
        page.fill("#prompt", PROMPT)
        page.click("#send")
        page.wait_for_selector("#followups .followup", timeout=60_000)
        page.wait_for_selector(f"#chat-title:has-text('{TITLE}')")

        page.click("#files-btn")
        page.locator("#files-list").get_by_text("report.md", exact=True).first.click()
        page.wait_for_selector("#files-viewer:not(.hidden)")
        page.wait_for_timeout(2500)  # the run-finished toast fades

        encoder = browser.new_page()  # about:blank: no page CSP around data: URLs
        for scheme, name in (("light", "web-ui.webp"), ("dark", "web-ui-dark.webp")):
            page.emulate_media(color_scheme=scheme)
            page.wait_for_timeout(800)
            data = to_webp(encoder, page.screenshot())
            (out / name).write_bytes(data)
            print(f"wrote {out / name} ({len(data) / 1024:.0f} KB)")
        browser.close()


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", type=Path, default=REPO / "docs" / "images")
    ap.add_argument(
        "--python",
        default=default_python(),
        help="interpreter with lovia[web] for the server (default: the repo .venv)",
    )
    ap.add_argument("--seed", metavar="DB", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.seed:
        seed(args.seed)
        return

    stub = ThreadingHTTPServer(("127.0.0.1", 0), StubModel)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="lovia-shot-") as tmp:
        root = Path(tmp)
        ws, run, db = root / "ws", root / "run", root / "chat.db"
        ws.mkdir()
        (run / ".lovia").mkdir(parents=True)
        (ws / "sales.csv").write_text(SALES_CSV)
        (ws / "README.md").write_text(
            "# Sales ops\n\nMonthly revenue exports land in `sales.csv` "
            "(one row per month and region).\n"
        )
        # A project-scope config wins over ~/.lovia/config.json as a whole file,
        # so the server can only ever reach the stub.
        model = {
            "id": "stub",
            "name": "glm-5.2",
            "model": "glm-5.2",
            "flavor": "openai",
            "base_url": f"http://127.0.0.1:{stub.server_address[1]}/v1",
            "api_key": "stub",
            "context_window": 200_000,
            "vision": "off",
        }
        config = {
            "version": 1,
            "models": [model],
            "roles": {"chat": "stub", "aux": "stub"},
            "skills": {"dirs": []},
        }
        (run / ".lovia" / "config.json").write_text(json.dumps(config))
        subprocess.run([args.python, __file__, "--seed", str(db)], check=True)

        port = free_port()
        log = root / "server.log"
        with log.open("wb") as log_file:
            cmd = [args.python, "-m", "lovia.web", "--port", str(port)]
            cmd += ["--db", str(db), "--workspace", str(ws), "--trusted"]
            proc = subprocess.Popen(
                cmd,
                cwd=run,
                env={**os.environ, "BROWSER": "true", "PYTHONUNBUFFERED": "1"},
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            try:
                url = f"http://127.0.0.1:{port}/"
                wait_ready(url, proc, log)
                if not re.search(r"config\s+\.lovia/config\.json", log.read_text()):
                    sys.exit(f"server did not load the stub config:\n{log.read_text()}")
                args.out.mkdir(parents=True, exist_ok=True)
                shoot(url, args.out)
            finally:
                proc.terminate()
                proc.wait(timeout=10)
    stub.shutdown()


if __name__ == "__main__":
    main()
