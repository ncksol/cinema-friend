"""Pure notification decisions: no database, Telegram, or wall-clock reads.

Everything here is a plain function of its arguments, so a check-result decision or a
host alert can be unit-tested without a database, a bot, or a clock, and so every side
effect a decision implies -- persisting a delivery, moving a watch's notification state
-- stays with the caller (the check orchestrator for results, the scheduler for host
alerts) rather than living inside "policy" code.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from cinema_friend.domain.results import HostCircuit, NotificationPayload, RankedOption, RankVector
from cinema_friend.domain.state import CheckTrigger

_RESULTS_KIND = "results"
_DEGRADATION_KIND = "degradation"
_RECOVERY_KIND = "recovery"
_RECOVERY_TEXT = "back online"


@dataclass(frozen=True, slots=True)
class NotificationDecision:
    """What a completed check should tell its watch's owner.

    ``new_option_keys`` is the subset of ``all_option_keys`` never previously announced
    to this watch's owner. ``best_rank`` is the best (smallest
    :meth:`RankVector.sort_key`) option among everything the check found this time, or
    ``None`` when it found nothing; a caller that acts on this decision stores it as the
    watch's new ``last_best_rank``. ``requires_snapshot`` is ``True`` exactly when a
    notification must be sent, and therefore a snapshot must be persisted for it to
    point at -- ``False`` means nothing changed enough to justify one.
    """

    kind: str
    new_option_keys: frozenset[str]
    all_option_keys: frozenset[str]
    best_rank: RankVector | None
    requires_snapshot: bool


@dataclass(frozen=True, slots=True)
class HostNotification:
    """One recipient's host alert, paired with its deterministic idempotency key."""

    idempotency_key: str
    payload: NotificationPayload


def _best_rank(options: Sequence[RankedOption]) -> RankVector | None:
    if not options:
        return None
    return min((option.rank_vector for option in options), key=lambda vector: vector.sort_key())


def decide_result_notification(
    trigger: CheckTrigger,
    options: Sequence[RankedOption],
    known_keys: frozenset[str],
    last_best: RankVector | None,
) -> NotificationDecision:
    """Decide whether a check's results are worth telling the owner about.

    A manual check -- which creating a watch also triggers, since it schedules an
    immediate check the same way "/check" does -- always responds, even with no options
    and no change, because the owner explicitly asked. A scheduled or recovery check
    stays silent unless there is something genuinely new: an option whose key was never
    surfaced before (regardless of how it ranks), or a strict improvement in the best
    rank on offer compared with what was last announced. A previously known option
    disappearing, or the best rank only getting worse, is not news on its own.
    """
    current_keys = frozenset(option.key for option in options)
    new_keys = current_keys - known_keys
    best_rank = _best_rank(options)
    rank_improved = (
        best_rank is not None
        and last_best is not None
        and best_rank.sort_key() < last_best.sort_key()
    )
    requires_snapshot = trigger is CheckTrigger.MANUAL or bool(new_keys) or rank_improved
    return NotificationDecision(
        kind=_RESULTS_KIND,
        new_option_keys=new_keys,
        all_option_keys=current_keys,
        best_rank=best_rank,
        requires_snapshot=requires_snapshot,
    )


def _host_notifications(
    circuit: HostCircuit,
    owner_user_ids: Iterable[int],
    *,
    kind: str,
    recovery_text: str | None,
) -> tuple[HostNotification, ...]:
    """One notification per distinct owner, keyed by host, generation, kind, and owner.

    Deduplicating owners first is what keeps a user with several watches against the
    same host from getting one alert per watch: the idempotency key carries no watch
    identity at all, so every one of their watches maps to the same key and therefore
    the same, single delivery.
    """
    return tuple(
        HostNotification(
            idempotency_key=f"{circuit.host}:{circuit.generation}:{kind}:{owner_user_id}",
            payload=NotificationPayload(
                kind=kind,
                recipient_user_id=owner_user_id,
                watch_id=None,
                snapshot_id=None,
                new_option_count=0,
                host=circuit.host,
                recovery_text=recovery_text,
            ),
        )
        for owner_user_id in sorted(set(owner_user_ids))
    )


def decide_degradation_notification(
    circuit: HostCircuit, owner_user_ids: Iterable[int]
) -> tuple[HostNotification, ...]:
    """Return one degradation alert per distinct owner of a watch against ``circuit``."""
    return _host_notifications(circuit, owner_user_ids, kind=_DEGRADATION_KIND, recovery_text=None)


def decide_recovery_notification(
    circuit: HostCircuit, owner_user_ids: Iterable[int]
) -> tuple[HostNotification, ...]:
    """Return one recovery alert per distinct owner of a watch against ``circuit``."""
    return _host_notifications(
        circuit, owner_user_ids, kind=_RECOVERY_KIND, recovery_text=_RECOVERY_TEXT
    )
