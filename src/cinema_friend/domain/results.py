"""Result and ranking domain types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from cinema_friend.domain.bfi import Performance
from cinema_friend.domain.state import CheckOutcome, CheckTrigger


class DeliveryStatus(Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class RankVector:
    preferred_seat_overlap: int
    preferred_row_match: int
    view_score_band: int
    preferred_time_distance_minutes: int
    raw_view_score: float
    performance_start: datetime
    seat_label: str

    def sort_key(self) -> tuple[int, int, int, int, float, datetime, str]:
        return (
            -self.preferred_seat_overlap,
            -self.preferred_row_match,
            -self.view_score_band,
            self.preferred_time_distance_minutes,
            -self.raw_view_score,
            self.performance_start,
            self.seat_label,
        )


@dataclass(frozen=True, slots=True)
class RankedOption:
    performance: Performance
    seat_label: str
    rank_vector: RankVector
    price_pence: int | None


@dataclass(frozen=True, slots=True)
class CheckResult:
    check_run_id: int
    watch_id: int
    trigger: CheckTrigger
    outcome: CheckOutcome
    snapshot_id: int | None
    performance_count: int
    option_count: int
    error_detail: str | None


@dataclass(frozen=True, slots=True)
class SnapshotPage:
    snapshot_id: int
    checked_at: datetime
    options: tuple[RankedOption, ...]
    page: int
    total_pages: int
    total_options: int
    total_performances: int


@dataclass(frozen=True, slots=True)
class NotificationPayload:
    kind: str
    recipient_user_id: int
    watch_id: int | None
    snapshot_id: int | None
    new_option_count: int
    host: str | None
    recovery_text: str | None


@dataclass(frozen=True, slots=True)
class HostCircuit:
    host: str
    state: CircuitState
    backoff_step: int
    generation: int
    next_probe: datetime | None
    updated_at: datetime
