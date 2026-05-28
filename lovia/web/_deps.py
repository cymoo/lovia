"""Optional dependency guards for the web extra."""

from __future__ import annotations

from typing import NoReturn

from ..exceptions import UserError


def raise_missing_web_extra(exc: ImportError) -> NoReturn:
    raise UserError(
        "lovia.web requires the 'web' extra.",
        hint='Install with: pip install "lovia[web]"',
    ) from exc
