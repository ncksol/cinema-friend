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
    SEARCH_NAMES,
    make_article_html,
    performance_mapping,
    performance_row,
    real_article_html,
)
from tests.fixtures import (
    FIXTURE_TOKEN,
    article_context_row_mappings,
    load_article_context,
)

# ---------------------------------------------------------------------------
# The captured live page — the schema of record
# ---------------------------------------------------------------------------


def test_capture_carries_no_live_token() -> None:
    """The committed capture must not contain the transient token it was served with."""
    assert load_article_context()["sToken"] == FIXTURE_TOKEN


def test_captured_search_names_are_the_wire_schema() -> None:
    """The fields the parser reads are the fields BFI actually sends.

    The previous schema was invented: ``performance_id``, ``event_id``,
    ``availability_code`` and ``reserved_seating`` appear nowhere in a live page. This
    test fails the moment any of them is reintroduced, and it reads the answer from the
    capture rather than restating it.
    """
    names = set(SEARCH_NAMES)
    assert {"id", "object_type", "start_date", "sales_status", "availability_status",
            "availability_num", "options", "short_description", "name"} <= names
    assert not names & {"performance_id", "event_id", "availability_code", "reserved_seating"}


def test_parses_the_captured_live_page() -> None:
    page = parse_article_page(real_article_html())
    context = load_article_context()
    assert page.article_id == context["articleId"]
    assert page.s_token == FIXTURE_TOKEN
    assert page.current_page == 1
    assert page.total_pages == 2
    assert len(page.rows) == len(context["searchResults"])


def test_every_captured_row_maps_to_a_performance() -> None:
    page = parse_article_page(real_article_html())
    captured = article_context_row_mappings()
    performances = [performance_from_row(row) for row in page.rows]

    assert [p.performance_id for p in performances] == [row["id"] for row in captured]
    assert [p.availability_num for p in performances] == [
        int(row["availability_num"]) for row in captured
    ]
    assert [p.availability_status_code for p in performances] == [
        row["availability_status"] for row in captured
    ]
    assert [p.sales_status_code for p in performances] == [row["sales_status"] for row in captured]
    assert [p.title for p in performances] == [row["short_description"] for row in captured]
    assert all(p.reserved_seating for p in performances)
    assert all(p.availability_published for p in performances)
    assert all(p.seat_map_url is not None and p.performance_id in p.seat_map_url
               for p in performances)


def test_listing_title_comes_from_the_rows_not_a_top_level_key() -> None:
    """A live articleContext has no ``title`` key; the film's name is on every row."""
    assert "title" not in load_article_context()
    page = parse_article_page(real_article_html())
    assert page.title == article_context_row_mappings()[0]["short_description"]


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
# Field mapping
# ---------------------------------------------------------------------------


def test_performance_id_comes_from_the_id_field() -> None:
    row = performance_mapping(id="9A5DBB0A-2F02-4B15-9E4F-8D2E6E9E1D77")
    assert performance_from_row(row).performance_id == "9A5DBB0A-2F02-4B15-9E4F-8D2E6E9E1D77"


def test_availability_status_is_carried_verbatim() -> None:
    row = performance_mapping(availability_status="L")
    assert performance_from_row(row).availability_status_code == "L"


def test_availability_status_base_strips_the_display_star() -> None:
    row = performance_mapping(availability_status="U*")
    performance = performance_from_row(row)
    assert performance.availability_status_code == "U*"
    assert performance.availability_status_base == "U"


def test_reserved_seating_is_derived_from_option_code_2() -> None:
    assert performance_from_row(performance_mapping(options=["1", "2"])).reserved_seating is True
    assert performance_from_row(performance_mapping(options=["2"])).reserved_seating is True


def test_absent_option_code_2_means_unreserved_seating() -> None:
    assert performance_from_row(performance_mapping(options=["1"])).reserved_seating is False
    assert performance_from_row(performance_mapping(options=[])).reserved_seating is False


def test_title_prefers_short_description() -> None:
    row = performance_mapping(short_description="The Dog Stars", name="thedog_e1_26aug26")
    assert performance_from_row(row).title == "The Dog Stars"


def test_title_falls_back_to_name_when_short_description_is_blank() -> None:
    row = performance_mapping(short_description="", name="thedog_e1_26aug26")
    assert performance_from_row(row).title == "thedog_e1_26aug26"


def test_availability_num_is_a_numeric_string_on_the_wire() -> None:
    assert performance_from_row(performance_mapping(availability_num="42")).availability_num == 42


def test_non_numeric_availability_is_a_contract_error() -> None:
    with pytest.raises(BfiContractError, match="availability_num"):
        performance_from_row(performance_mapping(availability_num="many"))


def test_availability_sent_as_a_json_number_is_a_contract_error() -> None:
    """BFI sends every scalar as a string; a bare number is drift, not a convenience."""
    with pytest.raises(BfiContractError, match="availability_num"):
        performance_from_row(performance_mapping(availability_num=42))


# ---------------------------------------------------------------------------
# The unpublished-availability sentinel
# ---------------------------------------------------------------------------


def test_unpublished_sentinel_is_accepted_with_status_u() -> None:
    """``availability_num = -1`` with status ``U`` means "count withheld", not "invalid".

    Observed live. It is not a negative seat count and must not be read as one: the
    count is simply not published, so the performance contributes no candidate seats.
    """
    row = performance_mapping(availability_status="U", availability_num="-1")
    performance = performance_from_row(row)
    assert performance.availability_published is False
    assert performance.availability_num == 0
    assert performance.availability_status_code == "U"


def test_unpublished_sentinel_is_accepted_with_starred_status_u() -> None:
    row = performance_mapping(availability_status="U*", availability_num="-1")
    performance = performance_from_row(row)
    assert performance.availability_published is False
    assert performance.availability_num == 0


def test_sentinel_with_any_other_status_is_a_contract_error() -> None:
    """Only ``U`` licenses the sentinel; anywhere else ``-1`` is unexplained drift."""
    row = performance_mapping(availability_status="E", availability_num="-1")
    with pytest.raises(BfiContractError, match="availability_num"):
        performance_from_row(row)


def test_other_negative_availability_is_a_contract_error() -> None:
    row = performance_mapping(availability_status="U", availability_num="-2")
    with pytest.raises(BfiContractError, match="availability_num"):
        performance_from_row(row)


def test_a_published_zero_is_published() -> None:
    row = performance_mapping(availability_status="U", availability_num="0")
    performance = performance_from_row(row)
    assert performance.availability_published is True
    assert performance.availability_num == 0


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

    duped = list(SEARCH_NAMES) + [SEARCH_NAMES[0]]
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
        "searchNames": list(SEARCH_NAMES),
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


def test_escaped_apostrophe_in_a_row_value_is_normalised() -> None:
    """A JS-escaped apostrophe in a string value must round-trip as a plain apostrophe."""
    html = make_article_html(rows=[performance_row(short_description="O'Brien's Dog")])
    page = parse_article_page(html)
    assert performance_from_row(page.rows[0]).title == "O'Brien's Dog"
