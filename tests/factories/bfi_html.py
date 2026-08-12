"""Factories for BFI HTML fixture data.

Every performance row these factories emit starts as a **row from the live capture**
(:mod:`tests.fixtures`), with only the named overrides applied. Nothing here declares
what BFI's schema is; it reads it. A factory that authored its own field list would
prove only that the parser agrees with the factory, which is how an entirely invented
schema (``performance_id``, ``event_id``, ``availability_code``, ``reserved_seating``)
passed a green suite while matching nothing the site actually serves.
"""

from __future__ import annotations

import json
from typing import Any

from tests.fixtures import (
    article_context_rows,
    article_context_search_names,
    load_article_context,
)

# The ordered field names BFI's articleContext searchNames carries, read from the capture.
SEARCH_NAMES: list[str] = article_context_search_names()

_REAL_ROWS: list[list[Any]] = article_context_rows()
_DEFAULT_ROW: list[Any] = list(_REAL_ROWS[0])

PERFORMANCE_ID: str = str(_DEFAULT_ROW[SEARCH_NAMES.index("id")])
ZONE_ID = "3F5950DF-50B9-45EB-A78A-E0E518827835"

ACCESS_NOTE = "NB: This is a space for wheelchair users and their companion"
"""The wording BFI puts in ``data-tsmessage`` on an accessible space.

Taken from the live seat map recorded in the design's verification table: eight seats
on the verified performance carried such a message, every one of them wheelchair-space
or companion wording.
"""

# The wire value of `options` that means "this performance has a reserved seating plan".
RESERVED_SEATING_OPTIONS: list[str] = list(_DEFAULT_ROW[SEARCH_NAMES.index("options")])

# Cloudflare's *passive* JSD probe, injected into ordinary HTTP 200 pages that were
# never challenged. Reproduced from the live BFI film page (see the Task 16 fix-round-2
# evidence capture); only the ray id and timestamp are replaced with obvious fakes.
# It is the reason a bare "cdn-cgi/challenge-platform" substring cannot mean "challenge":
# this script is present on every successful response.
PASSIVE_JSD_SCRIPT = (
    "<script>(function(){function c(){var b=a.contentDocument||"
    "(a.contentWindow&&a.contentWindow.document);if(b){var d=b.createElement('script');"
    "d.innerHTML=\"window.__CF$cv$params={r:'FIXTURE0RAYID0000',t:'RklYVFVSRQ=='};"
    "var a=document.createElement('script');"
    "a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';"
    "document.getElementsByTagName('head')[0].appendChild(a);\";"
    "b.getElementsByTagName('head')[0].appendChild(d)}}"
    "if(document.body){var a=document.createElement('iframe');c()}})();</script>"
)

# Cloudflare's managed/orchestrate interstitial: the body actually served *instead of*
# the page when a request is challenged. Its script path is the `/h/` orchestrate shape,
# which the passive probe above never uses.
MANAGED_CHALLENGE_HTML = (
    "<html><head><title>Just a moment...</title></head><body>"
    "<div class='cf-browser-verification'></div>"
    "<div id='challenge-error-text'>Enable JavaScript and cookies to continue</div>"
    "<script src='/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"
    "?ray=FIXTURE0RAYID0000'></script>"
    "</body></html>"
)


def performance_row(**overrides: Any) -> list[Any]:
    """Return a searchResults row: the first captured real row, with *overrides* applied.

    Values are passed through untouched, including their type. ``availability_num`` is a
    *string* on the wire (``"387"``), so a test that wants a different count passes
    ``availability_num="12"``; passing an ``int`` is how a test asserts the parser
    rejects a type BFI does not send.
    """
    row = list(_DEFAULT_ROW)
    for key, value in overrides.items():
        row[SEARCH_NAMES.index(key)] = value
    return row


def performance_mapping(**overrides: Any) -> dict[str, Any]:
    """Return a single searchResults row as a field→value mapping."""
    row = performance_row(**overrides)
    return dict(zip(SEARCH_NAMES, row, strict=True))


