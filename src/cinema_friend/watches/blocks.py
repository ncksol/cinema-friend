"""Adjacent purchasable seat-block generation."""

from __future__ import annotations

import statistics
from collections import defaultdict

from cinema_friend.domain.bfi import Seat, SeatBlock, SeatMap, SeatStatus
from cinema_friend.domain.watch import WatchCriteria

_AISLE_THRESHOLD_MULTIPLIER = 1.75
_MIN_USABLE_GAPS = 3


def _is_purchasable(seat: Seat, criteria: WatchCriteria) -> bool:
    return seat.status is SeatStatus.AVAILABLE and seat.seat_id not in criteria.excluded_seats


def _median_gap(row_seats_sorted: list[Seat]) -> float | None:
    """Return the row's normal seat-to-seat gap, or None if there is not
    enough geometric evidence (fewer than three usable gaps, or zero width).
    """
    xs = [seat.x for seat in row_seats_sorted]
    if max(xs) - min(xs) == 0:
        return None
    gaps = [
        abs(row_seats_sorted[i + 1].x - row_seats_sorted[i].x)
        for i in range(len(row_seats_sorted) - 1)
        if row_seats_sorted[i + 1].column == row_seats_sorted[i].column + 1
    ]
    if len(gaps) < _MIN_USABLE_GAPS:
        return None
    return statistics.median(gaps)


def _emit_windows(
    blocks: list[SeatBlock], row: str, run: list[Seat], quantity: int
) -> None:
    for start in range(len(run) - quantity + 1):
        blocks.append(SeatBlock(row=row, seats=tuple(run[start : start + quantity])))


def _generate_row_blocks(
    row: str, row_seats: list[Seat], criteria: WatchCriteria
) -> list[SeatBlock]:
    quantity = criteria.quantity
    row_seats_sorted = sorted(row_seats, key=lambda s: s.column)

    if quantity == 1:
        return [
            SeatBlock(row=row, seats=(seat,))
            for seat in row_seats_sorted
            if _is_purchasable(seat, criteria)
        ]

    median_gap = _median_gap(row_seats_sorted)
    if median_gap is None:
        return []
    threshold = _AISLE_THRESHOLD_MULTIPLIER * median_gap

    blocks: list[SeatBlock] = []
    run: list[Seat] = []
    for seat in row_seats_sorted:
        if not _is_purchasable(seat, criteria):
            _emit_windows(blocks, row, run, quantity)
            run = []
            continue
        can_extend = bool(run) and (
            seat.column == run[-1].column + 1 and abs(seat.x - run[-1].x) <= threshold
        )
        if run and not can_extend:
            _emit_windows(blocks, row, run, quantity)
            run = [seat]
        else:
            run.append(seat)
    _emit_windows(blocks, row, run, quantity)
    return blocks


def generate_blocks(seat_map: SeatMap, criteria: WatchCriteria) -> tuple[SeatBlock, ...]:
    """Generate every exact-size sliding-window seat block purchasable under
    *criteria* from *seat_map*.

    Only ``AVAILABLE`` seats are offerable; sold, unavailable, contended,
    restricted, unknown-status, and explicitly excluded seats break a run of
    adjacent seats. Runs are also broken across an aisle, detected by an
    outsized gap in seat x-coordinates relative to the row's normal
    (median) seat-to-seat gap.
    """
    seats_by_row: dict[str, list[Seat]] = defaultdict(list)
    for seat in seat_map.seats:
        seats_by_row[seat.row].append(seat)

    blocks: list[SeatBlock] = []
    for row, row_seats in seats_by_row.items():
        if row in criteria.excluded_rows:
            continue
        blocks.extend(_generate_row_blocks(row, row_seats, criteria))
    return tuple(blocks)
