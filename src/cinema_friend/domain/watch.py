"""Watch-related domain types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from uuid import UUID

from cinema_friend.domain.errors import InputError
from cinema_friend.domain.state import SeatPreferenceStrategy, WatchMode, WatchStatus
from cinema_friend.domain.time_window import (
    LONDON,
    DailyTimeWindow,
    window_for_local_date,
    within_daily_window,
)


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
    seat_preference_strategy: SeatPreferenceStrategy = SeatPreferenceStrategy.ADVANCED
    preferred_seats: frozenset[str] = field(default_factory=frozenset)
    excluded_seats: frozenset[str] = field(default_factory=frozenset)
    preferred_rows: frozenset[str] = field(default_factory=frozenset)
    excluded_rows: frozenset[str] = field(default_factory=frozenset)
    preferred_utc_instant: datetime | None = None
    weekend_time_from: time | None = None
    weekend_time_to: time | None = None

    def time_window_for(self, local_date: date) -> DailyTimeWindow:
        weekend_window = (
            (self.weekend_time_from, self.weekend_time_to)
            if self.weekend_time_from is not None and self.weekend_time_to is not None
            else None
        )
        return window_for_local_date(
            local_date,
            default_window=(self.time_from, self.time_to),
            weekend_window=weekend_window,
        )

    def __post_init__(self) -> None:
        if not (1 <= self.quantity <= 8):
            raise InputError("quantity must be between 1 and 8")
        if (self.weekend_time_from is None) != (self.weekend_time_to is None):
            raise InputError("weekend time range requires both start and end")
        manual_seat_criteria = (
            self.preferred_seats or self.excluded_seats or self.preferred_rows or self.excluded_rows
        )
        if (
            self.seat_preference_strategy is not SeatPreferenceStrategy.ADVANCED
            and manual_seat_criteria
        ):
            raise InputError(
                "simple seat preferences cannot be combined with manual seat criteria"
            )
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
            # The date and time bounds are wall-clock facts about the cinema, and so is
            # the user's preference; comparing UTC components against them is wrong by
            # an hour for eight months of the year.
            local = inst.astimezone(LONDON)
            if not (self.date_from <= local.date() <= self.date_to):
                raise InputError("preferred_utc_instant date is outside the watch date range")
            time_from, time_to = self.time_window_for(local.date())
            if not within_daily_window(time_from, time_to, local.time()):
                raise InputError("preferred_utc_instant time is outside the watch time range")


@dataclass(frozen=True, slots=True)
class Watch:
    """One user's saved search.

    ``watch_id`` is a client-generated UUID: services need a stable identity before the
    row exists, callback payloads carry it as text, and the ``watches`` primary key is
    ``TEXT``. ``title`` stays ``None`` until a successful BFI parse names the film, and
    ``last_check_at`` stays ``None`` until the first check completes.
    """

    watch_id: UUID
    user_id: int
    criteria: WatchCriteria
    status: WatchStatus
    created_at: datetime
    updated_at: datetime
    next_check_at: datetime | None = None
    title: str | None = None
    last_check_at: datetime | None = None
