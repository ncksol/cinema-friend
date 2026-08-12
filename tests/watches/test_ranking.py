"""Tests for cinema_friend.watches.ranking."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from cinema_friend.domain.bfi import Performance, PriceZone, Seat, SeatBlock, SeatMap, SeatStatus
from cinema_friend.domain.state import WatchMode
from cinema_friend.domain.watch import WatchCriteria
from cinema_friend.watches.ranking import rank_options, score_block

_ZONE = PriceZone(zone_id="z1", label="Premium", price=None)


def seat(
    row: str,
    column: int,
    x: float,
    *,
    y: float = 100.0,
    status: SeatStatus = SeatStatus.AVAILABLE,
    section: str = "BFI IMAX",
    seat_id: str | None = None,
    zone: PriceZone | None = _ZONE,
) -> Seat:
    return Seat(
        seat_id=seat_id if seat_id is not None else f"{section}/{row}{column}",
        raw_status_code="A" if status is SeatStatus.AVAILABLE else "S",
        status=status,
        zone=zone,
        note="",
        section=section,
        row=row,
        column=column,
        x=x,
        y=y,
    )


def block_for(row: str, columns: list[int], xs: list[float]) -> SeatBlock:
    return SeatBlock(
        row=row, seats=tuple(seat(row, c, x) for c, x in zip(columns, xs, strict=True))
    )


def symmetric_row(row: str, *, start_x: float, end_x: float, count: int = 20) -> tuple[Seat, ...]:
    step = (end_x - start_x) / (count - 1)
    return tuple(seat(row, i + 1, start_x + i * step) for i in range(count))


# ---------------------------------------------------------------------------
# score_block
# ---------------------------------------------------------------------------


def test_dead_centre_row_l_scores_100() -> None:
    block = block_for("L", [17, 18], xs=[343, 357])
    all_seats = symmetric_row("L", start_x=70, end_x=630)
    assert score_block(block, all_seats) == 100.0


def test_dead_centre_row_m_also_scores_100() -> None:
    block = block_for("M", [17, 18], xs=[343, 357])
    all_seats = symmetric_row("M", start_x=70, end_x=630)
    assert score_block(block, all_seats) == 100.0


def test_off_centre_block_reduces_score() -> None:
    # row_center=350, half_width=280; block centred at x=(105+119)/2=112 ->
    # normalized_offset=(350-112)/280=0.85 -> center_score=60*0.15=9.0; row_score
    # is 40 (row L, distance 0) -> total 49.0.
    block = block_for("L", [1, 2], xs=[105, 119])
    all_seats = symmetric_row("L", start_x=70, end_x=630)
    assert score_block(block, all_seats) == 49.0


def test_row_distance_uses_observed_row_order_not_alphabet() -> None:
    # Rows H, J, L are observed (I and K are skipped, as real BFI seat maps do).
    # Ordinal order is H(0), J(1), L(2); row H is therefore 2 steps from the
    # nearest of L/M (L), not 4 as an alphabet-distance calculation would give.
    row_h = tuple(seat("H", i + 1, 100.0 + i * 14.0, y=10.0) for i in range(4))
    row_j = tuple(seat("J", i + 1, 100.0 + i * 14.0, y=20.0) for i in range(4))
    row_l = tuple(seat("L", i + 1, 100.0 + i * 14.0, y=30.0) for i in range(4))
    all_seats = row_h + row_j + row_l

    block_h = block_for("H", [2, 3], xs=[114.0, 128.0])
    block_j = block_for("J", [2, 3], xs=[114.0, 128.0])

    # Both blocks are dead-centre in their own row (row width 100..142, centre
    # 121); only the row component of the score should differ.
    assert score_block(block_h, all_seats) == 90.0  # (40 - 5*2) + 60
    assert score_block(block_j, all_seats) == 95.0  # (40 - 5*1) + 60


# ---------------------------------------------------------------------------
# rank_options
# ---------------------------------------------------------------------------

_SLUG = "dog-stars"
_URL = "https://whatson.bfi.org.uk/imax/Online/default.asp"


def criteria_with_preferred_time(
    *,
    preferred_utc_instant: datetime,
    quantity: int = 1,
    preferred_seats: frozenset[str] = frozenset(),
    preferred_rows: frozenset[str] = frozenset(),
) -> WatchCriteria:
    return WatchCriteria(
        source_url=_URL,
        slug=_SLUG,
        date_from=date(2026, 8, 26),
        date_to=date(2026, 8, 27),
        time_from=time(0, 0),
        time_to=time(23, 59),
        quantity=quantity,
        mode=WatchMode.ONE_OFF,
        preferred_seats=preferred_seats,
        preferred_rows=preferred_rows,
        preferred_utc_instant=preferred_utc_instant,
    )


def performance(performance_id: str, start_utc: datetime) -> Performance:
    return Performance(
        performance_id=performance_id,
        event_id="E8A1B2C3-D4E5-F6A7-B8C9-D0E1F2A3B4C5",
        start_utc=start_utc,
        sales_status_code="S",
        availability_code="A",
        availability_num=50,
        reserved_seating=True,
        seat_map_url="https://whatson.bfi.org.uk/imax/Online/mapSelect.asp",
    )


def _single_row_map(performance_id: str, *, target_x: float) -> SeatMap:
    seats = (
        seat("L", 1, 0.0, status=SeatStatus.SOLD),
        seat("L", 2, 600.0, status=SeatStatus.SOLD),
        seat("L", 3, target_x, status=SeatStatus.AVAILABLE),
    )
    return SeatMap(performance_id=performance_id, seats=seats)


def maps_with_scores(
    score_a: float, score_b: float, score_c: float, *, preferred_start: datetime
) -> tuple[tuple[Performance, SeatMap], ...]:
    # row_score is fixed at 40 (single row "L", distance 0); solving
    # center_score = 60*(1 - offset/half_width) for offset with half_width=300
    # gives offset = (100 - score) * 5.
    perf_a = performance("PERF-AAAA0000-0000-0000-0000-000000000001", preferred_start + timedelta(minutes=30))
    perf_b = performance("PERF-BBBB0000-0000-0000-0000-000000000002", preferred_start + timedelta(minutes=5))
    perf_c = performance("PERF-CCCC0000-0000-0000-0000-000000000003", preferred_start + timedelta(minutes=1000))
    return (
        (perf_a, _single_row_map(perf_a.performance_id, target_x=300.0 + (100.0 - score_a) * 5.0)),
        (perf_b, _single_row_map(perf_b.performance_id, target_x=300.0 + (100.0 - score_b) * 5.0)),
        (perf_c, _single_row_map(perf_c.performance_id, target_x=300.0 + (100.0 - score_c) * 5.0)),
    )


def test_preferred_time_distance_is_measured_from_the_utc_instant() -> None:
    """A London-local preference must reach ranking as the instant it really names.

    ``20:00`` on 27 August in London is ``19:00Z``; the performance starting at that
    instant is the closest one, and would not be if the local reading were treated as
    UTC.
    """
    preferred_utc = datetime(2026, 8, 27, 20, 0, tzinfo=ZoneInfo("Europe/London")).astimezone(UTC)
    assert preferred_utc == datetime(2026, 8, 27, 19, 0, tzinfo=UTC)
    criteria = criteria_with_preferred_time(preferred_utc_instant=preferred_utc)
    at_preference = performance("A-PERF", datetime(2026, 8, 27, 19, 0, tzinfo=UTC))
    an_hour_later = performance("B-PERF", datetime(2026, 8, 27, 20, 0, tzinfo=UTC))
    maps = (
        (at_preference, _single_row_map("A-PERF", target_x=305.0)),
        (an_hour_later, _single_row_map("B-PERF", target_x=305.0)),
    )

    ranked = rank_options(criteria, maps)

    assert [option.performance.performance_id for option in ranked] == ["A-PERF", "B-PERF"]
    assert [option.rank_vector.preferred_time_distance_minutes for option in ranked] == [0, 60]


def test_time_breaks_only_within_same_five_point_band() -> None:
    preferred_start = datetime(2026, 8, 26, 18, 0, tzinfo=UTC)
    criteria = criteria_with_preferred_time(preferred_utc_instant=preferred_start)
    performance_maps = maps_with_scores(99.0, 96.0, 94.0, preferred_start=preferred_start)
    ranked = rank_options(criteria, performance_maps)
    assert [option.rank_vector.raw_view_score for option in ranked] == [96.0, 99.0, 94.0]


def test_preferred_seat_overlap_outranks_raw_score() -> None:
    # Rows "L" and "M" are both dead-centre (row distance 0, identical raw
    # score); only seats 2/3 are purchasable in each (1/4 are sold and only
    # used to establish row width/median-gap geometry).
    xs = [100.0, 114.0, 128.0, 142.0]
    row_l = tuple(
        seat("L", i + 1, x, status=SeatStatus.SOLD if i in (0, 3) else SeatStatus.AVAILABLE)
        for i, x in enumerate(xs)
    )
    row_m = tuple(
        seat("M", i + 1, x, status=SeatStatus.SOLD if i in (0, 3) else SeatStatus.AVAILABLE)
        for i, x in enumerate(xs)
    )
    seat_map = SeatMap(performance_id="p1", seats=row_l + row_m)
    perf = performance("p1", datetime(2026, 8, 26, 18, 0, tzinfo=UTC))
    criteria = criteria_with_preferred_time(
        preferred_utc_instant=datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
        quantity=2,
        preferred_seats=frozenset({"M2", "M3"}),
    )
    ranked = rank_options(criteria, ((perf, seat_map),))
    assert [option.seat_label for option in ranked] == ["M2-M3", "L2-L3"]


def test_preferred_row_outranks_raw_score_when_no_seat_overlap() -> None:
    # Row "K" is one ordinal step from "L" (row_score 35); row "L" is dead
    # centre (row_score 40). Both blocks are dead-centre in their own row, so
    # without the preferred-row boost L (raw score 100) would rank first.
    xs = [100.0, 114.0, 128.0, 142.0]
    row_k = tuple(
        seat("K", i + 1, x, y=10.0, status=SeatStatus.SOLD if i in (0, 3) else SeatStatus.AVAILABLE)
        for i, x in enumerate(xs)
    )
    row_l = tuple(
        seat("L", i + 1, x, y=20.0, status=SeatStatus.SOLD if i in (0, 3) else SeatStatus.AVAILABLE)
        for i, x in enumerate(xs)
    )
    seat_map = SeatMap(performance_id="p1", seats=row_k + row_l)
    perf = performance("p1", datetime(2026, 8, 26, 18, 0, tzinfo=UTC))
    criteria = criteria_with_preferred_time(
        preferred_utc_instant=datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
        quantity=2,
        preferred_rows=frozenset({"K"}),
    )
    ranked = rank_options(criteria, ((perf, seat_map),))
    assert [option.seat_label for option in ranked] == ["K2-K3", "L2-L3"]


def test_ties_break_deterministically_on_performance_id_then_seat_key() -> None:
    start = datetime(2026, 8, 26, 18, 0, tzinfo=UTC)
    criteria = criteria_with_preferred_time(preferred_utc_instant=start)
    perf_b = performance("B-PERF", start)
    perf_a = performance("A-PERF", start)
    map_b = _single_row_map("B-PERF", target_x=305.0)
    map_a = _single_row_map("A-PERF", target_x=305.0)
    # Input order deliberately places B before A; every RankVector field ties
    # (same score, same overlap/row-match, same start), so only the explicit
    # performance-id tiebreak can make the ordering deterministic.
    ranked = rank_options(criteria, ((perf_b, map_b), (perf_a, map_a)))
    assert [option.performance.performance_id for option in ranked] == ["A-PERF", "B-PERF"]


# ---------------------------------------------------------------------------
# Seat identity: human-readable label vs stable seat IDs
# ---------------------------------------------------------------------------


def _adjacent_pair_map(
    performance_id: str,
    *,
    section: str = "BFI IMAX",
    row: str = "L",
    seat_ids: tuple[str, str] | None = None,
    zone: PriceZone | None = _ZONE,
) -> SeatMap:
    """Row of four seats where only columns 2 and 3 are purchasable."""
    xs = [100.0, 114.0, 128.0, 142.0]
    seats = []
    for index, x in enumerate(xs):
        column = index + 1
        override: str | None = None
        if seat_ids is not None and column in (2, 3):
            override = seat_ids[column - 2]
        seats.append(
            seat(
                row,
                column,
                x,
                section=section,
                seat_id=override,
                zone=zone,
                status=SeatStatus.SOLD if index in (0, 3) else SeatStatus.AVAILABLE,
            )
        )
    return SeatMap(performance_id=performance_id, seats=tuple(seats))


def _pair_criteria(**overrides: object) -> WatchCriteria:
    return criteria_with_preferred_time(
        preferred_utc_instant=datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
        quantity=2,
        **overrides,  # type: ignore[arg-type]
    )


def test_seat_label_is_human_readable_row_and_seat_numbers_not_seat_ids() -> None:
    perf = performance("p1", datetime(2026, 8, 26, 18, 0, tzinfo=UTC))
    seat_map = _adjacent_pair_map(
        "p1",
        seat_ids=(
            "1FA0A9C8-1111-4000-8000-000000000002",
            "1FA0A9C8-2222-4000-8000-000000000003",
        ),
    )

    (ranked,) = rank_options(_pair_criteria(), ((perf, seat_map),))

    assert ranked.seat_label == "L2-L3"


def test_seat_ids_preserve_the_stable_bfi_identifiers_in_block_order() -> None:
    perf = performance("p1", datetime(2026, 8, 26, 18, 0, tzinfo=UTC))
    seat_map = _adjacent_pair_map("p1", seat_ids=("guid-second", "guid-third"))

    (ranked,) = rank_options(_pair_criteria(), ((perf, seat_map),))

    assert ranked.seat_ids == ("guid-second", "guid-third")
    assert ranked.key == "p1:guid-second|guid-third"
    assert ranked.rank_vector.seat_key == "guid-second|guid-third"


def test_identical_row_numbers_in_two_sections_share_a_label_but_not_a_key() -> None:
    perf = performance("p1", datetime(2026, 8, 26, 18, 0, tzinfo=UTC))
    stalls = _adjacent_pair_map(
        "p1", section="Stalls", seat_ids=("stalls-2", "stalls-3")
    )
    balcony = _adjacent_pair_map(
        "p1", section="Balcony", seat_ids=("balcony-2", "balcony-3")
    )
    seat_map = SeatMap(performance_id="p1", seats=stalls.seats + balcony.seats)

    ranked = rank_options(_pair_criteria(), ((perf, seat_map),))

    assert {option.seat_label for option in ranked} == {"L2-L3"}
    assert len({option.key for option in ranked}) == len(ranked) == 2


def test_preferred_seat_overlap_compares_human_labels_not_seat_ids() -> None:
    perf = performance("p1", datetime(2026, 8, 26, 18, 0, tzinfo=UTC))
    seat_map = _adjacent_pair_map("p1", seat_ids=("opaque-guid-a", "opaque-guid-b"))

    (ranked,) = rank_options(
        _pair_criteria(preferred_seats=frozenset({"L2"})), ((perf, seat_map),)
    )

    assert ranked.rank_vector.preferred_seat_overlap == 1


def test_seat_categories_carry_the_distinct_price_zone_labels() -> None:
    perf = performance("p1", datetime(2026, 8, 26, 18, 0, tzinfo=UTC))
    seat_map = _adjacent_pair_map("p1")

    (ranked,) = rank_options(_pair_criteria(), ((perf, seat_map),))

    assert ranked.seat_categories == ("Premium",)


def test_seat_categories_are_empty_when_no_zone_is_known() -> None:
    perf = performance("p1", datetime(2026, 8, 26, 18, 0, tzinfo=UTC))
    seat_map = _adjacent_pair_map("p1", zone=None)

    (ranked,) = rank_options(_pair_criteria(), ((perf, seat_map),))

    assert ranked.seat_categories == ()


def test_ranked_options_carry_the_listing_title_when_one_is_known() -> None:
    perf = performance("p1", datetime(2026, 8, 26, 18, 0, tzinfo=UTC))
    seat_map = _adjacent_pair_map("p1")

    (ranked,) = rank_options(_pair_criteria(), ((perf, seat_map),), title="Dog Stars")

    assert ranked.title == "Dog Stars"


def test_ranked_options_have_no_title_when_the_listing_did_not_name_the_film() -> None:
    perf = performance("p1", datetime(2026, 8, 26, 18, 0, tzinfo=UTC))
    seat_map = _adjacent_pair_map("p1")

    (ranked,) = rank_options(_pair_criteria(), ((perf, seat_map),))

    assert ranked.title is None
