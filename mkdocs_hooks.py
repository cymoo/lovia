"""MkDocs build hooks.

The Markdown sources under ``docs/`` double as GitHub-rendered pages, so some of
their relative links point at things the built site lays out differently:

* repo files *outside* the docs tree (root READMEs, ``examples/``, ``AGENTS.md``)
  — rewritten to absolute GitHub URLs;
* the other language's folder (``../zh/README.md`` / ``../en/README.md``) — which
  mkdocs-static-i18n builds as a separate context, so rewrite to that language's
  site home.

Both resolve on GitHub but not on the site; we fix them at build time and leave
the source Markdown untouched. In-site links (e.g. ``../architecture.md``) are
deliberately left alone.
"""

from __future__ import annotations

import re
from typing import Any

_BLOB = "https://github.com/cymoo/lovia/blob/main"
_DEFAULT_LOCALE = "en"

# ``](  ../…/<target>  )`` where <target> is a repo file outside the docs tree.
# ``architecture.md`` is intentionally excluded so it stays an in-site link.
_OUTBOUND = re.compile(
    r"\]\((?:\.\./)+(examples/[^)\s]+|README(?:-zh)?\.md|AGENTS\.md)(#[^)]*)?\)"
)

# ``](../zh/quickstart.md#anchor)`` — a link into the other language folder.
_CROSS_LANG = re.compile(r"\]\(\.\./(en|zh)/([^)\s]+?)\.md(#[^)]*)?\)")


def on_page_markdown(markdown: str, *, config: Any = None, **_kwargs: object) -> str:
    markdown = _OUTBOUND.sub(rf"]({_BLOB}/\1\2)", markdown)

    site_url = getattr(config, "site_url", None) if config is not None else None
    if site_url:
        base = site_url if site_url.endswith("/") else site_url + "/"

        def _rewrite(m: "re.Match[str]") -> str:
            locale, name, anchor = m.group(1), m.group(2), m.group(3) or ""
            home = base if locale == _DEFAULT_LOCALE else f"{base}{locale}/"
            # use_directory_urls: README is the folder index, others get a slug dir.
            target = home if name == "README" else f"{home}{name}/"
            return f"]({target}{anchor})"

        markdown = _CROSS_LANG.sub(_rewrite, markdown)

    return markdown
