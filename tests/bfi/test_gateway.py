"""Tests for cinema_friend.bfi.gateway."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from cinema_friend.bfi.gateway import AvailabilityDrift, BfiGateway
from cinema_friend.bfi.urls import film_page_url, pagination_url
from cinema_friend.domain.bfi import Performance
from cinema_friend.domain.errors import BfiContractError
from tests.factories.bfi_html import make_article_html, performance_row, seat_map_html
from tests.fakes import FakeClock, FakeTransport, fetched_document

SLUG = "dog-stars"
FILM_URL = film_page_url(SLUG)
ARTICLE_ID = "2152D1E8-CFF7-419F-BE57-F51C1E490F24"
TOKEN = "1,a/b+="
PAGE_2_URL = pagination_url(TOKEN, 2, ARTICLE_ID)
PAGE_3_URL = pagination_url(TOKEN, 3, ARTICLE_ID)

PERF_1 = "2475959F-2B73-4EA6-AD26-AFA8AEB785FD"
PERF_2 = "3586A6AF-3C84-4FA7-BE37-B0B9BFC8960E"
PERF_3 = "469BB7B0-4D95-4FB8-CF48-C1CAC0D9A70F"


def make_gateway(
    *, transport: FakeTransport | None = None, clock: FakeClock | None = None, cache_seconds: float = 60.0
) -> tuple[BfiGateway, FakeTransport, FakeClock]:
    fake_transport = transport if transport is not None else FakeTransport()
    fake_clock = clock if clock is not None else FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    return BfiGateway(fake_transport, fake_clock, cache_seconds=cache_seconds), fake_transport, fake_clock


def make_performance(**overrides: object) -> Performance:
    defaults: dict[str, object] = {
        "performance_id": PERF_1,
        "start_utc": datetime(2026, 8, 8, 13, 0, tzinfo=UTC),
        "sales_status_code": "OPEN",
        "availability_status_code": "E",
        "availability_num": 1,
        "seat_map_url": (
            "https://whatson.bfi.org.uk/imax/Online/mapSelect.asp"
            "?BOparam::WSmap::loadMap::performance_ids=2475959F-2B73-4EA6-AD26-AFA8AEB785FD"
        ),
        "options": ("1", "2"),
    }
    defaults.update(overrides)
    return Performance(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pagination assembly
# ---------------------------------------------------------------------------


async def test_lists_every_paginated_performance_and_deduplicates():
    page_1 = make_article_html(
        rows=[performance_row(id=PERF_1), performance_row(id=PERF_2)],
        current_page=1,
        total_pages=2,
        token=TOKEN,
        article_id=ARTICLE_ID,
    )
    page_2 = make_article_html(
        rows=[
            performance_row(id=PERF_2),  # exact duplicate: deduplicated
            performance_row(id=PERF_3),
        ],
        current_page=2,
        total_pages=2,
        token=TOKEN,
        article_id=ARTICLE_ID,
    )
    gateway, transport, _ = make_gateway(
        transport=FakeTransport(
            {
                FILM_URL: fetched_document(page_1, url=FILM_URL),
                PAGE_2_URL: fetched_document(page_2, url=PAGE_2_URL),
            }
        )
    )

    performances = await gateway.list_performances(SLUG)

    assert performances.title == "The Dog Stars"
    assert [item.performance_id for item in performances] == [PERF_1, PERF_2, PERF_3]
    assert transport.calls == [FILM_URL, PAGE_2_URL]


async def test_empty_second_page_contributes_no_performances():
    page_1 = make_article_html(
        rows=[performance_row(id=PERF_1)],
        total_pages=2,
        token=TOKEN,
        article_id=ARTICLE_ID,
    )
    page_2 = make_article_html(rows=[], current_page=2, total_pages=2, token=TOKEN, article_id=ARTICLE_ID)
    gateway, _, _ = make_gateway(
        transport=FakeTransport(
            {
                FILM_URL: fetched_document(page_1, url=FILM_URL),
                PAGE_2_URL: fetched_document(page_2, url=PAGE_2_URL),
            }
        )
    )

    performances = await gateway.list_performances(SLUG)

    assert [item.performance_id for item in performances] == [PERF_1]


async def test_three_page_listing_fetches_every_page_in_order():
    page_1 = make_article_html(
        rows=[performance_row(id=PERF_1)],
        total_pages=3,
        token=TOKEN,
        article_id=ARTICLE_ID,
    )
    page_2 = make_article_html(
        rows=[performance_row(id=PERF_2)],
        current_page=2,
        total_pages=3,
        token=TOKEN,
        article_id=ARTICLE_ID,
    )
    page_3 = make_article_html(
        rows=[performance_row(id=PERF_3)],
        current_page=3,
        total_pages=3,
        token=TOKEN,
        article_id=ARTICLE_ID,
    )
    gateway, transport, _ = make_gateway(
        transport=FakeTransport(
            {
                FILM_URL: fetched_document(page_1, url=FILM_URL),
                PAGE_2_URL: fetched_document(page_2, url=PAGE_2_URL),
                PAGE_3_URL: fetched_document(page_3, url=PAGE_3_URL),
            }
        )
    )

    performances = await gateway.list_performances(SLUG)

    assert [item.performance_id for item in performances] == [PERF_1, PERF_2, PERF_3]
    assert transport.calls == [FILM_URL, PAGE_2_URL, PAGE_3_URL]


async def test_changing_total_pages_across_pages_is_contract_error():
    page_1 = make_article_html(
        rows=[performance_row(id=PERF_1)],
        total_pages=2,
        token=TOKEN,
        article_id=ARTICLE_ID,
    )
    page_2 = make_article_html(
        rows=[performance_row(id=PERF_2)],
        current_page=2,
        total_pages=3,  # changed mid-pagination
        token=TOKEN,
        article_id=ARTICLE_ID,
    )
    gateway, _, _ = make_gateway(
        transport=FakeTransport(
            {
                FILM_URL: fetched_document(page_1, url=FILM_URL),
                PAGE_2_URL: fetched_document(page_2, url=PAGE_2_URL),
            }
        )
    )

    with pytest.raises(BfiContractError, match="total_pages"):
        await gateway.list_performances(SLUG)


async def test_changing_article_id_across_pages_is_contract_error():
    other_article_id = "3152D1E8-CFF7-419F-BE57-F51C1E490F24"
    page_1 = make_article_html(
        rows=[performance_row(id=PERF_1)],
        total_pages=2,
        token=TOKEN,
        article_id=ARTICLE_ID,
    )
    page_2 = make_article_html(
        rows=[performance_row(id=PERF_2)],
        current_page=2,
        total_pages=2,
        token=TOKEN,
        article_id=other_article_id,
    )
    gateway, _, _ = make_gateway(
        transport=FakeTransport(
            {
                FILM_URL: fetched_document(page_1, url=FILM_URL),
                PAGE_2_URL: fetched_document(page_2, url=PAGE_2_URL),
            }
        )
    )

    with pytest.raises(BfiContractError, match="article_id"):
        await gateway.list_performances(SLUG)


async def test_conflicting_page_titles_are_contract_error():
    page_1 = make_article_html(
        rows=[performance_row(id=PERF_1, short_description="Dog Stars")],
        total_pages=2,
        token=TOKEN,
        article_id=ARTICLE_ID,
    )
    page_2 = make_article_html(
        rows=[performance_row(id=PERF_2, short_description="Different Film")],
        current_page=2,
        total_pages=2,
        token=TOKEN,
        article_id=ARTICLE_ID,
    )
    gateway, _, _ = make_gateway(
        transport=FakeTransport(
            {
                FILM_URL: fetched_document(page_1, url=FILM_URL),
                PAGE_2_URL: fetched_document(page_2, url=PAGE_2_URL),
            }
        )
    )

    with pytest.raises(BfiContractError, match="title"):
        await gateway.list_performances(SLUG)


async def test_duplicate_id_with_conflicting_data_is_contract_error():
    page_1 = make_article_html(
        rows=[performance_row(id=PERF_1, availability_num="5")],
        total_pages=2,
        token=TOKEN,
        article_id=ARTICLE_ID,
    )
    page_2 = make_article_html(
        rows=[performance_row(id=PERF_1, availability_num="6")],
        current_page=2,
        total_pages=2,
        token=TOKEN,
        article_id=ARTICLE_ID,
    )
    gateway, _, _ = make_gateway(
        transport=FakeTransport(
            {
                FILM_URL: fetched_document(page_1, url=FILM_URL),
                PAGE_2_URL: fetched_document(page_2, url=PAGE_2_URL),
            }
        )
    )

    with pytest.raises(BfiContractError, match="conflicting"):
        await gateway.list_performances(SLUG)


async def test_invalid_row_field_propagates_as_contract_error():
    page_1 = make_article_html(
        rows=[performance_row(id=PERF_1, start_date="not a date")],
        total_pages=1,
        token=TOKEN,
        article_id=ARTICLE_ID,
    )
    gateway, _, _ = make_gateway(
        transport=FakeTransport({FILM_URL: fetched_document(page_1, url=FILM_URL)})
    )

    with pytest.raises(BfiContractError, match="start_date"):
        await gateway.list_performances(SLUG)


async def test_non_performance_rows_are_skipped():
    page_1 = make_article_html(
        rows=[
            performance_row(id=PERF_1, object_type="D"),
            performance_row(id=PERF_2),
        ],
        total_pages=1,
        token=TOKEN,
        article_id=ARTICLE_ID,
    )
    gateway, _, _ = make_gateway(
        transport=FakeTransport({FILM_URL: fetched_document(page_1, url=FILM_URL)})
    )

    performances = await gateway.list_performances(SLUG)

    assert [item.performance_id for item in performances] == [PERF_2]


# ---------------------------------------------------------------------------
# Single-flight + TTL coalescing
# ---------------------------------------------------------------------------


async def test_concurrent_equal_reads_share_one_request():
    page_1 = make_article_html(
        rows=[performance_row(id=PERF_1)],
        total_pages=1,
        token=TOKEN,
        article_id=ARTICLE_ID,
    )
    gateway, transport, _ = make_gateway(
        transport=FakeTransport({FILM_URL: fetched_document(page_1, url=FILM_URL)})
    )

    results = await asyncio.gather(
        gateway.list_performances(SLUG),
        gateway.list_performances(SLUG),
    )

    assert results[0] == results[1]
    assert transport.call_count(FILM_URL) == 1


async def test_cache_reuses_document_within_ttl_window():
    page_1 = make_article_html(
        rows=[performance_row(id=PERF_1)],
        total_pages=1,
        token=TOKEN,
        article_id=ARTICLE_ID,
    )
    gateway, transport, clock = make_gateway(
        transport=FakeTransport({FILM_URL: fetched_document(page_1, url=FILM_URL)}),
        cache_seconds=60.0,
    )

    await gateway.list_performances(SLUG)
    clock.monotonic_value += 30.0
    await gateway.list_performances(SLUG)

    assert transport.call_count(FILM_URL) == 1


async def test_cache_expires_after_ttl():
    page_1 = make_article_html(
        rows=[performance_row(id=PERF_1)],
        total_pages=1,
        token=TOKEN,
        article_id=ARTICLE_ID,
    )
    gateway, transport, clock = make_gateway(
        transport=FakeTransport(
            {FILM_URL: [fetched_document(page_1, url=FILM_URL), fetched_document(page_1, url=FILM_URL)]}
        ),
        cache_seconds=60.0,
    )

    await gateway.list_performances(SLUG)
    clock.monotonic_value += 61.0
    await gateway.list_performances(SLUG)

    assert transport.call_count(FILM_URL) == 2


async def test_failed_fetch_is_not_cached_and_inflight_is_cleaned_up():
    page_1 = make_article_html(
        rows=[performance_row(id=PERF_1)],
        total_pages=1,
        token=TOKEN,
        article_id=ARTICLE_ID,
    )
    gateway, transport, _ = make_gateway(
        transport=FakeTransport(
            {FILM_URL: [BfiContractError("boom"), fetched_document(page_1, url=FILM_URL)]}
        )
    )

    with pytest.raises(BfiContractError, match="boom"):
        await gateway.list_performances(SLUG)

    performances = await gateway.list_performances(SLUG)
    assert [item.performance_id for item in performances] == [PERF_1]
    assert transport.call_count(FILM_URL) == 2


async def test_concurrent_reads_where_first_fails_do_not_poison_the_second():
    """One caller's cancellation/failure must not corrupt in-flight bookkeeping for a sibling."""
    page_1 = make_article_html(
        rows=[performance_row(id=PERF_1)],
        total_pages=1,
        token=TOKEN,
        article_id=ARTICLE_ID,
    )
    gateway, transport, _ = make_gateway(
        transport=FakeTransport({FILM_URL: fetched_document(page_1, url=FILM_URL)})
    )

    results = await asyncio.gather(
        gateway.list_performances(SLUG),
        gateway.list_performances(SLUG),
        return_exceptions=True,
    )

    assert all(not isinstance(item, BaseException) for item in results)
    assert transport.call_count(FILM_URL) == 1


