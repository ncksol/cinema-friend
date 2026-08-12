"""Sanitised captures of real BFI documents, and the loaders that read them.

A fixture in this package is a **recording**, not a design. It was taken from the live
site and edited only to remove what must not be committed; nothing in it was invented to
match the parser. That is the point: a hand-authored field list can only prove the parser
agrees with itself, which is exactly how the invented ``performance_id`` / ``event_id`` /
``availability_code`` / ``reserved_seating`` schema survived a full green suite.

``article_context_dog_stars.json``
    The ``articleContext`` object embedded in
    ``whatson.bfi.org.uk/imax/…permalink=dog-stars``, captured 2026-08-12 (HTTP 200, no
    ``cf-mitigated`` header). It carries the complete 98-name ``searchNames`` list and all
    five real ``searchResults`` rows from page 1 of 2, unedited.

    Two edits were made before it was written to disk:

    - ``sToken`` — the per-session pagination token — was replaced with
      ``FIXTURE-STOKEN-NOT-A-REAL-TOKEN``. It authorises requests, expires, and has no
      business in a repository.
    - The page-furniture keys (``searchHeaders``, ``searchLabels``, ``searchFilters``,
      ``searchCalendarFilters``, ``performanceDays``, ``searchSUMO``, ``loginLabels``)
      were dropped. No parser reads them and they are 9 kB of noise.

    Everything else — ``searchNames``, ``searchResults``, ``pagination``, ``articleId``,
    ``articleSearchId``, ``contextId``, ``salesType`` — is byte-for-byte what BFI served.
    The GUIDs that remain are public identifiers: they appear in the seat-map URL of
    every performance on the public site.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).parent
ARTICLE_CONTEXT_PATH = FIXTURE_DIR / "bfi" / "article_context_dog_stars.json"

FIXTURE_TOKEN = "FIXTURE-STOKEN-NOT-A-REAL-TOKEN"
"""The obvious fake standing in for the captured page's real ``sToken``."""


def load_article_context() -> dict[str, Any]:
    """Return a fresh mutable copy of the captured ``articleContext`` object."""
    with ARTICLE_CONTEXT_PATH.open(encoding="utf-8") as handle:
        context: dict[str, Any] = json.load(handle)
    return context


def article_context_search_names() -> list[str]:
    """The captured ``searchNames`` list, in wire order."""
    names = load_article_context()["searchNames"]
    assert isinstance(names, list)
    return names


def article_context_rows() -> list[list[Any]]:
    """The captured ``searchResults`` rows, in wire order."""
    rows = load_article_context()["searchResults"]
    assert isinstance(rows, list)
    return rows


def article_context_row_mappings() -> list[dict[str, Any]]:
    """The captured rows as field→value mappings."""
    names = article_context_search_names()
    return [dict(zip(names, row, strict=True)) for row in article_context_rows()]
