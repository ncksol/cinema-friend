"""BFI transport domain types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from zoneinfo import ZoneInfo

_LONDON = ZoneInfo("Europe/London")


class SeatStatus(Enum):
    AVAILABLE = "available"
    RESERVED = "reserved"
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
    row: str
    column: int
    x: float
    y: float


@dataclass(frozen=True, slots=True)
class SeatBlock:
    row: str
    seats: tuple[Seat, ...]


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
