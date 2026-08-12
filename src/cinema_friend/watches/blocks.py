"""Adjacent purchasable seat-block generation."""

from __future__ import annotations

import logging
from collections import defaultdict

from cinema_friend.domain.bfi import Seat, SeatBlock, SeatMap, SeatStatus
from cinema_friend.domain.state import SeatPreferenceStrategy
from cinema_friend.domain.watch import WatchCriteria
from cinema_friend.watches.seat_banks import center_seat_bank, partition_seat_banks

logger = logging.getLogger(__name__)


def _is_purchasable(seat: Seat, criteria: WatchCriteria) -> bool:
    """Whether *seat* can be offered under *criteria*.

    Exclusions are matched on the seat's human label (``L3``), because that is what a
    user types; the venue's opaque ``seat_id`` is never something they could name.
    """
    return seat.status is SeatStatus.AVAILABLE and seat.label not in criteria.excluded_seats


def _emit_windows(
    blocks: list[SeatBlock],
    row: str,
    run: list[Seat],
    quantity: int,
) -> None:
    for start in range(len(run) - quantity + 1):
        blocks.append(SeatBlock(row=row, seats=tuple(run[start : start + quantity])))


def _generate_bank_blocks(
    row: str,
    bank: tuple[Seat, ...],
    criteria: WatchCriteria,
) -> list[SeatBlock]:
    blocks: list[SeatBlock] = []
    run: list[Seat] = []
    for seat in bank:
        if not _is_purchasable(seat, criteria):
            _emit_windows(blocks, row, run, criteria.quantity)
            run = []
            continue
        run.append(seat)
    _emit_windows(blocks, row, run, criteria.quantity)
    return blocks


def _generate_advanced_row_blocks(
    row: str,
    row_seats: list[Seat],
    criteria: WatchCriteria,
) -> list[SeatBlock]:
    if criteria.quantity == 1:
        return [
            SeatBlock(row=row, seats=(seat,))
            for seat in sorted(row_seats, key=lambda seat: seat.column)
            if _is_purchasable(seat, criteria)
        ]
    banks = partition_seat_banks(row_seats)
    if banks is None:
        return []
    return [
        block
        for bank in banks
        for block in _generate_bank_blocks(row, bank, criteria)
    ]


_MINIMUM_ROW = {
    SeatPreferenceStrategy.ONLY_BEST: "J",
    SeatPreferenceStrategy.BEST_AND_GOOD: "C",
}


def _simple_row_is_allowed(
    row: str,
    strategy: SeatPreferenceStrategy,
) -> bool | None:
    normalized = row.upper()
    if len(normalized) != 1 or not ("A" <= normalized <= "Z"):
        return None
    return normalized >= _MINIMUM_ROW[strategy]


def _generate_simple_blocks(
    seat_map: SeatMap,
    criteria: WatchCriteria,
) -> list[SeatBlock]:
    seats_by_row: dict[str, list[Seat]] = defaultdict(list)
    for seat in seat_map.seats:
        seats_by_row[seat.row].append(seat)

    blocks: list[SeatBlock] = []
    unsupported_rows: list[str] = []
    unresolved_rows: list[str] = []
    for row, row_seats in seats_by_row.items():
        row_allowed = _simple_row_is_allowed(
            row,
            criteria.seat_preference_strategy,
        )
        if row_allowed is None:
            unsupported_rows.append(row)
            continue
        if not row_allowed:
            continue
        bank = center_seat_bank(row_seats)
        if bank is None:
            unresolved_rows.append(row)
            continue
        blocks.extend(_generate_bank_blocks(row, bank, criteria))

    if unsupported_rows:
        logger.warning(
            "simple seat preference excluded unsupported row labels",
            extra={
                "performance_id": seat_map.performance_id,
                "rows": sorted(unsupported_rows),
                "strategy": criteria.seat_preference_strategy.value,
            },
        )
    if unresolved_rows:
        logger.warning(
            "simple seat preference could not identify a unique center bank",
            extra={
                "performance_id": seat_map.performance_id,
                "rows": sorted(unresolved_rows),
                "strategy": criteria.seat_preference_strategy.value,
            },
        )
    return blocks


def generate_blocks(seat_map: SeatMap, criteria: WatchCriteria) -> tuple[SeatBlock, ...]:
    """Generate every exact-size sliding-window seat block purchasable under
    *criteria* from *seat_map*.

    Advanced mode: Only ``AVAILABLE`` seats are offerable; sold, unavailable, contended,
    restricted, unknown-status, and explicitly excluded seats break a run of
    adjacent seats. Runs are also broken across an aisle (detected by an outsized gap
    in seat x-coordinates relative to the row's normal gap) and across a section
    boundary. Explicit row exclusions are respected.

    Simple modes (ONLY_BEST, BEST_AND_GOOD): Only seats in the interior seating bank of
    each row, at or behind the preset's minimum row, are candidates. The interior bank is
    the single non-edge bank containing the median x-coordinate of the row's physical
    seats, and only rows split into at least three banks qualify. Rows with ambiguous or
    insufficient geometry produce no blocks; each diagnostic is logged once per seat map.
    """
    if criteria.seat_preference_strategy is not SeatPreferenceStrategy.ADVANCED:
        return tuple(_generate_simple_blocks(seat_map, criteria))

    seats_by_section_row: dict[tuple[str, str], list[Seat]] = defaultdict(list)
    for seat in seat_map.seats:
        seats_by_section_row[(seat.section, seat.row)].append(seat)

    blocks: list[SeatBlock] = []
    for (_section, row), row_seats in seats_by_section_row.items():
        if row in criteria.excluded_rows:
            continue
        blocks.extend(_generate_advanced_row_blocks(row, row_seats, criteria))
    return tuple(blocks)
