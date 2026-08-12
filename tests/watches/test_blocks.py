"""Tests for cinema_friend.watches.blocks."""

from __future__ import annotations

import logging
from datetime import date, time

import pytest

from cinema_friend.domain.bfi import PriceZone, Seat, SeatMap, SeatStatus
from cinema_friend.domain.state import SeatPreferenceStrategy, WatchMode
from cinema_friend.domain.watch import WatchCriteria
from cinema_friend.watches.blocks import generate_blocks

_ZONE = PriceZone(zone_id="z1", label="Premium", price=None)


def criteria_for(
    *,
    quantity: int = 2,
    excluded_seats: frozenset[str] = frozenset(),
    excluded_rows: frozenset[str] = frozenset(),
    seat_preference_strategy: SeatPreferenceStrategy = SeatPreferenceStrategy.ADVANCED,
) -> WatchCriteria:
    return WatchCriteria(
        source_url="https://whatson.bfi.org.uk/imax/Online/default.asp",
        slug="dog-stars",
        date_from=date(2026, 8, 26),
        date_to=date(2026, 8, 27),
        time_from=time(0, 0),
        time_to=time(23, 59),
        quantity=quantity,
        mode=WatchMode.ONE_OFF,
        excluded_seats=excluded_seats,
        excluded_rows=excluded_rows,
        seat_preference_strategy=seat_preference_strategy,
    )


def seat(
    row: str,
    column: int,
    x: float,
    *,
    status: SeatStatus = SeatStatus.AVAILABLE,
    y: float = 100.0,
    section: str = "BFI IMAX",
    seat_id: str | None = None,
) -> Seat:
    return Seat(
        seat_id=seat_id if seat_id is not None else f"{row}{column}",
        raw_status_code="A" if status is SeatStatus.AVAILABLE else "S",
        status=status,
        zone=_ZONE,
        note="",
        section=section,
        row=row,
        column=column,
        x=x,
        y=y,
    )


def _row_with_aisle() -> list[Seat]:
    # Numbers 1..5, x = 100, 114, 128, 180, 194 -> gaps 14,14,52,14 -> median 14,
    # threshold 24.5, aisle break between seat 3 (x=128) and seat 4 (x=180).
    xs = [100.0, 114.0, 128.0, 180.0, 194.0]
    return [seat("L", i + 1, x) for i, x in enumerate(xs)]


def test_aisle_gap_splits_row_into_separate_runs() -> None:
    seat_map = SeatMap(performance_id="p1", seats=tuple(_row_with_aisle()))
    blocks = generate_blocks(seat_map, criteria_for(quantity=2))
    labels = {tuple(s.seat_id for s in block.seats) for block in blocks}
    assert labels == {("L1", "L2"), ("L2", "L3"), ("L4", "L5")}
    assert ("L3", "L4") not in labels


def test_restricted_seat_breaks_a_run() -> None:
    xs = [100.0, 114.0, 128.0, 142.0, 156.0]
    seats = [seat("L", i + 1, x) for i, x in enumerate(xs)]
    seats[2] = seat("L", 3, xs[2], status=SeatStatus.RESTRICTED)
    seat_map = SeatMap(performance_id="p1", seats=tuple(seats))
    blocks = generate_blocks(seat_map, criteria_for(quantity=2))
    labels = {tuple(s.seat_id for s in block.seats) for block in blocks}
    assert labels == {("L1", "L2"), ("L4", "L5")}


def test_sold_seat_breaks_a_run() -> None:
    xs = [100.0, 114.0, 128.0, 142.0, 156.0]
    seats = [seat("L", i + 1, x) for i, x in enumerate(xs)]
    seats[2] = seat("L", 3, xs[2], status=SeatStatus.SOLD)
    seat_map = SeatMap(performance_id="p1", seats=tuple(seats))
    blocks = generate_blocks(seat_map, criteria_for(quantity=2))
    labels = {tuple(s.seat_id for s in block.seats) for block in blocks}
    assert labels == {("L1", "L2"), ("L4", "L5")}