def real_article_html(*, current_page: int | None = None) -> str:
    """Render the captured ``articleContext`` object as HTML.

    The capture is page 1 of a two-page listing. Passing *current_page* renumbers only
    the pagination block so the rest of the pagination chain can be served from the same
    real data; every field a parser reads is otherwise the captured one.
    """
    context = load_article_context()
    if current_page is not None:
        context["pagination"] = {**context["pagination"], "current_page": str(current_page)}
    return article_context_html(context)


def article_context_html(context: dict[str, Any]) -> str:
    """Wrap *context* in the ``var articleContext = …;`` script block BFI serves.

    The emitted literal replicates three JavaScript-vs-JSON differences the parser must
    handle: a bare (unquoted) key, a trailing comma before the closing brace, and
    apostrophes written as the JS ``\\'`` escape.
    """
    literal = json.dumps(context).replace('"searchNames":', "searchNames :")
    literal = literal[:-1] + ",}"
    literal = literal.replace("'", r"\'")
    return f"<script>var articleContext = {literal};\n</script>"


def make_article_html(
    *,
    rows: list[list[Any]],
    current_page: int | str = 1,
    total_pages: int | str = 1,
    token: str = "1,a/b+=",
    article_id: str = "2152D1E8-CFF7-419F-BE57-F51C1E490F24",
    search_names: list[str] | None = None,
) -> str:
    """Produce an HTML page embedding an articleContext built from *rows*.

    ``searchNames`` defaults to the captured real list, so a row built by
    :func:`performance_row` lines up with it by construction.
    """
    context: dict[str, Any] = {
        "searchNames": SEARCH_NAMES if search_names is None else search_names,
        "searchResults": rows,
        "pagination": {
            "current_page": str(current_page),
            "page_size": "5",
            "total_pages": str(total_pages),
        },
        "articleId": article_id,
        "sToken": token,
    }
    return article_context_html(context)


def seat_map_html(
    performance_id: str = PERFORMANCE_ID,
    zone_id: str = ZONE_ID,
    zone_label: str = "1 Standard",
    price_text: str = "- £22.00",
    extra_circles: str = "",
    access_note: str | None = ACCESS_NOTE,
) -> str:
    """Return a minimal seat-map HTML page suitable for ``parse_seat_map`` tests.

    The page includes:
    - A ``getPerformanceEcommerceObject`` call with *performance_id*.
    - One price zone whose GUID is *zone_id*, label is *zone_label*, and price is
      taken from *price_text* (``"- £22.00"`` format).
    - Two circles in the zone (seat-1 duplicated to exercise deduplication, seat-2).
    - One ``data-status="A"`` seat in that same ordinary zone carrying *access_note*
      in ``data-tsmessage``, which is how BFI marks a wheelchair space or its
      companion seat. Passing ``access_note=None`` omits it, for the tests that need
      a map with no access note at all.
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
  {access_note_circle(access_note)}
  {extra_circles}
</g></svg></html>"""


def access_note_circle(note: str | None = ACCESS_NOTE, *, seat_id: str = "seat-wheelchair") -> str:
    """Return one available seat carrying *note* in ``data-tsmessage``, or nothing.

    The attribute is ``data-tsmessage`` because that is the one BFI serves. A fixture
    that invented its own attribute name proved only that the parser agreed with the
    fixture, while the live signal went unread.
    """
    if note is None:
        return ""
    return (
        f'<circle id="{seat_id}" data-status="A" data-seat-section="BFI IMAX" '
        f'data-seat-row="L" data-seat-seat="19" data-tsmessage="{note}" cx="368" cy="180"/>'
    )



def available_circles(count: int, *, first_index: int = 3) -> str:
    """Return *count* additional ``data-status="A"`` seat circles for ``seat_map_html``.

    Used to build a seat map whose available-seat count matches a captured row's
    ``availability_num``, which is the equality the smoke command enforces.
    """
    return "\n  ".join(
        f'<circle id="seat-{i}" data-status="A" data-seat-section="BFI IMAX" '
        f'data-seat-row="M" data-seat-seat="{i}" cx="{340 + i * 14}" cy="200"/>'
        for i in range(first_index, first_index + count)
    )
