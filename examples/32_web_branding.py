"""Brand the bundled chat UI: logo, colors, your own links, wording, languages.

Run::

    pip install -e .[web]
    python examples/32_web_branding.py
    # open http://127.0.0.1:8000 and ask "where is order A-1001?"

Everything the page needs lives in ``examples/web_branding/``, which
``ChatUI(assets=...)`` serves at ``/ui/``:

- ``index.html`` extends the bundled page and fills four of its blocks: a
  help link under the sidebar's navigation, a status link in the top bar, a
  notice on the welcome screen, and who is signed in at the sidebar's foot.
- ``theme.css`` sets the brand color per theme through the design tokens,
  hides the Export menu through its ``data-part``, and styles the elements
  ``index.html`` adds. Plain rules win over the bundled stylesheet, which
  sits in the ``lovia`` cascade layer — no ``!important``.
- ``logo.svg`` replaces the sidebar's title, ``favicon.svg`` the tab icon.

``messages`` rewords the UI per language, labels the template's own elements
(the ``acme.*`` keys, through ``data-i18n``), and adds Japanese, which then
shows up in Settings → Language. Edit a file in ``web_branding/`` and reload:
the page revalidates its assets, no restart needed.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

from lovia import Agent, enable_logging, model_from_env, tool
from lovia.web import SURFACE_NOTE, ChatUI, create_app, serve

load_dotenv()
MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset

_ORDERS = {
    "A-1001": ("shipped", "arrives Thursday"),
    "A-1002": ("processing", "ships within 2 business days"),
    "A-1003": ("delivered", "signed for at the front desk"),
}


@tool
def order_status(order_id: str) -> str:
    """Look up an Acme order.

    Args:
        order_id: The order number, like A-1001.
    """
    status = _ORDERS.get(order_id.strip().upper())
    if status is None:
        return f"No order {order_id}. Known demo orders: {', '.join(_ORDERS)}."
    return f"Order {order_id.upper()}: {status[0]} — {status[1]}."


UI = ChatUI(
    assets=Path(__file__).parent / "web_branding",
    styles=["theme.css"],
    logo="logo.svg",
    favicon="favicon.svg",
    empty_title="How can we help?",
    empty_description="Ask about an order, a return, or anything Acme.",
    empty_examples=["Where is order A-1001?", "How do I return an item?"],
    messages={
        "en": {
            "composer.placeholder": "Ask Acme Support…",
            "acme.help": "Help center",
            "acme.status": "All systems normal",
            "acme.notice": "Order data in this demo is made up.",
            "acme.signedIn": "Signed in as Demo User",
        },
        "zh": {
            "composer.placeholder": "向 Acme 客服提问…",
            "acme.help": "帮助中心",
            "acme.status": "系统运行正常",
            "acme.notice": "本演示中的订单数据是虚构的。",
            "acme.signedIn": "当前用户：演示用户",
        },
        # A language the UI doesn't ship: listed in Settings; strings it
        # lacks fall back to English.
        "ja": {
            "nav.newChat": "新しいチャット",
            "nav.chats": "チャット",
            "nav.settings": "設定",
            "composer.placeholder": "Acme サポートに質問…",
            "acme.help": "ヘルプセンター",
            "acme.status": "すべて正常です",
            "acme.notice": "このデモの注文データは架空のものです。",
            "acme.signedIn": "デモユーザーでログイン中",
        },
    },
)


def main() -> None:
    enable_logging()
    agent = Agent(
        name="acme",
        instructions=(
            "You are Acme's friendly support agent. Look orders up with "
            "order_status; keep answers short.\n\n" + SURFACE_NOTE
        ),
        model=MODEL,
        tools=[order_status],
    )
    serve(create_app(agent, title="Acme Support", ui=UI), host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
