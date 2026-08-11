"""Watch-related domain types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from cinema_friend.domain.errors import InputError
from cinema_friend.domain.state import WatchMode, WatchStatus


@dataclass(frozen=True, slots=True)
class WatchCriteria:
    source_url: str
    slug: str
    date_from: date
    date_to: date
    time_from: time
    time_to: time
    quantity: int
    mode: WatchMode
    interval: timedelta | None = None
    preferred_seats: frozenset[str] = field(default_factory=frozenset)
    excluded_seats: frozenset[str] = field(default_factory=frozenset)
    preferred_rows: frozenset[str] = field(default_factory=frozenset)
    excluded_rows: frozenset[str] = field(default_factory=frozenset)
    preferred_utc_instant: datetime | None = None

    def __post_init__(self) -> None:
        if not (1 <= self.quantity <= 8):
            raise InputError("quantity must be between 1 and 8")
        if self.date_from > self.date_to:
            raise InputError("date_from must not be after date_to")
        if self.mode is WatchMode.RECURRING:
            if self.interval is None:
                raise InputError("interval is required for RECURRING mode")
            if self.interval < timedelta(minutes=15):
                raise InputError("interval must be at least 15 minutes")
        if self.mode is WatchMode.ONE_OFF and self.interval is not None:
            raise InputError("interval must not be set for ONE_OFF mode")
        if self.preferred_utc_instant is not None:
            inst = self.preferred_utc_instant
            if inst.tzinfo is None or inst.utcoffset() != timedelta(0):
                raise InputError("preferred_utc_instant must be a UTC-aware datetime")
            inst_date = inst.date()
            inst_time = inst.timetz().replace(tzinfo=None)
            if not (self.date_from <= inst_date <= self.date_to):
                raise InputError("preferred_utc_instant date is outside the watch date range")
            if not (self.time_from <= inst_time <= self.time_to):
                raise InputError("preferred_utc_instant time is outside the watch time range")


@dataclass(frozen=True, slots=True)
class Watch:
    watch_id: int
    user_id: int
    criteria: WatchCriteria
    status: WatchStatus
    created_at: datetime
    updated_at: datetime
    next_check_at: datetime | None = None
