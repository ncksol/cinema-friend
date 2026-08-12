"""Tests for cinema_friend.storage.notification_repository.

A delivery row is the record that a user was told something. Two properties matter more
than anything else here: the same event must never produce two messages no matter how
many times a check re-runs, and an option must only be marked known once the message
that announced it actually went out.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import aiosqlite
import pytest

from cinema_friend.domain.errors import InputError
from cinema_friend.domain.results import DeliveryStatus, NotificationPayload, RankVector
from cinema_friend.domain.state import CheckOutcome, CheckTrigger
from cinema_friend.domain.watch import Watch
from cinema_friend.storage.database import Database
from cinema_friend.storage.notification_repository import NotificationRepository
from cinema_friend.storage.result_repository import ResultRepository
from tests.storage.conftest import NOW, FailingConnection, option, options, uuid_for

WatchFactory = Callable[..., Coroutine[Any, Any, Watch]]


@pytest.fixture
def repository(database: Database) -> NotificationRepository:
    return NotificationRepository(database)


def payload(
    watch: Watch | None = None,
    *,
    kind: str = "new_options",
    recipient_user_id: int = 11,
    snapshot_id: UUID | None = None,
    new_option_count: int = 2,
    host: str | None = None,
    recovery_text: str | None = None,
) -> NotificationPayload:
    return NotificationPayload(
        kind=kind,
        recipient_user_id=recipient_user_id,
        watch_id=None if watch is None else watch.watch_id,
        snapshot_id=snapshot_id,
        new_option_count=new_option_count,
        host=host,
        recovery_text=recovery_text,
    )


def rank_vector(seat_key: str = "seat-a|seat-b") -> RankVector:
    return RankVector(
        preferred_seat_overlap=1,
        preferred_row_match=1,
        view_score_band=19,
        preferred_time_distance_minutes=0,
        raw_view_score=95.5,
        performance_start=datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
        seat_key=seat_key,
    )


async def persist_snapshot(
    database: Database, conn: aiosqlite.Connection, watch: Watch
) -> UUID:
    results = ResultRepository(database)
    run = await results.start_check(
        conn, watch_id=watch.watch_id, trigger=CheckTrigger.SCHEDULED, started_at=NOW
    )
    snapshot = await results.complete_with_snapshot(
        conn,
        check_run_id=run,
        watch_id=watch.watch_id,
        options=options(2),
        checked_at=NOW,
        outcome=CheckOutcome.SUCCESS,
        performance_count=1,
    )
    return snapshot.snapshot_id


# ---------------------------------------------------------------------------
# Delivery creation and idempotency
# ---------------------------------------------------------------------------


async def test_create_delivery_persists_a_pending_row(
    repository: NotificationRepository, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    watch = await make_watch()
    delivery = await repository.create_delivery(conn, "key-1", payload(watch), NOW)

    assert delivery.status is DeliveryStatus.PENDING
    assert delivery.attempt_count == 0
    assert delivery.next_attempt_at == NOW
    assert delivery.delivered_at is None
    assert delivery.payload.watch_id == watch.watch_id


async def test_create_delivery_round_trips_the_payload(
    repository: NotificationRepository, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    watch = await make_watch()
    snapshot_id = uuid_for(77)
    original = payload(
        watch,
        kind="recovery",
        snapshot_id=None,
        new_option_count=0,
        host="whatson.bfi.org.uk",
        recovery_text="back online",
    )
    created = await repository.create_delivery(conn, "key-1", original, NOW)
    assert created.payload == original
    assert snapshot_id != created.delivery_id


async def test_create_delivery_is_idempotent_on_the_same_key(
    repository: NotificationRepository, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    """A re-run check must not produce a second message for an event already queued."""
    watch = await make_watch()
    first = await repository.create_delivery(conn, "key-1", payload(watch), NOW)
    second = await repository.create_delivery(
        conn, "key-1", payload(watch, new_option_count=99), NOW + timedelta(minutes=5)
    )

    assert second.delivery_id == first.delivery_id
    assert second.payload.new_option_count == first.payload.new_option_count
    cursor = await conn.execute("SELECT COUNT(*) AS n FROM notification_deliveries")
    assert (await cursor.fetchone())["n"] == 1


async def test_create_delivery_does_not_resurrect_a_sent_delivery(
    repository: NotificationRepository, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    watch = await make_watch()
    created = await repository.create_delivery(conn, "key-1", payload(watch), NOW)
    await repository.mark_delivered(conn, created.delivery_id, (), None, NOW)

    replayed = await repository.create_delivery(conn, "key-1", payload(watch), NOW)
    assert replayed.status is DeliveryStatus.SENT
    assert await repository.due_deliveries(conn, NOW) == ()


async def test_distinct_keys_create_distinct_deliveries(
    repository: NotificationRepository, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    watch = await make_watch()
    first = await repository.create_delivery(conn, "key-1", payload(watch), NOW)
    second = await repository.create_delivery(conn, "key-2", payload(watch), NOW)
    assert first.delivery_id != second.delivery_id


async def test_host_alert_delivery_has_no_watch_or_snapshot(
    repository: NotificationRepository, conn: aiosqlite.Connection
) -> None:
    """Degradation alerts are about a host, not a watch, so both FK columns stay null."""
    created = await repository.create_delivery(
        conn,
        "whatson.bfi.org.uk:3:degradation:11",
        payload(None, kind="degradation", host="whatson.bfi.org.uk", new_option_count=0),
        NOW,
    )
    assert created.payload.watch_id is None
    cursor = await conn.execute(
        "SELECT watch_id, snapshot_id FROM notification_deliveries WHERE id = ?",
        (str(created.delivery_id),),
    )
    row = await cursor.fetchone()
    assert row["watch_id"] is None
    assert row["snapshot_id"] is None


async def test_delivery_snapshot_reference_survives_snapshot_deletion(
    repository: NotificationRepository,
    database: Database,
    conn: aiosqlite.Connection,
    make_watch: WatchFactory,
) -> None:
    """Retention nulls the snapshot pointer; it must not delete the delivery record."""
    watch = await make_watch()
    snapshot_id = await persist_snapshot(database, conn, watch)
    created = await repository.create_delivery(
        conn, "key-1", payload(watch, snapshot_id=snapshot_id), NOW
    )
    await conn.execute("DELETE FROM result_snapshots WHERE id = ?", (str(snapshot_id),))

    reloaded = await repository.delivery(conn, created.delivery_id)
    assert reloaded is not None
    assert reloaded.payload.snapshot_id is None


# ---------------------------------------------------------------------------
# Due deliveries and retries
# ---------------------------------------------------------------------------


async def test_due_deliveries_returns_pending_rows_at_or_before_now(
    repository: NotificationRepository, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    watch = await make_watch()
    due = await repository.create_delivery(conn, "key-due", payload(watch), NOW)
    await repository.create_delivery(
        conn, "key-later", payload(watch), NOW + timedelta(minutes=5)
    )

    assert [item.delivery_id for item in await repository.due_deliveries(conn, NOW)] == [
        due.delivery_id
    ]


async def test_due_deliveries_are_ordered_oldest_attempt_first(
    repository: NotificationRepository, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    watch = await make_watch()
    late = await repository.create_delivery(conn, "key-b", payload(watch), NOW)
    early = await repository.create_delivery(
        conn, "key-a", payload(watch), NOW - timedelta(minutes=1)
    )
    assert [item.delivery_id for item in await repository.due_deliveries(conn, NOW)] == [
        early.delivery_id,
        late.delivery_id,
    ]


async def test_reschedule_keeps_the_delivery_pending_and_counts_the_attempt(
    repository: NotificationRepository, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    watch = await make_watch()
    created = await repository.create_delivery(conn, "key-1", payload(watch), NOW)

    await repository.reschedule(conn, created.delivery_id, NOW + timedelta(minutes=1))

    assert await repository.due_deliveries(conn, NOW) == ()
    retried = await repository.due_deliveries(conn, NOW + timedelta(minutes=1))
    assert len(retried) == 1
    assert retried[0].attempt_count == 1
    assert retried[0].status is DeliveryStatus.PENDING


async def test_mark_failed_takes_a_delivery_out_of_the_queue(
    repository: NotificationRepository, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    """Retries must be able to give up, or an unsendable message blocks retention forever."""
    watch = await make_watch()
    created = await repository.create_delivery(conn, "key-1", payload(watch), NOW)

    await repository.mark_failed(conn, created.delivery_id, NOW)

    assert await repository.due_deliveries(conn, NOW + timedelta(days=1)) == ()
    failed = await repository.delivery(conn, created.delivery_id)
    assert failed is not None
    assert failed.status is DeliveryStatus.FAILED


async def test_mark_delivered_takes_a_delivery_out_of_the_queue(
    repository: NotificationRepository, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    watch = await make_watch()
    created = await repository.create_delivery(conn, "key-1", payload(watch), NOW)

    await repository.mark_delivered(conn, created.delivery_id, (), None, NOW)

    assert await repository.due_deliveries(conn, NOW + timedelta(days=1)) == ()
    sent = await repository.delivery(conn, created.delivery_id)
    assert sent is not None
    assert sent.status is DeliveryStatus.SENT
    assert sent.delivered_at == NOW


# ---------------------------------------------------------------------------
# Known option keys
# ---------------------------------------------------------------------------


async def test_options_become_known_only_after_delivery(
    repository: NotificationRepository, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    """Marking on queue instead of on send would silently drop a message that never sent."""
    watch = await make_watch()
    created = await repository.create_delivery(conn, "key-1", payload(watch), NOW)
    assert await repository.known_keys(conn, watch.watch_id) == frozenset()

    await repository.mark_delivered(
        conn, created.delivery_id, tuple(item.key for item in options(2)), None, NOW
    )

    assert await repository.known_keys(conn, watch.watch_id) == {
        item.key for item in options(2)
    }


async def test_marking_a_known_option_again_keeps_the_first_notified_time(
    repository: NotificationRepository, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    watch = await make_watch()
    first = await repository.create_delivery(conn, "key-1", payload(watch), NOW)
    later = NOW + timedelta(hours=1)
    await repository.mark_delivered(conn, first.delivery_id, (option().key,), None, NOW)
    second = await repository.create_delivery(conn, "key-2", payload(watch), later)
    await repository.mark_delivered(conn, second.delivery_id, (option().key,), None, later)

    cursor = await conn.execute(
        "SELECT first_notified_at FROM notified_options WHERE watch_id = ?",
        (str(watch.watch_id),),
    )
    rows = await cursor.fetchall()
    assert len(rows) == 1
    assert rows[0]["first_notified_at"].startswith("2026-01-01T12:00:00")


async def test_known_keys_are_scoped_to_one_watch(
    repository: NotificationRepository, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    first = await make_watch(1)
    second = await make_watch(2)
    created = await repository.create_delivery(conn, "key-1", payload(first), NOW)
    await repository.mark_delivered(conn, created.delivery_id, (option().key,), None, NOW)

    assert await repository.known_keys(conn, second.watch_id) == frozenset()


async def test_deleting_a_watch_removes_its_known_options(
    repository: NotificationRepository, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    watch = await make_watch()
    created = await repository.create_delivery(conn, "key-1", payload(watch), NOW)
    await repository.mark_delivered(conn, created.delivery_id, (option().key,), None, NOW)

    await conn.execute("DELETE FROM watches WHERE id = ?", (str(watch.watch_id),))

    assert await repository.known_keys(conn, watch.watch_id) == frozenset()


# ---------------------------------------------------------------------------
# Notification state
# ---------------------------------------------------------------------------


async def test_state_defaults_before_anything_is_delivered(
    repository: NotificationRepository, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    watch = await make_watch()
    state = await repository.state(conn, watch.watch_id)
    assert state.last_best_rank is None
    assert state.degradation_notified is False
    assert state.recovery_pending is False


async def test_mark_delivered_persists_the_full_last_best_rank(
    repository: NotificationRepository, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    """The whole vector is stored, not just its sort key, so future comparisons stay exact."""
    watch = await make_watch()
    created = await repository.create_delivery(conn, "key-1", payload(watch), NOW)

    await repository.mark_delivered(conn, created.delivery_id, (), rank_vector(), NOW)

    state = await repository.state(conn, watch.watch_id)
    assert state.last_best_rank == rank_vector()


async def test_mark_delivered_without_a_rank_leaves_the_stored_rank_alone(
    repository: NotificationRepository, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    watch = await make_watch()
    first = await repository.create_delivery(conn, "key-1", payload(watch), NOW)
    await repository.mark_delivered(conn, first.delivery_id, (), rank_vector(), NOW)
    second = await repository.create_delivery(conn, "key-2", payload(watch), NOW)

    await repository.mark_delivered(conn, second.delivery_id, (), None, NOW)

    state = await repository.state(conn, watch.watch_id)
    assert state.last_best_rank == rank_vector()


async def test_delivering_a_degradation_alert_sets_both_flags(
    repository: NotificationRepository, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    """Recovery is owed from the moment degradation is announced, not from the recovery."""
    watch = await make_watch()
    created = await repository.create_delivery(
        conn, "key-1", payload(watch, kind="degradation", host="whatson.bfi.org.uk"), NOW
    )

    await repository.mark_delivered(conn, created.delivery_id, (), None, NOW)

    state = await repository.state(conn, watch.watch_id)
    assert state.degradation_notified is True
    assert state.recovery_pending is True


async def test_delivering_a_recovery_alert_clears_both_flags(
    repository: NotificationRepository, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    watch = await make_watch()
    degraded = await repository.create_delivery(
        conn, "key-1", payload(watch, kind="degradation", host="h"), NOW
    )
    await repository.mark_delivered(conn, degraded.delivery_id, (), None, NOW)
    recovered = await repository.create_delivery(
        conn, "key-2", payload(watch, kind="recovery", host="h"), NOW
    )

    await repository.mark_delivered(conn, recovered.delivery_id, (), None, NOW)

    state = await repository.state(conn, watch.watch_id)
    assert state.degradation_notified is False
    assert state.recovery_pending is False


async def test_delivering_new_options_does_not_disturb_the_degradation_flags(
    repository: NotificationRepository, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    watch = await make_watch()
    degraded = await repository.create_delivery(
        conn, "key-1", payload(watch, kind="degradation", host="h"), NOW
    )
    await repository.mark_delivered(conn, degraded.delivery_id, (), None, NOW)
    results = await repository.create_delivery(conn, "key-2", payload(watch), NOW)

    await repository.mark_delivered(conn, results.delivery_id, (option().key,), None, NOW)

    state = await repository.state(conn, watch.watch_id)
    assert state.degradation_notified is True
    assert state.recovery_pending is True


async def test_a_host_wide_alert_leaves_every_watch_state_untouched(
    repository: NotificationRepository, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    watch = await make_watch()
    created = await repository.create_delivery(
        conn, "h:1:degradation:11", payload(None, kind="degradation", host="h"), NOW
    )

    await repository.mark_delivered(conn, created.delivery_id, (), None, NOW)

    state = await repository.state(conn, watch.watch_id)
    assert state.degradation_notified is False
    cursor = await conn.execute("SELECT COUNT(*) AS n FROM notification_state")
    assert (await cursor.fetchone())["n"] == 0


# ---------------------------------------------------------------------------
# Transaction composition and error handling
# ---------------------------------------------------------------------------


async def test_mark_delivered_rolls_back_entirely_when_a_write_fails_part_way(
    repository: NotificationRepository, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    """Options marked known with no message sent would silently drop those results forever."""
    watch = await make_watch()
    created = await repository.create_delivery(conn, "key-1", payload(watch), NOW)
    failing = FailingConnection(conn, "INSERT INTO notification_state", nth=1)

    with pytest.raises(aiosqlite.OperationalError):
        await repository.mark_delivered(
            failing,  # type: ignore[arg-type]
            created.delivery_id,
            (option().key,),
            rank_vector(),
            NOW,
        )

    assert await repository.known_keys(conn, watch.watch_id) == frozenset()
    pending = await repository.delivery(conn, created.delivery_id)
    assert pending is not None
    assert pending.status is DeliveryStatus.PENDING
    assert pending.delivered_at is None


async def test_mark_delivered_rolls_back_entirely_inside_a_failing_caller_transaction(
    repository: NotificationRepository,
    database: Database,
    conn: aiosqlite.Connection,
    make_watch: WatchFactory,
) -> None:
    """Marking options known and marking the delivery sent must not split across a rollback."""
    watch = await make_watch()
    created = await repository.create_delivery(conn, "key-1", payload(watch), NOW)

    with pytest.raises(RuntimeError):
        async with database.transaction(conn):
            await repository.mark_delivered(
                conn, created.delivery_id, (option().key,), rank_vector(), NOW
            )
            raise RuntimeError("caller failed")

    assert await repository.known_keys(conn, watch.watch_id) == frozenset()
    pending = await repository.delivery(conn, created.delivery_id)
    assert pending is not None
    assert pending.status is DeliveryStatus.PENDING
    assert (await repository.state(conn, watch.watch_id)).last_best_rank is None


async def test_marking_an_unknown_delivery_is_rejected(
    repository: NotificationRepository, conn: aiosqlite.Connection
) -> None:
    with pytest.raises(InputError):
        await repository.mark_delivered(conn, uuid_for(999), (), None, NOW)


async def test_rescheduling_an_unknown_delivery_is_rejected(
    repository: NotificationRepository, conn: aiosqlite.Connection
) -> None:
    with pytest.raises(InputError):
        await repository.reschedule(conn, uuid_for(999), NOW)


async def test_delivery_returns_none_for_an_unknown_id(
    repository: NotificationRepository, conn: aiosqlite.Connection
) -> None:
    assert await repository.delivery(conn, uuid_for(999)) is None