async def test_cancelling_one_reader_does_not_cancel_the_shared_fetch_for_a_sibling():
    """Cancelling one awaiter must not propagate into the shared in-flight task.

    `asyncio.Task.cancel()` cancels whatever future/task the caller is
    currently blocked on. Without `asyncio.shield`, a single cancelled
    reader sharing a coalesced fetch would tear down the underlying task
    out from under an uncancelled sibling relying on the same fetch.
    """
    page_1 = make_article_html(
        rows=[performance_row(id=PERF_1)],
        total_pages=1,
        token=TOKEN,
        article_id=ARTICLE_ID,
    )

    class SlowTransport:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.started = asyncio.Event()

        async def get(self, url: str, kind: object) -> object:
            self.calls.append(url)
            self.started.set()
            await asyncio.sleep(0.05)
            return fetched_document(page_1, url=url)

    transport = SlowTransport()
    gateway, _, _ = make_gateway(transport=transport)  # type: ignore[arg-type]

    cancelled_reader = asyncio.create_task(gateway.list_performances(SLUG))
    survivor_reader = asyncio.create_task(gateway.list_performances(SLUG))
    await transport.started.wait()
    cancelled_reader.cancel()

    with pytest.raises(asyncio.CancelledError):
        await cancelled_reader

    survivor_result = await survivor_reader
    assert [item.performance_id for item in survivor_result] == [PERF_1]
    assert transport.calls == [FILM_URL]  # only the single coalesced fetch


