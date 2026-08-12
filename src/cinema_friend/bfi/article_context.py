"""Parse BFI ``articleContext`` script blocks and performance pages."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cinema_friend.bfi.urls import seat_map_url as _seat_map_url
from cinema_friend.domain.bfi import (
    UNPUBLISHED_AVAILABILITY_SENTINEL,
    UNPUBLISHED_AVAILABILITY_STATUS,
    Performance,
)
from cinema_friend.domain.errors import BfiContractError
from cinema_friend.domain.time_window import LONDON as _LONDON

_START_FMT = "%A %d %B %Y %H:%M"

# Matches ``var articleContext = { … };`` spanning multiple lines.
CONTEXT_RE = re.compile(r"var\s+articleContext\s*=\s*(\{.*?\});", re.DOTALL)


@dataclass(frozen=True, slots=True)
class ArticlePage:
    """Parsed representation of a single BFI article search-results page."""

    article_id: str
    s_token: str
    current_page: int
    total_pages: int
    title: str | None
    rows: tuple[Mapping[str, object], ...]


def extract_article_context(html: str) -> Mapping[str, object]:
    """Extract and JSON-normalise the ``articleContext`` object from *html*.

    Three JavaScript-vs-JSON differences are corrected:

    1. Bare (unquoted) object keys → quoted keys.
    2. Trailing commas before ``}`` or ``]`` → removed.
    3. Escaped apostrophes (``\\'``) → plain apostrophes.
    """
    match = CONTEXT_RE.search(html)
    if match is None:
        raise BfiContractError("articleContext not found")
    literal = match.group(1)
    # 1. Quote bare keys: ``{  key :`` or ``,  key :`` → ``{ "key":``
    literal = re.sub(r'([{,]\s*)([A-Za-z_]\w*)\s*:', r'\1"\2":', literal)
    # 2. Strip trailing commas before closing brace/bracket.
    literal = re.sub(r",(\s*[}\]])", r"\1", literal)
    # 3. Un-escape apostrophes that JavaScript allows inside double-quoted strings.
    literal = literal.replace(r"\'", "'")
    try:
        value = json.loads(literal)
    except json.JSONDecodeError as exc:
        raise BfiContractError("articleContext is not parseable") from exc
    if not isinstance(value, dict):
        raise BfiContractError("articleContext must be an object")
    return value


def parse_article_page(html: str) -> ArticlePage:
    """Parse a full BFI performance-listing HTML page into an :class:`ArticlePage`."""
    ctx = extract_article_context(html)
    return _parse_context_dict(ctx)


def _parse_context_dict(ctx: Mapping[str, object]) -> ArticlePage:
    names = _require_list(ctx, "searchNames")
    if len(names) != len(set(names)):
        raise BfiContractError("articleContext searchNames contains duplicate field names")

    results = _require_list(ctx, "searchResults")
    pagination = _require_dict(ctx, "pagination")
    article_id = _require_str(ctx, "articleId")
    _validate_guid(article_id, "articleId")
    s_token = _require_str(ctx, "sToken")
    current_page = _require_pagination_int(pagination, "current_page")
    total_pages = _require_pagination_int(pagination, "total_pages")

    rows: list[Mapping[str, object]] = []
    for i, raw_row in enumerate(results):
        if not isinstance(raw_row, list):
            raise BfiContractError(f"searchResults[{i}] is not a list")
        if len(raw_row) != len(names):
            raise BfiContractError(
                f"searchResults[{i}] has {len(raw_row)} values but searchNames has {len(names)}"
            )
        rows.append(dict(zip(names, raw_row, strict=True)))

    return ArticlePage(
        article_id=article_id,
        s_token=s_token,
        current_page=current_page,
        total_pages=total_pages,
        title=_page_title(rows),
        rows=tuple(rows),
    )


def _page_title(rows: Sequence[Mapping[str, object]]) -> str | None:
    """The film this page lists, taken from its first performance row.

    A live ``articleContext`` has no top-level ``title``; the film's name is repeated on
    every performance row. The first performance row is authoritative for the page, and
    :func:`cinema_friend.bfi.gateway.BfiGateway.list_performances` still checks that
    every page of the same listing agrees.
    """
    for row in rows:
        if row.get("object_type") == "P":
            return _row_title(row) or None
    return None


def performance_from_row(row: Mapping[str, object]) -> Performance:
    """Map a searchResults row (field→value mapping) to a :class:`Performance`.

    The field names are BFI's, read from a live capture: identity is ``id``, the seat
    count is ``availability_num`` (a decimal *string*), its meaning is
    ``availability_status``, reserved seating is option code ``2``, and the film's name
    is ``short_description`` with ``name`` as a fallback.

    Raises :class:`BfiContractError` if any required field is absent or has an
    unexpected type/value.
    """
    object_type = _require_str(row, "object_type")
    if object_type != "P":
        raise BfiContractError(
            f"object_type must be 'P' for a performance row; got {object_type!r}"
        )

    performance_id = _require_str(row, "id")
    start_date_raw = _require_str(row, "start_date")
    sales_status = _require_str(row, "sales_status")
    availability_status = _require_str(row, "availability_status")
    availability_num, availability_published = _availability(row, availability_status)
    options = _require_str_list(row, "options")

    try:
        start_london = datetime.strptime(start_date_raw, _START_FMT).replace(tzinfo=_LONDON)
    except ValueError as exc:
        raise BfiContractError(f"start_date is not parseable: {start_date_raw!r}") from exc
    start_utc = start_london.astimezone(UTC)

    seat_map = _seat_map_url(performance_id)

    return Performance(
        performance_id=performance_id,
        start_utc=start_utc,
        sales_status_code=sales_status,
        availability_status_code=availability_status,
        availability_num=availability_num,
        availability_published=availability_published,
        title=_row_title(row),
        seat_map_url=seat_map,
        options=tuple(options),
    )


def _row_title(row: Mapping[str, object]) -> str:
    """The film's display name for this row: ``short_description``, else ``name``.

    ``name`` is the booking system's internal slug (``thedog_e1_26aug26``), so it is a
    last resort rather than a preference -- but it is always populated, and a listing
    with no title at all reads as a bug to a user.
    """
    return _require_str(row, "short_description") or _require_str(row, "name")


def _availability(row: Mapping[str, object], availability_status: str) -> tuple[int, bool]:
    """Return ``(effective_count, published)`` for a row's ``availability_num``.

    BFI sends the count as a decimal string. ``-1`` is not a count: paired with base
    availability status ``U`` it is the site's way of saying the number is withheld, and
    it is reported as zero candidate seats with ``published=False``. A ``-1`` under any
    other status, or any other negative number, is unexplained and fails closed.
    """
    raw = _require_str(row, "availability_num")
    try:
        value = int(raw)
    except ValueError as exc:
        raise BfiContractError(f"availability_num is not an integer: {raw!r}") from exc
    if value >= 0:
        return value, True
    if (
        value == UNPUBLISHED_AVAILABILITY_SENTINEL
        and availability_status.rstrip("*") == UNPUBLISHED_AVAILABILITY_STATUS
    ):
        return 0, False
    raise BfiContractError(
        f"availability_num {value} is not a count and is not the unpublished sentinel "
        f"for availability_status {availability_status!r}"
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_guid(value: str, field: str) -> None:
    try:
        uuid.UUID(value)
    except ValueError as exc:
        raise BfiContractError(f"{field!r} is not a valid GUID: {value!r}") from exc


def _require_pagination_int(mapping: Mapping[str, object], key: str) -> int:
    raw = _require_str(mapping, key)
    try:
        return int(raw)
    except ValueError as exc:
        raise BfiContractError(f"pagination {key!r} is not a valid integer: {raw!r}") from exc


def _require_str(mapping: Mapping[str, object], key: str) -> str:
    if key not in mapping:
        raise BfiContractError(f"missing required field: {key!r}")
    value = mapping[key]
    if not isinstance(value, str):
        raise BfiContractError(f"{key!r} must be a string; got {type(value).__name__!r}")
    return value


def _require_list(mapping: Mapping[str, object], key: str) -> list[Any]:
    if key not in mapping:
        raise BfiContractError(f"missing required field: {key!r}")
    value = mapping[key]
    if not isinstance(value, list):
        raise BfiContractError(f"{key!r} must be a list; got {type(value).__name__!r}")
    return value


def _require_dict(mapping: Mapping[str, object], key: str) -> dict[str, object]:
    if key not in mapping:
        raise BfiContractError(f"missing required field: {key!r}")
    value = mapping[key]
    if not isinstance(value, dict):
        raise BfiContractError(f"{key!r} must be an object; got {type(value).__name__!r}")
    return value


def _require_str_list(mapping: Mapping[str, object], key: str) -> list[str]:
    raw = _require_list(mapping, key)
    for i, item in enumerate(raw):
        if not isinstance(item, str):
            raise BfiContractError(
                f"{key!r}[{i}] must be a string; got {type(item).__name__!r}"
            )
    return raw
