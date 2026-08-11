"""BFI transport domain types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class SeatStatus(Enum):
    AVAILABLE = "available"
    RESERVED = "reserved"
    SOLD = "sold"
    RESTRICTED = "restricted"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PriceZone:
    zone_id: str
    name: str
    price_pence: int


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
    blocks: tuple[SeatBlock, ...]
    source_url: str


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