def test_unavailable_seat_breaks_a_run() -> None:
    xs = [100.0, 114.0, 128.0, 142.0, 156.0]
    seats = [seat("L", i + 1, x) for i, x in enumerate(xs)]
    seats[2] = seat("L", 3, xs[2], status=SeatStatus.UNAVAILABLE)
    seat_map = SeatMap(performance_id="p1", seats=tuple(seats))
    blocks = generate_blocks(seat_map, criteria_for(quantity=2))
    labels = {tuple(s.seat_id for s in block.seats) for block in blocks}
    assert labels == {("L1", "L2"), ("L4", "L5")}


def test_contended_seat_breaks_a_run() -> None:
    xs = [100.0, 114.0, 128.0, 142.0, 156.0]
    seats = [seat("L", i + 1, x) for i, x in enumerate(xs)]
    seats[2] = seat("L", 3, xs[2], status=SeatStatus.CONTENDED)
    seat_map = SeatMap(performance_id="p1", seats=tuple(seats))
    blocks = generate_blocks(seat_map, criteria_for(quantity=2))
    labels = {tuple(s.seat_id for s in block.seats) for block in blocks}
    assert labels == {("L1", "L2"), ("L4", "L5")}


def test_excluded_seat_breaks_a_run() -> None:
    xs = [100.0, 114.0, 128.0, 142.0, 156.0]
    seats = [seat("L", i + 1, x) for i, x in enumerate(xs)]
    seat_map = SeatMap(performance_id="p1", seats=tuple(seats))
    blocks = generate_blocks(
        seat_map, criteria_for(quantity=2, excluded_seats=frozenset({"L3"}))
    )
    labels = {tuple(s.seat_id for s in block.seats) for block in blocks}
    assert labels == {("L1", "L2"), ("L4", "L5")}


def test_excluded_seats_are_matched_by_human_label_not_by_opaque_seat_id() -> None:
    # A user types "L3"; the BFI seat map identifies that seat by an opaque GUID.
    xs = [100.0, 114.0, 128.0, 142.0, 156.0]
    seats = [
        seat("L", i + 1, x, seat_id=f"1FA0A9C8-0000-4000-8000-00000000000{i + 1}")
        for i, x in enumerate(xs)
    ]
    seat_map = SeatMap(performance_id="p1", seats=tuple(seats))

    blocks = generate_blocks(
        seat_map, criteria_for(quantity=2, excluded_seats=frozenset({"L3"}))
    )

    labels = {tuple(s.label for s in block.seats) for block in blocks}
    assert labels == {("L1", "L2"), ("L4", "L5")}


def test_excluded_row_yields_no_blocks() -> None:
    xs = [100.0, 114.0, 128.0, 142.0, 156.0]
    seats = [seat("L", i + 1, x) for i, x in enumerate(xs)]
    seat_map = SeatMap(performance_id="p1", seats=tuple(seats))
    blocks = generate_blocks(seat_map, criteria_for(quantity=2, excluded_rows=frozenset({"L"})))
    assert blocks == ()


def test_five_seat_run_yields_four_two_seat_windows() -> None:
    xs = [100.0, 114.0, 128.0, 142.0, 156.0]
    seats = [seat("L", i + 1, x) for i, x in enumerate(xs)]
    seat_map = SeatMap(performance_id="p1", seats=tuple(seats))
    blocks = generate_blocks(seat_map, criteria_for(quantity=2))
    labels = [tuple(s.seat_id for s in block.seats) for block in blocks]
    assert labels == [
        ("L1", "L2"),
        ("L2", "L3"),
        ("L3", "L4"),
        ("L4", "L5"),
    ]


