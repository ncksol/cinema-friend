"""Parse BFI ``articleContext`` script blocks and performance pages."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from cinema_friend.bfi.urls import seat_map_url as _seat_map_url
from cinema_friend.domain.bfi import Performance
from cinema_friend.domain.errors import BfiContractError

_LONDON = ZoneInfo("Europe/London")
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
    s_token = _require_str(ctx, "sToken")
    current_page = int(_require_str(pagination, "current_page"))
    total_pages = int(_require_str(pagination, "total_pages"))

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
        rows=tuple(rows),
    )


def performance_from_row(row: Mapping[str, object]) -> Performance:
    """Map a searchResults row (field→value mapping) to a :class:`Performance`.

    Raises :class:`BfiContractError` if any required field is absent or has an
    unexpected type/value.
    """
    object_type = _require_str(row, "object_type")
    if object_type != "P":
        raise BfiContractError(
            f"object_type must be 'P' for a performance row; got {object_type!r}"
        )

    performance_id = _require_str(row, "performance_id")
    event_id = _require_str(row, "event_id")
    start_date_raw = _require_str(row, "start_date")
    sales_status = _require_str(row, "sales_status")
    availability_code = _require_str(row, "availability_code")
    availability_num = _require_int(row, "availability_num")
    reserved_seating = _require_bool(row, "reserved_seating")
    options = _require_str_list(row, "options")

    start_london = datetime.strptime(start_date_raw, _START_FMT).replace(tzinfo=_LONDON)
    start_utc = start_london.astimezone(UTC)

    seat_map = _seat_map_url(performance_id)

    return Performance(
        performance_id=performance_id,
        event_id=event_id,
        start_utc=start_utc,
        sales_status_code=sales_status,
        availability_code=availability_code,
        availability_num=max(0, availability_num),
        reserved_seating=reserved_seating,
        seat_map_url=seat_map,
        options=tuple(options),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_str(mapping: Mapping[str, object], key: str) -> str:
    if key not in mapping:
        raise BfiContractError(f"missing required field: {key!r}")
    value = mapping[key]
    if not isinstance(value, str):
        raise BfiContractError(f"{key!r} must be a string; got {type(value).__name__!r}")
    return value


def _require_int(mapping: Mapping[str, object], key: str) -> int:
    if key not in mapping:
        raise BfiContractError(f"missing required field: {key!r}")
    value = mapping[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise BfiContractError(f"{key!r} must be an integer; got {type(value).__name__!r}")
    return value


def _require_bool(mapping: Mapping[str, object], key: str) -> bool:
    if key not in mapping:
        raise BfiContractError(f"missing required field: {key!r}")
    value = mapping[key]
    if not isinstance(value, bool):
        raise BfiContractError(f"{key!r} must be a boolean; got {type(value).__name__!r}")
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
