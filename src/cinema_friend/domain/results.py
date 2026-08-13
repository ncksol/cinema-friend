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
    seat_key: str

    def sort_key(self) -> tuple[int, int, int, int, float, datetime, str]:
        return (
            -self.preferred_seat_overlap,
            -self.preferred_row_match,
            -self.view_score_band,
            self.preferred_time_distance_minutes,
            -self.raw_view_score,
            self.performance_start,
            self.seat_key,
        )


SEAT_ID_SEPARATOR = "|"


@dataclass(frozen=True, slots=True)
class RankedOption:
    """One offerable seat block, carrying both how it is named and how it is identified.

    ``seat_label`` is for people (``L17-L18``); ``seat_ids`` are the venue's stable seat
    identifiers and are the only thing identity is ever derived from. The two are kept
    apart because a label is neither unique -- two sections can each have a row L -- nor
    stable across a re-parse, while an ID is both and is unreadable.
    """

    performance: Performance
    seat_label: str
    seat_ids: tuple[str, ...]
    rank_vector: RankVector
    price_pence: int | None
    seat_categories: tuple[str, ...] = ()
    title: str | None = None

    @property
    def seat_key(self) -> str:
        """The block's seat IDs in order, joined by a separator no seat ID contains.

        ``|`` rather than ``-`` because a BFI seat ID is a GUID and already contains
        hyphens, so a hyphen join could not be read back unambiguously.
        """
        return SEAT_ID_SEPARATOR.join(self.seat_ids)

    @property
    def key(self) -> str:
        """Stable identity of this option: performance ID then ordered seat IDs.

        Persistence stores it, notification policy compares against it, and both must
        agree, so it is derived here rather than rebuilt independently in each layer.
        Seat IDs, not labels, are what make it unique: identical row numbers in two
        different sections would otherwise collide into one key.
        """
        return f"{self.performance.performance_id}:{self.seat_key}"


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
    """One page of a stored snapshot, plus the watch title it belongs to.

    ``watch_title`` is the watch's current name rather than anything stored on the
    snapshot: it is the only title an empty snapshot can be shown under, since a
    snapshot with no options carries no performance to take one from.
    """

    snapshot_id: UUID
    checked_at: datetime
    options: tuple[RankedOption, ...]
    page: int
    total_pages: int
    total_options: int
    total_performances: int
    watch_title: str | None = None


@dataclass(frozen=True, slots=True)
class NotificationPayload:
    kind: str
    recipient_user_id: int
    watch_id: UUID | None
    snapshot_id: UUID | None
    new_option_count: int
    host: str | None
    recovery_text: str | None
    initial_recurring_empty: bool = False


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
