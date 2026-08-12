from __future__ import annotations

from cinema_friend.domain.bfi import PriceZone, Seat, SeatStatus
from cinema_friend.watches.seat_banks import (
    center_seat_bank,
    partition_seat_banks,
)

_ZONE = PriceZone(zone_id="z1", label="Premium", price=None)


def seat(
    column: int,
    x: float,
    *,
    section: str = "BFI IMAX",
    status: SeatStatus = SeatStatus.AVAILABLE,
) -> Seat:
    return Seat(
        seat_id=f"{section}-{column}",
        raw_status_code="A" if status is SeatStatus.AVAILABLE else "S",
        status=status,
        zone=_ZONE,
        note="",
        section=section,
        row="J",
        column=column,
        x=x,
        y=100.0,
    )


def test_partition_seat_banks_splits_on_oversized_aisle_gaps() -> None:
    row = [
        seat(column, x)
        for column, x in enumerate(
            [0.0, 10.0, 20.0, 70.0, 80.0, 90.0, 140.0, 150.0, 160.0],
            start=1,
        )
    ]

    banks = partition_seat_banks(row)

    assert banks is not None
    assert [[seat.column for seat in bank] for bank in banks] == [
        [1, 2, 3],
        [4, 5, 6],
        [7, 8, 9],
    ]
    center = center_seat_bank(row)
    assert center is not None
    assert [seat.column for seat in center] == [4, 5, 6]


def test_center_seat_bank_uses_unavailable_seats_to_hold_the_physical_layout() -> None:
    row = [
        seat(
            column,
            x,
            status=SeatStatus.SOLD if column == 5 else SeatStatus.AVAILABLE,
        )
        for column, x in enumerate(
            [0.0, 10.0, 20.0, 70.0, 80.0, 90.0, 140.0, 150.0, 160.0],
            start=1,
        )
    ]

    center = center_seat_bank(row)

    assert center is not None
    assert [seat.column for seat in center] == [4, 5, 6]


def test_partition_seat_banks_splits_on_section_boundaries() -> None:
    row = [
        *[seat(index, x, section="Left") for index, x in enumerate([0.0, 10.0, 20.0, 30.0], 1)],
        *[seat(index, x, section="Center") for index, x in enumerate([70.0, 80.0, 90.0, 100.0], 1)],
        *[seat(index, x, section="Right") for index, x in enumerate([140.0, 150.0, 160.0, 170.0], 1)],
    ]

    center = center_seat_bank(row)

    assert center is not None
    assert {seat.section for seat in center} == {"Center"}


def test_center_seat_bank_rejects_a_two_bank_row() -> None:
    row = [
        seat(column, x)
        for column, x in enumerate(
            [0.0, 10.0, 20.0, 30.0, 70.0, 80.0, 90.0, 100.0],
            start=1,
        )
    ]

    assert center_seat_bank(row) is None


def test_partition_seat_banks_rejects_insufficient_geometry() -> None:
    row = [seat(1, 0.0), seat(2, 10.0), seat(3, 20.0)]

    assert partition_seat_banks(row) is None
    assert center_seat_bank(row) is None


def test_center_seat_bank_rejects_a_single_uninterrupted_row() -> None:
    row = [
        seat(column, x)
        for column, x in enumerate(
            [0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0],
            start=1,
        )
    ]

    banks = partition_seat_banks(row)

    assert banks is not None
    assert len(banks) == 1
    assert center_seat_bank(row) is None


def test_center_seat_bank_ignores_a_detached_side_cluster() -> None:
    row = [
        seat(column, x)
        for column, x in enumerate(
            [
                *[0.0, 10.0, 20.0, 30.0, 40.0, 50.0],
                *[90.0, 100.0, 110.0, 120.0, 130.0, 140.0],
                *[180.0, 190.0, 200.0, 210.0, 220.0, 230.0],
                *[500.0, 510.0, 520.0, 530.0],
            ],
            start=1,
        )
    ]

    center = center_seat_bank(row)

    assert center is not None
    assert [seat.column for seat in center] == [7, 8, 9, 10, 11, 12]


def test_center_seat_bank_rejects_a_median_that_falls_in_an_aisle() -> None:
    row = [
        seat(column, x)
        for column, x in enumerate(
            [
                *[0.0, 10.0, 20.0, 30.0],
                *[70.0, 80.0, 90.0, 100.0],
                *[140.0, 150.0, 160.0, 170.0],
                *[400.0, 410.0, 420.0, 430.0],
            ],
            start=1,
        )
    ]

    assert center_seat_bank(row) is None


def test_center_seat_bank_never_selects_an_edge_bank() -> None:
    row = [
        seat(column, x)
        for column, x in enumerate(
            [
                *[0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0],
                *[130.0, 140.0, 150.0],
                *[190.0, 200.0, 210.0],
            ],
            start=1,
        )
    ]

    banks = partition_seat_banks(row)

    assert banks is not None
    assert len(banks) == 3
    assert center_seat_bank(row) is None
