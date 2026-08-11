"""Factories for BFI HTML fixture data."""

from __future__ import annotations

import json
from typing import Any

# The ordered list of field names the BFI articleContext searchNames key carries.
REQUIRED_FIELDS: list[str] = [
    "performance_id",
    "event_id",
    "start_date",
    "sales_status",
    "availability_code",
    "availability_num",
    "reserved_seating",
    "object_type",
    "options",
]

_DEFAULT_ROW: list[Any] = [
    "2475959F-2B73-4EA6-AD26-AFA8AEB785FD",
    "E8A1B2C3-D4E5-F6A7-B8C9-D0E1F2A3B4C5",
    "Saturday 08 August 2026 14:00",
    "OPEN",
    "A",
    387,
    True,
    "P",
    ["Subtitles"],
]


def performance_row(**overrides: Any) -> list[Any]:
    """Return a single searchResults row as a list."""
    row = list(_DEFAULT_ROW)
    for key, value in overrides.items():
        idx = REQUIRED_FIELDS.index(key)
        row[idx] = value
    return row


def performance_mapping(**overrides: Any) -> dict[str, Any]:
    """Return a single searchResults row as a field→value mapping."""
    row = performance_row(**overrides)
    return dict(zip(REQUIRED_FIELDS, row, strict=True))


def make_article_html(
    *,
    rows: list[list[Any]],
    current_page: int = 1,
    total_pages: int = 1,
    token: str = "1,a/b+=",
) -> str:
    """Produce a minimal HTML page that embeds a BFI articleContext script block.

    The emitted literal intentionally replicates three JavaScript differences that the
    parser must handle:
      1. Bare (unquoted) ``searchNames`` key.
      2. A trailing comma before the closing ``}``.
      3. Escaped apostrophes (``\\'``) in string values.
    """
    context = {
        "searchNames": REQUIRED_FIELDS,
        "searchResults": rows,
        "pagination": {
            "current_page": str(current_page),
            "page_size": "5",
            "total_pages": str(total_pages),
        },
        "articleId": "2152D1E8-CFF7-419F-BE57-F51C1E490F24",
        "sToken": token,
    }
    literal = json.dumps(context).replace('"searchNames":', "searchNames :")
    literal = literal[:-1] + ",}"
    literal = literal.replace("O'Brien", r"O\'Brien")
    return f"<script>var articleContext = {literal};\n</script>"