# ---------------------------------------------------------------------------
# Seat-map reads and availability cross-check
# ---------------------------------------------------------------------------


async def test_load_seat_map_returns_seat_map_matching_performance():
    performance = make_performance(availability_num=1)
    html = seat_map_html(performance_id=PERF_1)
    gateway, _, _ = make_gateway(
        transport=FakeTransport(
            {performance.seat_map_url: fetched_document(html, url=performance.seat_map_url)}
        )
    )

    seat_map = await gateway.load_seat_map(performance)

    assert seat_map.performance_id == PERF_1
    assert len(seat_map.seats) == 2


async def test_positive_reported_availability_with_zero_parsed_available_is_contract_error():
    performance = make_performance(availability_num=3)
    html = (
        "<html><script>"
        f'getPerformanceEcommerceObject({{"item_id":"{PERF_1}"}})'
        "</script><svg>"
        '<circle id="seat-1" data-status="S" data-seat-section="BFI IMAX" '
        'data-seat-row="L" data-seat-seat="1" cx="1" cy="1"/>'
        "</svg></html>"
    )
    gateway, _, _ = make_gateway(
        transport=FakeTransport(
            {performance.seat_map_url: fetched_document(html, url=performance.seat_map_url)}
        )
    )

    with pytest.raises(BfiContractError, match="zero available"):
        await gateway.load_seat_map(performance)


