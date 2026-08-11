"""Tests for cinema_friend.services.notification_policy.

Every decision here is a plain function of its arguments: no database, no Telegram, and
no wall-clock reads, so the same inputs always produce the same decision and nothing
here needs a fixture beyond the values it is handed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cinema_friend.domain.bfi import Performance
from cinema_friend.domain.results import CircuitState, HostCircuit, RankedOption, RankVector
from cinema_friend.domain.state import CheckTrigger
from cinema_friend.services.notification_policy import (
    decide_degradation_notification,
    decide_recovery_notification,
    decide_result_notification,
)

_START = datetime(2026, 8, 26, 18, 0, tzinfo=UTC)


def rank_vector(
    *,
    seat_label: str = "L17-L18",
    preferred_seat_overlap: int = 1,
    preferred_row_match: int = 1,
    view_score_band: int = 19,
    preferred_time_distance_minutes: int = 0,
    raw_view_score: float = 95.5,
) -> RankVector:
    return RankVector(
        preferred_seat_overlap=preferred_seat_overlap,
        preferred_row_match=preferred_row_match,
        view_score_band=view_score_band,
        preferred_time_distance_minutes=preferred_time_distance_minutes,
        raw_view_score=raw_view_score,
        performance_start=_START,
        seat_label=seat_label,
    )


def ranked_option(seat_label: str, vector: RankVector, *, performance_id: str = "p1") -> RankedOption:
    return RankedOption(
        performance=Performance(
            performance_id=performance_id,
            event_id="e1",
            start_utc=_START,
            sales_status_code="OPEN*",
            availability_code="A",
            availability_num=50,
            reserved_seating=True,
            seat_map_url="https://whatson.bfi.org.uk/imax/Online/mapSelect.asp?ID=p1",
        ),
        seat_label=seat_label,
        rank_vector=vector,
        price_pence=1500,
    )


_GOOD = rank_vector(seat_label="L17-L18", raw_view_score=98.0)
_BETTER = rank_vector(seat_label="L19-L20", raw_view_score=99.5)
_WORSE = rank_vector(seat_label="K1-K2", preferred_seat_overlap=0, raw_view_score=60.0)

_GOOD_OPTION = ranked_option("L17-L18", _GOOD)
_BETTER_OPTION = ranked_option("L19-L20", _BETTER)
_WORSE_OPTION = ranked_option("K1-K2", _WORSE)


# ---------------------------------------------------------------------------
# decide_result_notification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("trigger", [CheckTrigger.MANUAL, CheckTrigger.CREATION])
def test_manual_and_creation_triggers_always_require_a_snapshot_even_with_no_change(
    trigger: CheckTrigger,
) -> None:
    """Both an explicit ``/check`` and the immediate check a new watch schedules must
    respond, even when nothing changed since the owner was last told."""
    decision = decide_result_notification(
        trigger,
        (_GOOD_OPTION,),
        known_keys=frozenset({_GOOD_OPTION.key}),
        last_best=_GOOD,
        recipient_user_id=11,
    )

    assert decision.requires_snapshot is True
    assert decision.kind == "results"
    assert decision.recipient_user_id == 11


@pytest.mark.parametrize("trigger", [CheckTrigger.MANUAL, CheckTrigger.CREATION])
def test_manual_and_creation_triggers_require_a_snapshot_even_with_no_options(
    trigger: CheckTrigger,
) -> None:
    decision = decide_result_notification(
        trigger, (), known_keys=frozenset(), last_best=None, recipient_user_id=11
    )

    assert decision.requires_snapshot is True
    assert decision.all_option_keys == frozenset()
    assert decision.best_rank is None


@pytest.mark.parametrize("trigger", [CheckTrigger.SCHEDULED, CheckTrigger.RECOVERY])
def test_scheduled_and_recovery_triggers_produce_no_delivery_when_unchanged(
    trigger: CheckTrigger,
) -> None:
    """A system-initiated recheck -- scheduled or post-recovery -- is change-only,
    just like a scheduled one: neither is the owner asking, so silence is correct
    when nothing new or better appeared."""
    decision = decide_result_notification(
        trigger,
        (_GOOD_OPTION,),
        known_keys=frozenset({_GOOD_OPTION.key}),
        last_best=_GOOD,
        recipient_user_id=11,
    )

    assert decision.requires_snapshot is False
    assert decision.new_option_keys == frozenset()


@pytest.mark.parametrize("trigger", [CheckTrigger.SCHEDULED, CheckTrigger.RECOVERY])
def test_scheduled_and_recovery_triggers_notify_once_for_a_never_surfaced_option(
    trigger: CheckTrigger,
) -> None:
    """A brand-new option is worth telling the owner about even if it ranks worst."""
    decision = decide_result_notification(
        trigger,
        (_GOOD_OPTION, _WORSE_OPTION),
        known_keys=frozenset({_GOOD_OPTION.key}),
        last_best=_GOOD,
        recipient_user_id=11,
    )

    assert decision.requires_snapshot is True
    assert decision.new_option_keys == frozenset({_WORSE_OPTION.key})
    assert decision.all_option_keys == frozenset({_GOOD_OPTION.key, _WORSE_OPTION.key})
    assert decision.best_rank == _GOOD


def test_scheduled_known_option_with_a_better_rank_notifies_once() -> None:
    """No new key appears, but the best available rank improved on what was last told."""
    decision = decide_result_notification(
        CheckTrigger.SCHEDULED,
        (_BETTER_OPTION,),
        known_keys=frozenset({_BETTER_OPTION.key}),
        last_best=_GOOD,
        recipient_user_id=11,
    )

    assert decision.requires_snapshot is True
    assert decision.new_option_keys == frozenset()
    assert decision.best_rank == _BETTER


def test_scheduled_disappearance_is_silent() -> None:
    """A previously known option vanishing, with no new or better option, is not news."""
    decision = decide_result_notification(
        CheckTrigger.SCHEDULED,
        (_WORSE_OPTION,),
        known_keys=frozenset({_GOOD_OPTION.key, _WORSE_OPTION.key}),
        last_best=_GOOD,
        recipient_user_id=11,
    )

    assert decision.requires_snapshot is False


def test_scheduled_worse_only_change_is_silent() -> None:
    """The known best option's rank got worse; nothing new or improved appeared."""
    decision = decide_result_notification(
        CheckTrigger.SCHEDULED,
        (_WORSE_OPTION,),
        known_keys=frozenset({_WORSE_OPTION.key}),
        last_best=_GOOD,
        recipient_user_id=11,
    )

    assert decision.requires_snapshot is False
    assert decision.best_rank == _WORSE


