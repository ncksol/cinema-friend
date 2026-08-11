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

PERFORMANCE_ID = "2475959F-2B73-4EA6-AD26-AFA8AEB785FD"
ZONE_ID = "3F5950DF-50B9-45EB-A78A-E0E518827835"


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
    current_page: int | str = 1,
    total_pages: int | str = 1,
    token: str = "1,a/b+=",
    article_id: str = "2152D1E8-CFF7-419F-BE57-F51C1E490F24",
    title_with_apostrophe: str | None = None,
) -> str:
    """Produce a minimal HTML page that embeds a BFI articleContext script block.

    The emitted literal intentionally replicates three JavaScript differences that the
    parser must handle:
      1. Bare (unquoted) ``searchNames`` key.
      2. A trailing comma before the closing ``}``.
      3. Escaped apostrophes (``\\'``) in string values.

    Pass ``title_with_apostrophe`` to inject a ``"title"`` key whose value contains a
    plain apostrophe; the factory will encode it as the JS ``\\'`` escape so the
    normalisation path is exercised.
    """
    context: dict[str, Any] = {
        "searchNames": REQUIRED_FIELDS,
        "searchResults": rows,
        "pagination": {
            "current_page": str(current_page),
            "page_size": "5",
            "total_pages": str(total_pages),
        },
        "articleId": article_id,
        "sToken": token,
    }
    if title_with_apostrophe is not None:
        context["title"] = title_with_apostrophe
    literal = json.dumps(context).replace('"searchNames":', "searchNames :")
    literal = literal[:-1] + ",}"
    # Encode plain apostrophes in string values as the JS \' escape so the parser's
    # un-escape path (finding \' inside a JSON double-quoted string) is exercised.
    literal = literal.replace("'", r"\'")
    return f"<script>var articleContext = {literal};\n</script>"


def seat_map_html(
    performance_id: str = PERFORMANCE_ID,
    zone_id: str = ZONE_ID,
    zone_label: str = "1 Standard",
    price_text: str = "- £22.00",
    extra_circles: str = "",
) -> str:
    """Return a minimal seat-map HTML page suitable for ``parse_seat_map`` tests.

    The page includes:
    - A ``getPerformanceEcommerceObject`` call with *performance_id*.
    - One price zone whose GUID is *zone_id*, label is *zone_label*, and price is
      taken from *price_text* (``"- £22.00"`` format).
    - Two circles in the zone (seat-1 duplicated to exercise deduplication, seat-2).
    - An optional *extra_circles* snippet appended inside the zone ``<g>``.
    """
    return f"""<html><script>
getPerformanceEcommerceObject({{"item_id":"{performance_id}"}})
let priceZoneId = "{zone_id}";
priceZoneInfo[priceZoneId].label = "{zone_label}";
</script>
<div class="zone-label">{zone_label}</div>
<div class="price-zone-price-text">{price_text}</div>
<svg><g id="{zone_id}">
  <circle id="seat-1" data-status="A" data-seat-section="BFI IMAX"
    data-seat-row="L" data-seat-seat="17" cx="340" cy="180"/>
  <circle id="seat-1" data-status="A" data-seat-section="BFI IMAX"
    data-seat-row="L" data-seat-seat="17" cx="340" cy="180"/>
  <circle id="seat-2" data-status="S" data-seat-section="BFI IMAX"
    data-seat-row="L" data-seat-seat="18" cx="354" cy="180"/>
  {extra_circles}
</g></svg></html>"""