def test_quantity_one_yields_every_available_seat_ignoring_geometry() -> None:
    seats = [
        seat("L", 1, 100.0),
        seat("L", 2, 250.0, status=SeatStatus.SOLD),
        seat("L", 3, 400.0),
    ]
    seat_map = SeatMap(performance_id="p1", seats=tuple(seats))
    blocks = generate_blocks(seat_map, criteria_for(quantity=1))
    labels = {tuple(s.seat_id for s in block.seats) for block in blocks}
    assert labels == {("L1",), ("L3",)}


def test_row_with_too_few_gaps_yields_no_multi_seat_blocks() -> None:
    # Only two seats -> a single gap; not enough evidence for a median.
    seats = [seat("L", 1, 100.0), seat("L", 2, 114.0)]
    seat_map = SeatMap(performance_id="p1", seats=tuple(seats))
    blocks = generate_blocks(seat_map, criteria_for(quantity=2))
    assert blocks == ()


def test_quantity_larger_than_run_yields_no_blocks() -> None:
    # Four seats -> three usable gaps (14, 14, 14), enough for a median, but the
    # run itself (length 4) is shorter than the requested quantity (5).
    xs = [100.0, 114.0, 128.0, 142.0]
    seats = [seat("L", i + 1, x) for i, x in enumerate(xs)]
    seat_map = SeatMap(performance_id="p1", seats=tuple(seats))
    blocks = generate_blocks(seat_map, criteria_for(quantity=5))
    assert blocks == ()


# ---------------------------------------------------------------------------
# Fix round 1: section identity gates adjacency, independent of row/geometry
# ---------------------------------------------------------------------------


def test_different_sections_same_row_never_form_a_cross_section_block() -> None:
    # Row "L" spans two sections. Seats L5 (section A) and L6 (section B) are
    # consecutively numbered with a normal in-section gap (14) between them,
    # so a row/geometry-only algorithm would treat them as adjacent. Section
    # identity must prevent that: L5-L6 must never appear as a block, while
    # each section still forms its own internal blocks.
    section_a_xs = [100.0, 114.0, 128.0, 142.0, 156.0]
    section_a = [seat("L", i + 1, x, section="Section A") for i, x in enumerate(section_a_xs)]
    section_b_xs = [170.0, 184.0, 198.0, 212.0, 226.0]
    section_b = [seat("L", i + 6, x, section="Section B") for i, x in enumerate(section_b_xs)]
    seat_map = SeatMap(performance_id="p1", seats=tuple(section_a + section_b))
    blocks = generate_blocks(seat_map, criteria_for(quantity=2))
    labels = {tuple(s.seat_id for s in block.seats) for block in blocks}

    assert ("L5", "L6") not in labels
    assert all(
        {s.section for s in block.seats} == {section}
        for block in blocks
        for section in [block.seats[0].section]
    )
    # Each section still independently forms its own adjacent-seat blocks.
    assert ("L4", "L5") in labels
    assert ("L6", "L7") in labels


# ---------------------------------------------------------------------------
# Task 2: Simple preset hard-filter tests
# ---------------------------------------------------------------------------


def _three_bank_row(row: str) -> list[Seat]:
    return [
        seat(row, column, x)
        for column, x in enumerate(
            [0.0, 10.0, 20.0, 70.0, 80.0, 90.0, 140.0, 150.0, 160.0],
            start=1,
        )
    ]


@pytest.mark.parametrize(
    ("strategy", "front_row", "cutoff_row", "back_row"),
    [
        (SeatPreferenceStrategy.ONLY_BEST, "I", "J", "K"),
        (SeatPreferenceStrategy.BEST_AND_GOOD, "B", "C", "D"),
    ],
)
def test_simple_strategy_keeps_only_the_center_bank_at_and_behind_its_cutoff(
    strategy: SeatPreferenceStrategy,
    front_row: str,
    cutoff_row: str,
    back_row: str,
) -> None:
    seats = [
        *_three_bank_row(front_row),
        *_three_bank_row(cutoff_row),
        *_three_bank_row(back_row),
    ]
    seat_map = SeatMap(performance_id="p1", seats=tuple(seats))

    blocks = generate_blocks(
        seat_map,
        criteria_for(quantity=2, seat_preference_strategy=strategy),
    )

    assert {(block.row, tuple(seat.column for seat in block.seats)) for block in blocks} == {
        (cutoff_row, (4, 5)),
        (cutoff_row, (5, 6)),
        (back_row, (4, 5)),
        (back_row, (5, 6)),
    }