def test_decision_carries_the_recipient_user_id_unchanged() -> None:
    """``NotificationDecision`` must name who it is for; Task 11 persists deliveries
    keyed on this recipient and cannot derive it from anything else in the decision."""
    decision = decide_result_notification(
        CheckTrigger.MANUAL,
        (),
        known_keys=frozenset(),
        last_best=None,
        recipient_user_id=42,
    )

    assert decision.recipient_user_id == 42


# ---------------------------------------------------------------------------
# Host degradation / recovery alerts
# ---------------------------------------------------------------------------


def _circuit(*, generation: int = 3, state: CircuitState = CircuitState.OPEN) -> HostCircuit:
    return HostCircuit(
        host="whatson.bfi.org.uk",
        state=state,
        backoff_step=1,
        generation=generation,
        next_probe=None,
        updated_at=_START,
    )


def test_degradation_alert_emits_once_per_owner_even_with_several_watches() -> None:
    """Owner 11 owns two watches against the host; they must get exactly one alert."""
    notifications = decide_degradation_notification(_circuit(), owner_user_ids=[11, 11, 22])

    keyed = {item.payload.recipient_user_id: item for item in notifications}
    assert set(keyed) == {11, 22}
    assert keyed[11].idempotency_key == "whatson.bfi.org.uk:3:degradation:11"
    assert keyed[11].payload.kind == "degradation"
    assert keyed[11].payload.host == "whatson.bfi.org.uk"
    assert keyed[11].payload.watch_id is None
    assert keyed[11].payload.snapshot_id is None


def test_recovery_alert_emits_once_per_owner_and_carries_recovery_text() -> None:
    notifications = decide_recovery_notification(_circuit(generation=5), owner_user_ids=[11, 11])

    assert len(notifications) == 1
    only = notifications[0]
    assert only.idempotency_key == "whatson.bfi.org.uk:5:recovery:11"
    assert only.payload.kind == "recovery"
    assert only.payload.recovery_text is not None


def test_degradation_and_recovery_keys_differ_by_generation() -> None:
    """A later incident on the same host must not collide with an earlier one's key."""
    first = decide_degradation_notification(_circuit(generation=1), owner_user_ids=[11])
    second = decide_degradation_notification(_circuit(generation=2), owner_user_ids=[11])

    assert first[0].idempotency_key != second[0].idempotency_key


def test_host_alerts_with_no_owners_produce_nothing() -> None:
    assert decide_degradation_notification(_circuit(), owner_user_ids=[]) == ()
    assert decide_recovery_notification(_circuit(), owner_user_ids=[]) == ()
