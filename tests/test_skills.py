from __future__ import annotations

import tempfile
from pathlib import Path

from lovia.skills import SkillCatalog


def test_skill_catalog_from_dir() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "policy").mkdir()
        (root / "policy" / "SKILL.md").write_text(
            "---\nname: refund\ndescription: How to handle refunds.\n---\n# Refund policy\n..."
        )
        (root / "no_manifest").mkdir()  # should be skipped

        cat = SkillCatalog.from_dir(root)
        assert cat.names() == ["refund"]
        assert "refund: How to handle refunds." in cat.render_catalog()
        skill = cat.get("refund")
        assert skill is not None and "Refund policy" in skill.content


def test_skill_catalog_empty_dir() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cat = SkillCatalog.from_dir(tmp)
        assert cat.names() == []
        assert cat.render_catalog() == ""
