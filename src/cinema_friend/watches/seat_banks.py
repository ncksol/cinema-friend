"""Physical seat-bank geometry shared by adjacency and preference filtering."""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence
from itertools import pairwise

from cinema_friend.domain.bfi import Seat

_AISLE_THRESHOLD_MULTIPLIER = 1.75
_MIN_USABLE_GAPS = 3


def _normal_gap(section_seats: Sequence[Seat]) -> float | None:
    ordered = sorted(section_seats, key=lambda seat: seat.column)
    if (
        not ordered
        or max(seat.x for seat in ordered) - min(seat.x for seat in ordered)
        == 0
    ):
        return None
    gaps = [
        abs(right.x - left.x)
        for left, right in pairwise(ordered)
        if right.column == left.column + 1
    ]
    if len(gaps) < _MIN_USABLE_GAPS:
        return None
    return statistics.median(gaps)


def partition_seat_banks(
    row_seats: Sequence[Seat],
) -> tuple[tuple[Seat, ...], ...] | None:
    """Return aisle- and section-bounded physical banks, or None if geometry is weak."""
    if not row_seats:
        return None
    seats_by_section: dict[str, list[Seat]] = defaultdict(list)
    for seat in row_seats:
        seats_by_section[seat.section].append(seat)

    banks: list[tuple[Seat, ...]] = []
    for section in sorted(seats_by_section):
        ordered = sorted(seats_by_section[section], key=lambda seat: seat.column)
        normal_gap = _normal_gap(ordered)
        if normal_gap is None:
            return None
        threshold = _AISLE_THRESHOLD_MULTIPLIER * normal_gap
        current = [ordered[0]]
        for left, right in pairwise(ordered):
            if (
                right.column != left.column + 1
                or abs(right.x - left.x) > threshold
            ):
                banks.append(tuple(current))
                current = [right]
            else:
                current.append(right)
        banks.append(tuple(current))

    return tuple(
        sorted(
            banks,
            key=lambda bank: (
                min(seat.x for seat in bank),
                bank[0].section,
                bank[0].column,
            ),
        )
    )


_MIN_BANKS_FOR_INTERIOR = 3


def center_seat_bank(row_seats: Sequence[Seat]) -> tuple[Seat, ...] | None:
    """Return the unique interior bank holding the row's median seat, or None.

    Simple mode promises seats between the aisles, so this fails closed unless the row
    offers positive evidence of an interior bank: at least three physical banks, and
    exactly one non-edge bank whose horizontal span contains the median x-coordinate of
    every physical seat in the row (available or not). A single bank, two banks, a median
    that lands in an aisle or in an edge bank, and any ambiguity all return ``None``.
    The median is used rather than a min/max midpoint so a detached side cluster cannot
    drag the selection onto an outer bank.
    """
    banks = partition_seat_banks(row_seats)
    if banks is None or len(banks) < _MIN_BANKS_FOR_INTERIOR:
        return None
    row_median = statistics.median(seat.x for seat in row_seats)
    winners = [
        bank
        for bank in banks[1:-1]
        if min(seat.x for seat in bank) <= row_median <= max(seat.x for seat in bank)
    ]
    return winners[0] if len(winners) == 1 else None
