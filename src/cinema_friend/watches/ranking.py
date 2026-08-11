"""View-quality scoring and candidate ranking for adjacent seat blocks."""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence

from cinema_friend.domain.bfi import Performance, Seat, SeatBlock, SeatMap
from cinema_friend.domain.results import RankedOption, RankVector
from cinema_friend.domain.watch import WatchCriteria
from cinema_friend.watches.blocks import generate_blocks

_ROW_DISTANCE_REFERENCE_ROWS = ("L", "M")
_VIEW_SCORE_BAND_WIDTH = 5.0


def _row_order(all_physical_seats: Sequence[Seat]) -> list[str]:
    """Return row labels ordered by median cy (front-to-back seat order)."""
    cys_by_row: dict[str, list[float]] = defaultdict(list)
    for seat in all_physical_seats:
        cys_by_row[seat.row].append(seat.y)
    return sorted(cys_by_row, key=lambda row: statistics.median(cys_by_row[row]))


def _row_distance(row: str, ordered_rows: list[str]) -> float:
    """Number of observed-row steps from *row* to the nearest of L/M.

    Distances are ordinal (based on the row's position among the seat map's
    observed rows), not alphabetic, so skipped letters (e.g. no I or O row)
    do not distort the result.
    """
    index = {label: position for position, label in enumerate(ordered_rows)}
    row_index = index[row]
    reference_indices = [
        index[label] for label in _ROW_DISTANCE_REFERENCE_ROWS if label in index
    ]
    if reference_indices:
        return float(min(abs(row_index - reference) for reference in reference_indices))
    # No canonical L/M row is observed at all; fall back to distance from the
    # middle of the observed rows as the least-arbitrary reference point.
    return abs(row_index - (len(ordered_rows) - 1) / 2)


def score_block(block: SeatBlock, all_physical_seats: Sequence[Seat]) -> float:
    """Score *block* on a 0-100 view-quality scale.

    Combines a row-distance component (up to 40 points, decaying with
    distance from the dead-centre L/M rows) and a horizontal-centring
    component (up to 60 points, decaying with offset from the row's own
    centre).
    """
    ordered_rows = _row_order(all_physical_seats)
    row_distance = _row_distance(block.row, ordered_rows)
    row_score = max(0.0, 40.0 - 5.0 * row_distance)

    row_xs = [seat.x for seat in all_physical_seats if seat.row == block.row]
    min_x, max_x = min(row_xs), max(row_xs)
    row_center_x = (min_x + max_x) / 2
    half_width = (max_x - min_x) / 2
    block_center_x = sum(seat.x for seat in block.seats) / len(block.seats)
    normalized_offset = abs(block_center_x - row_center_x) / half_width if half_width else 0.0
    center_score = 60.0 * max(0.0, 1.0 - normalized_offset)

    return round(row_score + center_score, 2)


def _price_pence(block: SeatBlock) -> int | None:
    prices = {seat.zone.price for seat in block.seats if seat.zone is not None}
    if len(prices) != 1:
        return None
    (price,) = prices
    if price is None:
        return None
    return round(price * 100)


def _time_distance_minutes(criteria: WatchCriteria, performance: Performance) -> int:
    if criteria.preferred_utc_instant is None:
        return 0
    delta = abs(performance.start_utc - criteria.preferred_utc_instant)
    return int(delta.total_seconds() // 60)


def rank_options(
    criteria: WatchCriteria,
    performance_maps: Sequence[tuple[Performance, SeatMap]],
) -> tuple[RankedOption, ...]:
    """Generate and rank every adjacent seat-block option across performances.

    Options are ordered by :meth:`RankVector.sort_key`, with ties broken
    deterministically by performance ID and seat label so ordering never
    depends on the order of *performance_maps*.
    """
    options: list[RankedOption] = []
    for performance, seat_map in performance_maps:
        for block in generate_blocks(seat_map, criteria):
            raw_view_score = score_block(block, seat_map.seats)
            seat_ids = tuple(seat.seat_id for seat in block.seats)
            seat_label = "-".join(seat_ids)
            rank_vector = RankVector(
                preferred_seat_overlap=len(set(seat_ids) & criteria.preferred_seats),
                preferred_row_match=1 if block.row in criteria.preferred_rows else 0,
                view_score_band=int(raw_view_score // _VIEW_SCORE_BAND_WIDTH),
                preferred_time_distance_minutes=_time_distance_minutes(criteria, performance),
                raw_view_score=raw_view_score,
                performance_start=performance.start_utc,
                seat_label=seat_label,
            )
            options.append(
                RankedOption(
                    performance=performance,
                    seat_label=seat_label,
                    rank_vector=rank_vector,
                    price_pence=_price_pence(block),
                )
            )
    options.sort(
        key=lambda option: (
            *option.rank_vector.sort_key(),
            option.performance.performance_id,
            option.seat_label,
        )
    )
    return tuple(options)
