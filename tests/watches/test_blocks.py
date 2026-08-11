"""Tests for cinema_friend.watches.blocks."""

from __future__ import annotations

from datetime import date, time

from cinema_friend.domain.bfi import PriceZone, Seat, SeatMap, SeatStatus
from cinema_friend.domain.state import WatchMode
from cinema_friend.domain.watch import WatchCriteria
from cinema_friend.watches.blocks import generate_blocks

_ZONE = PriceZone(zone_id="z1", label="Premium", price=None)


def criteria_for(
    *,
    quantity: int = 2,
    excluded_seats: frozenset[str] = frozenset(),
    excluded_rows: frozenset[str] = frozenset(),
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
    )


def seat(
    row: str,
    column: int,
    x: float,
    *,
    status: SeatStatus = SeatStatus.AVAILABLE,
    y: float = 100.0,
) -> Seat:
    return Seat(
        seat_id=f"{row}{column}",
        raw_status_code="A" if status is SeatStatus.AVAILABLE else "S",
        status=status,
        zone=_ZONE,
        note="",
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
