"""Tests for BFI articleContext parsing."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from cinema_friend.bfi.article_context import (
    extract_article_context,
    parse_article_page,
    performance_from_row,
)
from cinema_friend.domain.errors import BfiContractError
from tests.factories.bfi_html import (
    REQUIRED_FIELDS,
    make_article_html,
    performance_mapping,
    performance_row,
)

# ---------------------------------------------------------------------------
# Core round-trip
# ---------------------------------------------------------------------------


def test_parses_page_and_maps_performance() -> None:
    html = make_article_html(rows=[performance_row()])
    page = parse_article_page(html)
    performance = performance_from_row(page.rows[0])
    assert page.total_pages == 1
    assert performance.performance_id == "2475959F-2B73-4EA6-AD26-AFA8AEB785FD"
    assert performance.start.tzinfo == ZoneInfo("Europe/London")
    assert performance.availability_num == 387
    assert performance.reserved_seating is True


def test_missing_consumed_field_is_contract_error() -> None:
    row = performance_mapping()
    row.pop("availability_num")
    with pytest.raises(BfiContractError, match="availability_num"):
        performance_from_row(row)


# ---------------------------------------------------------------------------
# ArticlePage fields
# ---------------------------------------------------------------------------


def test_article_page_pagination() -> None:
    html = make_article_html(rows=[performance_row()], current_page=2, total_pages=5)
    page = parse_article_page(html)
    assert page.current_page == 2
    assert page.total_pages == 5


def test_article_page_carries_article_id_and_token() -> None:
    html = make_article_html(rows=[performance_row()], token="1,a/b+=")
    page = parse_article_page(html)
    assert page.article_id == "2152D1E8-CFF7-419F-BE57-F51C1E490F24"
    assert page.s_token == "1,a/b+="


def test_article_page_rows_count() -> None:
    html = make_article_html(rows=[performance_row(), performance_row()])
    page = parse_article_page(html)
    assert len(page.rows) == 2


# ---------------------------------------------------------------------------
# extract_article_context — JavaScript normalisation
# ---------------------------------------------------------------------------


def test_extract_article_context_bare_key() -> None:
    html = make_article_html(rows=[performance_row()])
    ctx = extract_article_context(html)
    assert "searchNames" in ctx


def test_extract_article_context_trailing_comma() -> None:
    # The factory emits a trailing comma; parse must succeed.
    html = make_article_html(rows=[performance_row()])
    ctx = extract_article_context(html)
    assert isinstance(ctx, dict)


def test_extract_article_context_missing_raises() -> None:
    with pytest.raises(BfiContractError, match="articleContext not found"):
        extract_article_context("<html><body>no script here</body></html>")


# ---------------------------------------------------------------------------
# DST boundary
# ---------------------------------------------------------------------------


def test_dst_ambiguous_time_retains_london_zone() -> None:
    # 01:30 on 25 Oct 2026 is in the BST→GMT transition hour.
    html = make_article_html(
        rows=[performance_row(start_date="Sunday 25 October 2026 01:30")]
    )
    page = parse_article_page(html)
    perf = performance_from_row(page.rows[0])
    assert perf.start.tzinfo == ZoneInfo("Europe/London")


# ---------------------------------------------------------------------------
# Duplicate / malformed rows
# ---------------------------------------------------------------------------


def test_duplicate_searchnames_raises_contract_error() -> None:
    # Craft HTML that embeds a searchNames list with a duplicate entry.
    import json

    duped = list(REQUIRED_FIELDS) + [REQUIRED_FIELDS[0]]
    ctx = {
        "searchNames": duped,
        "searchResults": [performance_row()],
        "pagination": {"current_page": "1", "page_size": "5", "total_pages": "1"},
        "articleId": "2152D1E8-CFF7-419F-BE57-F51C1E490F24",
        "sToken": "tok",
    }
    html = f"<script>var articleContext = {json.dumps(ctx)};\n</script>"
    with pytest.raises(BfiContractError, match="duplicate"):
        parse_article_page(html)


def test_row_shorter_than_searchnames_raises_contract_error() -> None:
    # Craft HTML where a row has fewer entries than searchNames.
    import json

    ctx = {
        "searchNames": list(REQUIRED_FIELDS),
        "searchResults": [performance_row()[:-1]],  # one value short
        "pagination": {"current_page": "1", "page_size": "5", "total_pages": "1"},
        "articleId": "2152D1E8-CFF7-419F-BE57-F51C1E490F24",
        "sToken": "tok",
    }
    html = f"<script>var articleContext = {json.dumps(ctx)};\n</script>"
    with pytest.raises(BfiContractError):
        parse_article_page(html)


# ---------------------------------------------------------------------------
# Non-performance object_type
# ---------------------------------------------------------------------------


def test_non_performance_object_type_rejected_by_performance_from_row() -> None:
    row = performance_mapping(object_type="A")
    with pytest.raises(BfiContractError, match="object_type"):
        performance_from_row(row)


# ---------------------------------------------------------------------------
# sales_status with trailing star
# ---------------------------------------------------------------------------


def test_sales_status_star_retained_as_raw_and_base() -> None:
    row = performance_mapping(sales_status="S*")
    perf = performance_from_row(row)
    assert perf.sales_status_code == "S*"


def test_sales_status_base_stripped_of_star() -> None:
    row = performance_mapping(sales_status="S*")
    perf = performance_from_row(row)
    # base code (without trailing *) is accessible via the sales_status_base property
    assert perf.sales_status_base == "S"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# seat_map_url construction
# ---------------------------------------------------------------------------


def test_seat_map_url_is_constructed_from_performance_id() -> None:
    row = performance_mapping()
    perf = performance_from_row(row)
    assert perf.seat_map_url is not None
    assert "2475959F-2B73-4EA6-AD26-AFA8AEB785FD" in perf.seat_map_url


# ---------------------------------------------------------------------------
# options normalised to tuple
# ---------------------------------------------------------------------------


def test_options_normalised_to_tuple_of_strings() -> None:
    row = performance_mapping(options=["Subtitles", "Relaxed"])
    perf = performance_from_row(row)
    assert perf.options == ("Subtitles", "Relaxed")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Fix round 1 — fail-closed validation and apostrophe path
# ---------------------------------------------------------------------------


def test_invalid_article_id_raises_contract_error() -> None:
    """articleId that is not a valid GUID must raise BfiContractError."""
    html = make_article_html(rows=[performance_row()], article_id="not-a-guid")
    with pytest.raises(BfiContractError, match="articleId"):
        parse_article_page(html)


def test_non_integer_current_page_raises_contract_error() -> None:
    """Non-integer current_page must raise BfiContractError, not ValueError."""
    html = make_article_html(rows=[performance_row()], current_page="bad")
    with pytest.raises(BfiContractError):
        parse_article_page(html)


def test_non_integer_total_pages_raises_contract_error() -> None:
    """Non-integer total_pages must raise BfiContractError, not ValueError."""
    html = make_article_html(rows=[performance_row()], total_pages="bad")
    with pytest.raises(BfiContractError):
        parse_article_page(html)


def test_malformed_start_date_raises_contract_error() -> None:
    """Unparseable start_date must raise BfiContractError, not ValueError."""
    row = performance_mapping(start_date="not a date")
    with pytest.raises(BfiContractError, match="start_date"):
        performance_from_row(row)


def test_negative_availability_raises_contract_error() -> None:
    """Negative availability_num must raise BfiContractError, not be clamped."""
    row = performance_mapping(availability_num=-1)
    with pytest.raises(BfiContractError, match="availability_num"):
        performance_from_row(row)


def test_escaped_apostrophe_in_title_normalised() -> None:
    """A JS-escaped apostrophe in a string value must survive round-trip as a plain apostrophe."""
    html = make_article_html(rows=[performance_row()], title_with_apostrophe="O'Brien")
    ctx = extract_article_context(html)
    assert ctx.get("title") == "O'Brien"