async def test_availability_drift_over_five_logs_but_does_not_raise(caplog: pytest.LogCaptureFixture):
    performance = make_performance(availability_num=10)
    html = seat_map_html(performance_id=PERF_1)  # 1 available, 1 sold seat -> difference 9
    gateway, _, _ = make_gateway(
        transport=FakeTransport(
            {performance.seat_map_url: fetched_document(html, url=performance.seat_map_url)}
        )
    )

    with caplog.at_level(logging.WARNING, logger="cinema_friend.bfi.gateway"):
        seat_map = await gateway.load_seat_map(performance)

    assert seat_map.performance_id == PERF_1
    assert any("drift" in record.message.lower() for record in caplog.records)


async def test_small_availability_drift_does_not_log():
    performance = make_performance(availability_num=1)
    html = seat_map_html(performance_id=PERF_1)  # 1 available seat -> difference 0
    gateway, _, _ = make_gateway(
        transport=FakeTransport(
            {performance.seat_map_url: fetched_document(html, url=performance.seat_map_url)}
        )
    )

    seat_map = await gateway.load_seat_map(performance)

    assert seat_map.performance_id == PERF_1


async def test_seat_map_identity_mismatch_propagates_contract_error():
    performance = make_performance(availability_num=1)
    html = seat_map_html(performance_id=PERF_2)  # mismatched performance ID
    gateway, _, _ = make_gateway(
        transport=FakeTransport(
            {performance.seat_map_url: fetched_document(html, url=performance.seat_map_url)}
        )
    )

    with pytest.raises(BfiContractError, match="performance ID mismatch"):
        await gateway.load_seat_map(performance)


async def test_load_seat_map_without_seat_map_url_raises_contract_error():
    performance = replace(make_performance(), seat_map_url=None)
    gateway, _, _ = make_gateway()

    with pytest.raises(BfiContractError, match="seat_map_url"):
        await gateway.load_seat_map(performance)


def test_availability_drift_is_a_plain_dataclass():
    drift = AvailabilityDrift(reported=5, parsed=3, difference=2)
    assert drift.reported == 5
    assert drift.parsed == 3
    assert drift.difference == 2
