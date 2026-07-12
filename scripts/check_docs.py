"""Validate built documentation links and executable-looking Python snippets."""

from __future__ import annotations

import ast
import importlib
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SITE = ROOT / "site"
SITE_PREFIX = "/lovia"
PYTHON_FENCE = re.compile(r"^```python\s*$\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL)


class ParsedHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"] or "")
        if tag == "a" and values.get("href"):
            self.links.append(values["href"] or "")


def _built_link_issues() -> list[str]:
    if not SITE.exists():
        return ["site/ does not exist; run mkdocs build first"]

    pages: dict[Path, ParsedHTML] = {}
    for path in SITE.rglob("*.html"):
        page = ParsedHTML()
        page.feed(path.read_text(encoding="utf-8"))
        pages[path.resolve()] = page

    issues: list[str] = []
    for source, page in pages.items():
        for raw in page.links:
            parts = urlsplit(raw)
            if (
                parts.scheme
                or parts.netloc
                or raw.startswith(("mailto:", "javascript:"))
            ):
                continue

            path_part = unquote(parts.path)
            if path_part == SITE_PREFIX:
                path_part = "/"
            elif path_part.startswith(f"{SITE_PREFIX}/"):
                path_part = path_part[len(SITE_PREFIX) :]

            if not path_part:
                target = source
            elif path_part.startswith("/"):
                target = SITE / path_part.lstrip("/")
            else:
                target = source.parent / path_part
            if target.is_dir():
                target /= "index.html"
            target = target.resolve()
            if target.suffix == "":
                target = (target / "index.html").resolve()

            label = source.relative_to(SITE)
            if not target.exists():
                issues.append(f"missing target: {label} -> {raw}")
                continue
            if parts.fragment and target.suffix == ".html":
                target_page = pages.get(target)
                fragment = unquote(parts.fragment)
                if target_page is not None and fragment not in target_page.ids:
                    issues.append(f"missing anchor: {label} -> {raw}")
    return sorted(set(issues))


def _snippet_issues() -> list[str]:
    issues: list[str] = []
    for path in sorted(DOCS.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        for index, match in enumerate(PYTHON_FENCE.finditer(text), 1):
            source = match.group(1)
            try:
                tree = ast.parse(source)
                compile(
                    source,
                    str(path),
                    "exec",
                    flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
                )
            except SyntaxError as exc:
                line = text.count("\n", 0, match.start()) + (exc.lineno or 1) + 1
                issues.append(
                    f"{path.relative_to(ROOT)}:{line}: Python fence {index}: {exc.msg}"
                )
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                if node.module != "lovia" and not node.module.startswith("lovia."):
                    continue
                line = text.count("\n", 0, match.start()) + node.lineno + 1
                try:
                    module = importlib.import_module(node.module)
                except Exception as exc:  # pragma: no cover - diagnostic path
                    issues.append(
                        f"{path.relative_to(ROOT)}:{line}: import {node.module}: {exc}"
                    )
                    continue
                for alias in node.names:
                    if alias.name != "*" and not hasattr(module, alias.name):
                        issues.append(
                            f"{path.relative_to(ROOT)}:{line}: "
                            f"{node.module}.{alias.name} does not exist"
                        )
    return issues


def _translation_issues() -> list[str]:
    issues: list[str] = []
    en = {path.name for path in (DOCS / "en").glob("*.md")}
    zh = {path.name for path in (DOCS / "zh").glob("*.md")}
    for name in sorted(en - zh):
        issues.append(f"missing Chinese page: docs/zh/{name}")
    for name in sorted(zh - en):
        issues.append(f"missing English page: docs/en/{name}")
    return issues


def main() -> int:
    issues = _built_link_issues() + _snippet_issues() + _translation_issues()
    if issues:
        print("Documentation checks failed:", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)
        return 1
    print("Documentation links, anchors, snippets, imports, and translations: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
