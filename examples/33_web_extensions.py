"""Extend the chat UI's behavior with window.lovia: tool views, events, actions.

Run::

    pip install -e .[web]
    python examples/33_web_extensions.py
    # open http://127.0.0.1:8000 and ask "how do I rotate an API key?"

The page loads ``examples/web_extensions/extension.js`` (``ChatUI(scripts=...)``)
before it starts. The script uses only ``window.lovia``, the page's public
script API:

- **Tool views.** ``registerToolView('*', ...)`` gives every tool a friendly
  one-line summary instead of ``search_docs(query: …)``. ``search_docs`` gets a
  view of its own: its card lists the hits as links (``render``), and the
  chat's HTML export keeps them (``snapshot``).
- **Events.** ``run-start`` / ``run-end`` drive a "Working…" pill in the top
  bar and a per-chat token counter; ``session`` resets the counter.
- **Actions.** A "Summarize" button in the composer sends a message
  (``lovia.send``), and a "New ticket" entry in the sidebar fills the composer
  with a template to edit (``lovia.setDraft``).
- **A view.** The sidebar's "Docs index" link (``?view=docs``) opens a page
  in place of the chat (``registerView``), listing the docs from this
  script's own ``/acme/docs`` route; each entry links to ``?view=docs/<slug>``.
  It is drawn with Preact + htm, still with no build step: the copies in
  ``web_extensions/vendor/`` are mapped to their bare names by an import map
  in ``index.html``'s ``head`` block.

``index.html`` adds the button, the entries and the pill through template
blocks; ``messages`` supplies their wording (``lovia.t`` reads it too).
"""

from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv

from lovia import Agent, enable_logging, model_from_env, tool
from lovia.web import SURFACE_NOTE, ChatUI, create_app, serve

load_dotenv()
MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset

_DOCS = [  # (title, path under https://example.com/docs/, snippet)
    ("Rotating API keys", "keys#rotate", "Create the new key, then revoke the old."),
    ("Key permissions", "keys#scopes", "Scope a key to the projects it needs."),
    ("Audit log", "audit", "Every key creation, use and revocation is logged."),
    ("Rate limits", "limits", "Each key gets 600 requests a minute by default."),
    ("Webhooks", "webhooks", "Sign payloads with the webhook secret, not an API key."),
]


@tool
def search_docs(query: str) -> str:
    """Search Acme's documentation. Returns JSON: {"results": [{title, url, snippet}]}.

    Args:
        query: What to look for.
    """
    words = query.lower().split()
    hits = [
        {"title": title, "url": f"https://example.com/docs/{path}", "snippet": snippet}
        for title, path, snippet in _DOCS
        if any(w in f"{title} {snippet}".lower() for w in words)
    ]
    return json.dumps({"results": hits[:4]}, ensure_ascii=False)


@tool
def open_ticket(subject: str, details: str) -> str:
    """File a support ticket for the user.

    Args:
        subject: A one-line summary.
        details: What happened, steps to reproduce, what was expected.
    """
    return f"Ticket T-4821 opened: {subject}"


UI = ChatUI(
    assets=Path(__file__).parent / "web_extensions",
    scripts=["extension.js"],
    styles=["extension.css"],
    empty_title="Acme Docs Assistant",
    empty_description="Answers from the Acme documentation, with links.",
    empty_examples=["How do I rotate an API key?", "What are the rate limits?"],
    messages={
        "en": {
            "acme.newTicket": "New ticket",
            "acme.ticketTemplate": "Please open a ticket.\nSubject: \nWhat happened: \nExpected: ",
            "acme.summarize": "Summarize",
            "acme.summarizePrompt": "Summarize this conversation in three bullet points.",
            "acme.working": "Working…",
            "acme.tokens": "{n} tokens",
            "acme.tool.search_docs": "Searching the docs",
            "acme.tool.search_docs.done": "Searched the docs for “{query}”",
            "acme.tool.open_ticket": "Opening a ticket",
            "acme.tool.open_ticket.done": "Opened a ticket",
            "acme.noHits": "No matching pages.",
            "acme.docs": "Docs index",
            "acme.allDocs": "← All pages",
            "acme.askAbout": "Ask about this",
            "acme.filter": "Filter pages",
        },
        "zh": {
            "acme.newTicket": "新建工单",
            "acme.ticketTemplate": "请帮我提交一个工单。\n标题：\n发生了什么：\n期望的结果：",
            "acme.summarize": "总结",
            "acme.summarizePrompt": "用三个要点总结这次对话。",
            "acme.working": "处理中…",
            "acme.tokens": "{n} tokens",
            "acme.tool.search_docs": "正在搜索文档",
            "acme.tool.search_docs.done": "已在文档中搜索「{query}」",
            "acme.tool.open_ticket": "正在提交工单",
            "acme.tool.open_ticket.done": "已提交工单",
            "acme.noHits": "没有找到相关页面。",
            "acme.docs": "文档目录",
            "acme.allDocs": "← 全部页面",
            "acme.askAbout": "问问这一页",
            "acme.filter": "筛选页面",
        },
    },
)


def main() -> None:
    enable_logging()
    agent = Agent(
        name="docs",
        instructions=(
            "You answer questions about Acme from its documentation: search it "
            "with search_docs and cite the pages you use as links. When the "
            "user asks for a ticket, file it with open_ticket.\n\n" + SURFACE_NOTE
        ),
        model=MODEL,
        tools=[search_docs, open_ticket],
    )
    app = create_app(agent, title="Acme Docs", ui=UI)

    @app.get("/acme/docs")
    def docs() -> list[dict[str, str]]:
        """The docs index the view lists (no auth here: the demo binds to loopback)."""
        return [
            {"slug": path.replace("#", "-"), "title": title, "snippet": snippet}
            for title, path, snippet in _DOCS
        ]

    serve(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
