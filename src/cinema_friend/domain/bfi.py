"""BFI transport domain types."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from cinema_friend.domain.time_window import LONDON as _LONDON


class SeatStatus(Enum):
    AVAILABLE = "available"
    RESERVED = "reserved"
    UNAVAILABLE = "unavailable"
    SOLD = "sold"
    CONTENDED = "contended"
    RESTRICTED = "restricted"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PriceZone:
    zone_id: str
    label: str
    price: Decimal | None


@dataclass(frozen=True, slots=True)
class Seat:
    seat_id: str
    raw_status_code: str
    status: SeatStatus
    zone: PriceZone | None
    note: str
    section: str
    row: str
    column: int
    x: float
    y: float

    @property
    def label(self) -> str:
        """How a person names this seat: its row letter and seat number, e.g. ``L17``.

        ``seat_id`` is the venue's opaque identifier (a GUID on a real BFI seat map) and
        is stable but meaningless; this is what a user types into a watch and reads back
        off a ticket. It is not unique on its own -- two sections can both number a row
        L -- so it must never stand in for identity.
        """
        return f"{self.row}{self.column}"


@dataclass(frozen=True, slots=True)
class SeatBlock:
    row: str
    seats: tuple[Seat, ...]

    @property
    def label(self) -> str:
        """The block's human-readable seat labels in seat order, e.g. ``L17-L18``."""
        return "-".join(seat.label for seat in self.seats)

    @property
    def seat_ids(self) -> tuple[str, ...]:
        """The block's stable venue seat identifiers, in the same order as ``seats``."""
        return tuple(seat.seat_id for seat in self.seats)

    @property
    def categories(self) -> tuple[str, ...]:
        """Distinct price-zone labels covering the block, in seat order."""
        return tuple(
            dict.fromkeys(seat.zone.label for seat in self.seats if seat.zone is not None)
        )


@dataclass(frozen=True, slots=True)
class SeatMap:
    performance_id: str
    seats: tuple[Seat, ...]


@dataclass(frozen=True, slots=True)
class Performance:
    performance_id: str
    event_id: str
    start_utc: datetime
    sales_status_code: str
    availability_code: str
    availability_num: int
    reserved_seating: bool
    seat_map_url: str | None
    options: tuple[str, ...] = ()

    @property
    def start(self) -> datetime:
        """Return the performance start time in the Europe/London timezone."""
        return self.start_utc.astimezone(_LONDON)

    @property
    def sales_status_base(self) -> str:
        """Return sales status code with any trailing ``*`` stripped."""
        return self.sales_status_code.rstrip("*")


@dataclass(frozen=True, slots=True)
class PerformanceListing:
    title: str | None
    performances: tuple[Performance, ...]

    def __iter__(self) -> Iterator[Performance]:
        return iter(self.performances)

    def __len__(self) -> int:
        return len(self.performances)

    def __getitem__(self, index: int) -> Performance:
        return self.performances[index]
