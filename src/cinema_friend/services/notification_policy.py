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
from uuid import UUID

from cinema_friend.domain.results import HostCircuit, NotificationPayload, RankedOption, RankVector
from cinema_friend.domain.state import CheckTrigger

_RESULTS_KIND = "results"
_DEGRADATION_KIND = "degradation"
_RECOVERY_KIND = "recovery"
_CONTRACT_ERROR_KIND = "contract_error"
_RECOVERY_TEXT = "back online"

# Both an explicit "/check" and the immediate check a new watch schedules on creation
# are the owner asking directly, so both always produce a response. A scheduled recheck
# and a post-recovery recheck are system-initiated -- neither is the owner asking -- so
# both stay change-only.
_ALWAYS_NOTIFY_TRIGGERS = frozenset({CheckTrigger.MANUAL, CheckTrigger.CREATION})


@dataclass(frozen=True, slots=True)
class NotificationDecision:
    """What a completed check should tell its watch's owner.

    ``recipient_user_id`` names who the decision is for -- the caller (the check
    orchestrator) always knows this from the watch it just checked, but this decision
    is the only thing Task 11 persists a delivery from, so the recipient must travel
    with it rather than be re-derived from context the persistence layer does not have.
    ``new_option_keys`` is the subset of ``all_option_keys`` never previously announced
    to this watch's owner. ``best_rank`` is the best (smallest
    :meth:`RankVector.sort_key`) option among everything the check found this time, or
    ``None`` when it found nothing; a caller that acts on this decision stores it as the
    watch's new ``last_best_rank``. ``requires_snapshot`` is ``True`` exactly when a
    notification must be sent, and therefore a snapshot must be persisted for it to
    point at -- ``False`` means nothing changed enough to justify one.
    """

    kind: str
    recipient_user_id: int
    new_option_keys: frozenset[str]
    all_option_keys: frozenset[str]
    best_rank: RankVector | None
    requires_snapshot: bool


@dataclass(frozen=True, slots=True)
class QueuedNotification:
    """One deliverable message paired with its deterministic idempotency key.

    Host alerts key on host, generation, kind, and owner; a watch-scoped alert keys on
    the watch. Either way the key is derived from the fact being announced rather than
    from the attempt announcing it, so replaying a decision cannot produce a second
    message.
    """

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
    recipient_user_id: int,
) -> NotificationDecision:
    """Decide whether a check's results are worth telling the owner about.

    A manual check or the immediate check creating a watch schedules always responds,
    even with no options and no change, because the owner explicitly asked (directly,
    for manual; by creating the watch, for creation). A scheduled or recovery check
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
    requires_snapshot = trigger in _ALWAYS_NOTIFY_TRIGGERS or bool(new_keys) or rank_improved
    return NotificationDecision(
        kind=_RESULTS_KIND,
        recipient_user_id=recipient_user_id,
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
) -> tuple[QueuedNotification, ...]:
    """One notification per distinct owner, keyed by host, generation, kind, and owner.

    Deduplicating owners first is what keeps a user with several watches against the
    same host from getting one alert per watch: the idempotency key carries no watch
    identity at all, so every one of their watches maps to the same key and therefore
    the same, single delivery.
    """
    return tuple(
        QueuedNotification(
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


def circuit_records_incident(circuit: HostCircuit) -> bool:
    """Whether ``circuit`` has ever been tripped by a real host failure.

    ``generation`` advances only when a closed circuit trips, so a generation of zero
    means no incident was ever recorded for this host -- whatever the circuit's current
    state happens to be. That is the only honest evidence that a host was reported down,
    and therefore the only thing that makes "it is back" true.
    """
    return circuit.generation >= 1


def decide_degradation_notification(
    circuit: HostCircuit, owner_user_ids: Iterable[int]
) -> tuple[QueuedNotification, ...]:
    """Return one degradation alert per distinct owner of a watch against ``circuit``."""
    return _host_notifications(circuit, owner_user_ids, kind=_DEGRADATION_KIND, recovery_text=None)


def decide_recovery_notification(
    circuit: HostCircuit, owner_user_ids: Iterable[int]
) -> tuple[QueuedNotification, ...]:
    """Return one recovery alert per distinct owner of a watch against ``circuit``.

    Nothing is announced for a circuit that records no incident. A watch can enter
    backoff without the host ever being implicated -- exhausted network retries put it
    there on its own schedule -- and its probe succeeding is then just a check that
    worked, not a host coming back. Sending on that would tell every owner of a watch on
    the host about an outage none of them was told about, and would consume the
    generation-0 idempotency key so the *real* first recovery could never be sent.
    """
    if not circuit_records_incident(circuit):
        return ()
    return _host_notifications(
        circuit, owner_user_ids, kind=_RECOVERY_KIND, recovery_text=_RECOVERY_TEXT
    )


def decide_contract_error_notification(
    watch_id: UUID, recipient_user_id: int
) -> QueuedNotification:
    """Return the single alert owed to the owner of a watch paused by a contract error.

    Keyed on the watch alone, so however many times the parse fails -- and however many
    times the failure is retried -- the owner is told once that this watch has stopped.
    The payload carries identity only: which watch, whose it is. Nothing here decides how
    it reads, because rendering is Task 14's contract, not this module's.
    """
    return QueuedNotification(
        idempotency_key=f"{_CONTRACT_ERROR_KIND}:{watch_id}",
        payload=NotificationPayload(
            kind=_CONTRACT_ERROR_KIND,
            recipient_user_id=recipient_user_id,
            watch_id=watch_id,
            snapshot_id=None,
            new_option_count=0,
            host=None,
            recovery_text=None,
        ),
    )
