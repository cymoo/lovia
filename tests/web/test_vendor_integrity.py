"""The vendored UI libraries, the template's SRI attributes, and the vendor
README manifest must agree.

The template pins each vendored script with ``integrity="sha384-..."`` so a
corrupted or tampered file refuses to execute in the browser. That pin lives
in three places — the file itself, the ``<script>`` tag, and the manifest in
``static/vendor/README.md`` — and upgrading a library while forgetting one of
them would make the script silently fail its hash and stop loading. This test
turns that silent runtime failure into a loud CI failure with the correct
hash in the message.
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "lovia" / "web"
VENDOR = WEB / "static" / "vendor"
TEMPLATE = WEB / "templates" / "index.html"
MANIFEST = VENDOR / "README.md"


def sha384_sri(path: Path) -> str:
    digest = hashlib.sha384(path.read_bytes()).digest()
    return "sha384-" + base64.b64encode(digest).decode("ascii")


def vendored_files() -> list[Path]:
    return sorted(VENDOR.glob("*.min.js"))


def test_vendor_directory_is_not_empty() -> None:
    assert vendored_files(), f"no vendored libraries found under {VENDOR}"


def test_template_pins_every_vendored_file_with_its_actual_hash() -> None:
    html = TEMPLATE.read_text(encoding="utf-8")
    for path in vendored_files():
        tag = re.search(rf"""<script[^>]*vendor/{re.escape(path.name)}[^>]*>""", html)
        assert tag, f"{path.name} is vendored but not referenced by index.html"
        expected = sha384_sri(path)
        integrity = re.search(r'integrity="([^"]+)"', tag.group(0))
        assert integrity, f"{path.name}: script tag has no integrity attribute"
        assert integrity.group(1) == expected, (
            f"{path.name}: template integrity is stale.\n"
            f"  template: {integrity.group(1)}\n"
            f"  actual:   {expected}\n"
            "Update the integrity attribute in templates/index.html and the "
            "manifest in static/vendor/README.md."
        )


def test_manifest_lists_every_vendored_file_with_its_actual_hash() -> None:
    manifest = MANIFEST.read_text(encoding="utf-8")
    for path in vendored_files():
        assert path.name in manifest, f"{path.name} missing from vendor/README.md"
        assert sha384_sri(path) in manifest, (
            f"{path.name}: hash in vendor/README.md does not match the file; "
            f"actual is {sha384_sri(path)}"
        )


def test_template_references_no_external_script_or_style_origins() -> None:
    html = TEMPLATE.read_text(encoding="utf-8")
    external = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
    assert not external, f"index.html references external origins: {external}"
