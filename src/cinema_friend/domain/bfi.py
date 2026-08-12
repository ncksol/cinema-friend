"""BFI transport domain types."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from cinema_friend.domain.time_window import LONDON as _LONDON

RESERVED_SEATING_OPTION = "2"
"""The ``options`` code BFI uses to mark a performance as reserved-seating.

Observed on every reserved IMAX performance as ``options: ["1", "2"]``. There is no
boolean ``reserved_seating`` field on the wire; the earlier one was invented.
"""

UNPUBLISHED_AVAILABILITY_SENTINEL = -1
"""The ``availability_num`` value BFI sends when it does not publish a count."""

UNPUBLISHED_AVAILABILITY_STATUS = "U"
"""The base ``availability_status`` that licenses the unpublished sentinel."""


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
    """One BFI performance, as read from an ``articleContext`` search-results row.

    Field names follow BFI's wire schema, which is the only schema there is: the
    performance's identity is the row's ``id``, and how many seats are on offer is
    ``availability_status`` plus ``availability_num``.

    ``availability_num`` is the *effective* candidate count and is never negative. BFI
    sends ``-1`` alongside availability status ``U`` to mean "the count is not
    published", which is not a count of anything; that case is normalised to ``0`` here
    and flagged by ``availability_published`` so a caller can tell "none free" from
    "not saying". Every eligibility test in the service is a ``> 0`` / ``>= quantity``
    comparison, so an unpublished count offers nothing rather than accidentally passing
    an ``!= 0`` check.
    """

    performance_id: str
    start_utc: datetime
    sales_status_code: str
    availability_status_code: str
    availability_num: int
    seat_map_url: str | None
    title: str = ""
    availability_published: bool = True
    options: tuple[str, ...] = ()

    @property
    def start(self) -> datetime:
        """Return the performance start time in the Europe/London timezone."""
        return self.start_utc.astimezone(_LONDON)

    @property
    def sales_status_base(self) -> str:
        """Return sales status code with any trailing ``*`` stripped."""
        return self.sales_status_code.rstrip("*")

    @property
    def availability_status_base(self) -> str:
        """Return availability status code with any trailing ``*`` stripped."""
        return self.availability_status_code.rstrip("*")

    @property
    def reserved_seating(self) -> bool:
        """True when BFI lists this performance as having a reserved seating plan.

        Derived, not stored: the site carries it as option code ``2`` in ``options``,
        and there is no separate boolean on the wire to disagree with.
        """
        return RESERVED_SEATING_OPTION in self.options


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