def test_simple_quantity_one_still_requires_the_center_bank() -> None:
    seat_map = SeatMap(performance_id="p1", seats=tuple(_three_bank_row("J")))

    blocks = generate_blocks(
        seat_map,
        criteria_for(
            quantity=1,
            seat_preference_strategy=SeatPreferenceStrategy.ONLY_BEST,
        ),
    )

    assert {block.seats[0].label for block in blocks} == {"J4", "J5", "J6"}


def test_simple_strategy_fails_closed_and_logs_when_center_geometry_is_ambiguous(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seats = [
        seat("J", column, x)
        for column, x in enumerate(
            [0.0, 10.0, 20.0, 30.0, 70.0, 80.0, 90.0, 100.0],
            start=1,
        )
    ]
    seat_map = SeatMap(performance_id="p1", seats=tuple(seats))

    with caplog.at_level(logging.WARNING):
        blocks = generate_blocks(
            seat_map,
            criteria_for(
                quantity=1,
                seat_preference_strategy=SeatPreferenceStrategy.ONLY_BEST,
            ),
        )

    assert blocks == ()
    assert "could not identify a unique center bank" in caplog.text


def test_simple_strategy_fails_closed_and_logs_when_geometry_is_insufficient(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seat_map = SeatMap(
        performance_id="p1",
        seats=(
            seat("J", 1, 0.0),
            seat("J", 2, 10.0),
            seat("J", 3, 20.0),
        ),
    )

    with caplog.at_level(logging.WARNING):
        blocks = generate_blocks(
            seat_map,
            criteria_for(
                quantity=1,
                seat_preference_strategy=SeatPreferenceStrategy.ONLY_BEST,
            ),
        )

    assert blocks == ()
    assert "could not identify a unique center bank" in caplog.text


def test_simple_strategy_fails_closed_and_logs_for_an_unsupported_row_label(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seat_map = SeatMap(performance_id="p1", seats=tuple(_three_bank_row("AA")))

    with caplog.at_level(logging.WARNING):
        blocks = generate_blocks(
            seat_map,
            criteria_for(
                quantity=1,
                seat_preference_strategy=SeatPreferenceStrategy.BEST_AND_GOOD,
            ),
        )

    assert blocks == ()
    assert "unsupported row label" in caplog.text


def test_simple_strategy_aggregates_each_diagnostic_once_per_seat_map(
    caplog: pytest.LogCaptureFixture,
) -> None:
    unresolved_rows = [
        seat(row, column, x)
        for row in ("J", "K")
        for column, x in enumerate([0.0, 10.0, 20.0, 30.0, 70.0, 80.0, 90.0, 100.0], start=1)
    ]
    unsupported_rows = [
        *_three_bank_row("AA"),
        *_three_bank_row("BB"),
    ]
    seat_map = SeatMap(
        performance_id="p1",
        seats=tuple(unresolved_rows + unsupported_rows),
    )

    with caplog.at_level(logging.WARNING):
        blocks = generate_blocks(
            seat_map,
            criteria_for(
                quantity=1,
                seat_preference_strategy=SeatPreferenceStrategy.ONLY_BEST,
            ),
        )

    assert blocks == ()
    unsupported_warnings = [
        record for record in caplog.records if "unsupported row label" in record.getMessage()
    ]
    unresolved_warnings = [
        record
        for record in caplog.records
        if "could not identify a unique center bank" in record.getMessage()
    ]
    assert len(unsupported_warnings) == 1
    assert len(unresolved_warnings) == 1
    assert sorted(unsupported_warnings[0].rows) == ["AA", "BB"]
    assert sorted(unresolved_warnings[0].rows) == ["J", "K"]
