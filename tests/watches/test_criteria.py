"""Tests for cinema_friend.watches.criteria."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time

from cinema_friend.domain.bfi import RESERVED_SEATING_OPTION, Performance
from cinema_friend.domain.state import WatchMode
from cinema_friend.domain.watch import WatchCriteria
from cinema_friend.watches.criteria import performance_matches

SLUG = "dog-stars"
PERF_ID = "2475959F-2B73-4EA6-AD26-AFA8AEB785FD"


def criteria_for(
    *,
    date_from: date = date(2026, 8, 26),
    date_to: date = date(2026, 8, 27),
    time_from: time = time(0, 0),
    time_to: time = time(23, 59),
    weekend_time_from: time | None = None,
    weekend_time_to: time | None = None,
    quantity: int = 2,
) -> WatchCriteria:
    return WatchCriteria(
        source_url="https://whatson.bfi.org.uk/imax/Online/default.asp?doWork::WScontent::loadArticle=Load&BOparam::WScontent::loadArticle::article_id=2152D1E8-CFF7-419F-BE57-F51C1E490F24",
        slug=SLUG,
        date_from=date_from,
        date_to=date_to,
        time_from=time_from,
        time_to=time_to,
        quantity=quantity,
        mode=WatchMode.ONE_OFF,
        weekend_time_from=weekend_time_from,
        weekend_time_to=weekend_time_to,
    )


def performance_at(
    iso_instant: str,
    *,
    sales_status_code: str = "S",
    availability_num: int = 50,
    reserved_seating: bool = True,
) -> Performance:
    return Performance(
        performance_id=PERF_ID,
        start_utc=datetime.fromisoformat(iso_instant).astimezone(UTC),
        sales_status_code=sales_status_code,
        availability_status_code="E",
        availability_num=availability_num,
        seat_map_url="https://whatson.bfi.org.uk/imax/Online/mapSelect.asp",
        options=(RESERVED_SEATING_OPTION,) if reserved_seating else (),
    )


def test_midnight_window_uses_performance_local_date() -> None:
    criteria = criteria_for(
        date_from=date(2026, 8, 26),
        date_to=date(2026, 8, 27),
        time_from=time(22, 0),
        time_to=time(1, 0),
    )
    assert performance_matches(criteria, performance_at("2026-08-26T23:30:00+01:00"))
    assert performance_matches(criteria, performance_at("2026-08-27T00:30:00+01:00"))
    assert not performance_matches(criteria, performance_at("2026-08-27T14:00:00+01:00"))


def test_midnight_window_boundaries_are_inclusive() -> None:
    """Both ends of a wrapping window match, exactly as both ends of a plain one do."""
    criteria = criteria_for(
        date_from=date(2026, 8, 26),
        date_to=date(2026, 8, 27),
        time_from=time(22, 0),
        time_to=time(1, 0),
    )
    assert performance_matches(criteria, performance_at("2026-08-26T22:00:00+01:00"))
    assert performance_matches(criteria, performance_at("2026-08-27T01:00:00+01:00"))
    assert not performance_matches(criteria, performance_at("2026-08-26T21:59:00+01:00"))
    assert not performance_matches(criteria, performance_at("2026-08-27T01:01:00+01:00"))


def test_date_outside_range_does_not_match() -> None:
    criteria = criteria_for(date_from=date(2026, 8, 26), date_to=date(2026, 8, 26))
    assert not performance_matches(criteria, performance_at("2026-08-27T18:00:00+01:00"))


def test_time_boundaries_are_inclusive() -> None:
    criteria = criteria_for(time_from=time(18, 0), time_to=time(20, 0))
    assert performance_matches(criteria, performance_at("2026-08-26T18:00:00+01:00"))
    assert performance_matches(criteria, performance_at("2026-08-26T20:00:00+01:00"))
    assert not performance_matches(criteria, performance_at("2026-08-26T17:59:00+01:00"))
    assert not performance_matches(criteria, performance_at("2026-08-26T20:01:00+01:00"))


def test_on_sale_base_codes_s_o_r_all_match() -> None:
    criteria = criteria_for()
    for code in ("S", "O", "R", "S*", "O*", "R*"):
        assert performance_matches(criteria, performance_at(
            "2026-08-26T18:00:00+01:00", sales_status_code=code
        )), code


def test_non_on_sale_code_does_not_match() -> None:
    criteria = criteria_for()
    for code in ("X", "N", "C"):
        assert not performance_matches(criteria, performance_at(
            "2026-08-26T18:00:00+01:00", sales_status_code=code
        )), code


def test_non_reserved_seating_does_not_match() -> None:
    criteria = criteria_for()
    assert not performance_matches(
        criteria, performance_at("2026-08-26T18:00:00+01:00", reserved_seating=False)
    )


def test_availability_below_quantity_does_not_match() -> None:
    criteria = criteria_for(quantity=4)
    assert not performance_matches(
        criteria, performance_at("2026-08-26T18:00:00+01:00", availability_num=3)
    )
    assert performance_matches(
        criteria, performance_at("2026-08-26T18:00:00+01:00", availability_num=4)
    )


def test_preferred_utc_instant_does_not_affect_matching() -> None:
    base = criteria_for()
    criteria = WatchCriteria(
        source_url=base.source_url,
        slug=base.slug,
        date_from=base.date_from,
        date_to=base.date_to,
        time_from=base.time_from,
        time_to=base.time_to,
        quantity=base.quantity,
        mode=base.mode,
        preferred_utc_instant=datetime(2026, 8, 26, 17, 0, tzinfo=UTC),
    )
    assert performance_matches(criteria, performance_at("2026-08-26T18:00:00+01:00"))


def test_split_schedule_matches_each_day_category() -> None:
    criteria = criteria_for(
        date_from=date(2026, 8, 28),
        date_to=date(2026, 8, 30),
        time_from=time(18, 0),
        time_to=time(23, 0),
        weekend_time_from=time(12, 0),
        weekend_time_to=time(16, 0),
    )

    assert performance_matches(criteria, performance_at("2026-08-28T19:00:00+01:00"))
    assert performance_matches(criteria, performance_at("2026-08-29T13:00:00+01:00"))
    assert not performance_matches(criteria, performance_at("2026-08-29T19:00:00+01:00"))


def test_split_wrapping_schedule_is_selected_by_performance_start_day() -> None:
    criteria = criteria_for(
        date_from=date(2026, 8, 28),
        date_to=date(2026, 8, 29),
        time_from=time(22, 0),
        time_to=time(1, 0),
        weekend_time_from=time(12, 0),
        weekend_time_to=time(16, 0),
    )

    assert performance_matches(criteria, performance_at("2026-08-28T23:30:00+01:00"))
    assert not performance_matches(criteria, performance_at("2026-08-29T00:30:00+01:00"))


# ---------------------------------------------------------------------------
# Fix round 2 -- unpublished availability
# ---------------------------------------------------------------------------


def test_an_unpublished_count_never_satisfies_a_quantity() -> None:
    """A withheld count is not evidence of seats, so no quantity can be met by it.

    The parser normalises BFI's ``-1`` sentinel to zero, so this holds for the smallest
    request the domain allows without ``performance_matches`` needing its own special
    case.
    """
    performance = replace(
        performance_at("2026-08-26T18:15:00+01:00"),
        availability_num=0,
        availability_published=False,
        availability_status_code="U",
    )
    assert not performance_matches(criteria_for(quantity=1), performance)


def test_a_published_count_of_one_satisfies_a_request_for_one() -> None:
    """The eligibility test is ``>= quantity``, so an exact match is enough."""
    performance = performance_at("2026-08-26T18:15:00+01:00", availability_num=1)
    assert performance_matches(criteria_for(quantity=1), performance)
