"""Result and ranking domain types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID

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

    @property
    def key(self) -> str:
        """Stable identity of this option: performance ID then ordered seat IDs.

        Persistence stores it, notification policy compares against it, and both must
        agree, so it is derived here rather than rebuilt independently in each layer.
        """
        return f"{self.performance.performance_id}:{self.seat_label}"


@dataclass(frozen=True, slots=True)
class ResultSnapshot:
    snapshot_id: UUID
    watch_id: UUID
    checked_at: datetime
    options: tuple[RankedOption, ...]


@dataclass(frozen=True, slots=True)
class CheckResult:
    check_run_id: UUID
    watch_id: UUID
    trigger: CheckTrigger
    outcome: CheckOutcome
    snapshot_id: UUID | None
    performance_count: int
    option_count: int
    error_detail: str | None


@dataclass(frozen=True, slots=True)
class SnapshotPage:
    snapshot_id: UUID
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
    watch_id: UUID | None
    snapshot_id: UUID | None
    new_option_count: int
    host: str | None
    recovery_text: str | None


@dataclass(frozen=True, slots=True)
class NotificationDelivery:
    """One queued or completed outbound message.

    ``delivered_at`` is set only when the message actually went out, which is also the
    only point at which its options become known and its watch flags move.
    """

    delivery_id: UUID
    idempotency_key: str
    payload: NotificationPayload
    status: DeliveryStatus
    attempt_count: int
    next_attempt_at: datetime
    created_at: datetime
    delivered_at: datetime | None


@dataclass(frozen=True, slots=True)
class NotificationState:
    """What a watch's owner has already been told.

    ``recovery_pending`` is owed from the moment a degradation alert is delivered, so a
    watch that degrades and is never followed up is visible rather than silently stuck.
    """

    watch_id: UUID
    last_best_rank: RankVector | None
    degradation_notified: bool
    recovery_pending: bool


@dataclass(frozen=True, slots=True)
class HostCircuit:
    """One host's persisted breaker state.

    ``generation`` counts incidents: it advances only when a closed circuit trips, so
    every transition belonging to one outage shares a generation. ``revision`` is a
    per-row fencing token that advances on *every* persisted write; ``0`` means no row
    has been stored yet. A writer carries the revision it observed and only wins a
    compare-and-swap while the stored row still has it, which is what stops a second
    process from claiming a probe or replaying a stale transition over a newer one.
    """

    host: str
    state: CircuitState
    backoff_step: int
    generation: int
    next_probe: datetime | None
    updated_at: datetime
    revision: int = 0
