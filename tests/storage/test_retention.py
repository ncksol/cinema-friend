"""Tests for cinema_friend.storage.retention.

Retention deletes rows a user can still be shown, so every rule here is about what must
*survive*: the newest snapshot of each watch, any snapshot a queued message still needs
to render, and every option key the notification policy compares against.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import aiosqlite
import pytest

from cinema_friend.domain.results import NotificationPayload
from cinema_friend.domain.state import CheckOutcome, CheckTrigger
from cinema_friend.domain.watch import Watch
from cinema_friend.storage.database import Database, encode_datetime
from cinema_friend.storage.draft_repository import DraftRepository
from cinema_friend.storage.notification_repository import NotificationRepository
from cinema_friend.storage.result_repository import ResultRepository
from cinema_friend.storage.retention import RetentionService
from tests.storage.conftest import NOW, option, options

WatchFactory = Callable[..., Coroutine[Any, Any, Watch]]

RECENT = NOW - timedelta(hours=1)
OLD = NOW - timedelta(hours=25)
ANCIENT = NOW - timedelta(days=31)


@pytest.fixture
def results(database: Database) -> ResultRepository:
    return ResultRepository(database)


@pytest.fixture
def notifications(database: Database) -> NotificationRepository:
    return NotificationRepository(database)


@pytest.fixture
def service(database: Database) -> RetentionService:
    return RetentionService(database)


async def snapshot_at(
    results: ResultRepository,
    conn: aiosqlite.Connection,
    watch: Watch,
    checked_at: datetime,
    *,
    seat_label: str = "L17-L18",
) -> UUID:
    run = await results.start_check(
        conn, watch_id=watch.watch_id, trigger=CheckTrigger.SCHEDULED, started_at=checked_at
    )
    snapshot = await results.complete_with_snapshot(
        conn,
        check_run_id=run,
        watch_id=watch.watch_id,
        options=(option(seat_label),),
        checked_at=checked_at,
        outcome=CheckOutcome.SUCCESS,
        performance_count=1,
    )
    return snapshot.snapshot_id


async def snapshot_ids(conn: aiosqlite.Connection) -> set[UUID]:
    cursor = await conn.execute("SELECT id FROM result_snapshots")
    return {UUID(row["id"]) for row in await cursor.fetchall()}


async def check_run_count(conn: aiosqlite.Connection) -> int:
    cursor = await conn.execute("SELECT COUNT(*) AS n FROM check_runs")
    return int((await cursor.fetchone())["n"])


def payload(watch: Watch, snapshot_id: UUID) -> NotificationPayload:
    return NotificationPayload(
        kind="new_options",
        recipient_user_id=watch.user_id,
        watch_id=watch.watch_id,
        snapshot_id=snapshot_id,
        new_option_count=1,
        host=None,
        recovery_text=None,
    )


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


async def test_old_snapshots_are_removed(
    service: RetentionService,
    results: ResultRepository,
    conn: aiosqlite.Connection,
    make_watch: WatchFactory,
) -> None:
    watch = await make_watch()
    old = await snapshot_at(results, conn, watch, OLD, seat_label="A1")
    latest = await snapshot_at(results, conn, watch, RECENT, seat_label="B2")

    counts = await service.run(conn, NOW)

    assert await snapshot_ids(conn) == {latest}
    assert counts.snapshots_deleted == 1
    assert old not in await snapshot_ids(conn)


async def test_the_newest_snapshot_survives_however_old_it_is(
    service: RetentionService,
    results: ResultRepository,
    conn: aiosqlite.Connection,
    make_watch: WatchFactory,
) -> None:
    """A dormant watch must still be able to show its last result, not an empty page."""
    watch = await make_watch()
    only = await snapshot_at(results, conn, watch, ANCIENT)

    counts = await service.run(conn, NOW)

    assert await snapshot_ids(conn) == {only}
    assert counts.snapshots_deleted == 0


async def test_recent_snapshots_are_kept(
    service: RetentionService,
    results: ResultRepository,
    conn: aiosqlite.Connection,
    make_watch: WatchFactory,
) -> None:
    watch = await make_watch()
    first = await snapshot_at(results, conn, watch, RECENT - timedelta(minutes=5), seat_label="A1")
    second = await snapshot_at(results, conn, watch, RECENT, seat_label="B2")

    await service.run(conn, NOW)

    assert await snapshot_ids(conn) == {first, second}


async def test_each_watch_keeps_its_own_latest(
    service: RetentionService,
    results: ResultRepository,
    conn: aiosqlite.Connection,
    make_watch: WatchFactory,
) -> None:
    first = await make_watch(1)
    second = await make_watch(2)
    first_latest = await snapshot_at(results, conn, first, ANCIENT)
    second_latest = await snapshot_at(results, conn, second, ANCIENT)

    await service.run(conn, NOW)

    assert await snapshot_ids(conn) == {first_latest, second_latest}


async def test_a_snapshot_held_by_a_pending_delivery_is_retained(
    service: RetentionService,
    results: ResultRepository,
    notifications: NotificationRepository,
    conn: aiosqlite.Connection,
    make_watch: WatchFactory,
) -> None:
    """A queued message still has to render its snapshot when it is finally sent."""
    watch = await make_watch()
    held = await snapshot_at(results, conn, watch, OLD, seat_label="A1")
    await snapshot_at(results, conn, watch, RECENT, seat_label="B2")
    await notifications.create_delivery(conn, "key-1", payload(watch, held), OLD)

    counts = await service.run(conn, NOW)

    assert held in await snapshot_ids(conn)
    assert counts.snapshots_deleted == 0


async def test_a_snapshot_is_released_once_its_delivery_is_sent(
    service: RetentionService,
    results: ResultRepository,
    notifications: NotificationRepository,
    conn: aiosqlite.Connection,
    make_watch: WatchFactory,
) -> None:
    watch = await make_watch()
    held = await snapshot_at(results, conn, watch, OLD, seat_label="A1")
    latest = await snapshot_at(results, conn, watch, RECENT, seat_label="B2")
    delivery = await notifications.create_delivery(conn, "key-1", payload(watch, held), OLD)
    await notifications.mark_delivered(conn, delivery.delivery_id, (option().key,), None, OLD)

    await service.run(conn, NOW)

    assert await snapshot_ids(conn) == {latest}


async def test_a_snapshot_is_released_once_its_delivery_has_failed(
    service: RetentionService,
    results: ResultRepository,
    notifications: NotificationRepository,
    conn: aiosqlite.Connection,
    make_watch: WatchFactory,
) -> None:
    """An abandoned message must not pin its snapshot in the database forever."""
    watch = await make_watch()
    held = await snapshot_at(results, conn, watch, OLD, seat_label="A1")
    latest = await snapshot_at(results, conn, watch, RECENT, seat_label="B2")
    delivery = await notifications.create_delivery(conn, "key-1", payload(watch, held), OLD)
    await notifications.mark_failed(conn, delivery.delivery_id, OLD)

    await service.run(conn, NOW)

    assert await snapshot_ids(conn) == {latest}


async def test_deleting_a_snapshot_removes_its_options(
    service: RetentionService,
    results: ResultRepository,
    conn: aiosqlite.Connection,
    make_watch: WatchFactory,
) -> None:
    watch = await make_watch()
    old = await snapshot_at(results, conn, watch, OLD, seat_label="A1")
    await snapshot_at(results, conn, watch, RECENT, seat_label="B2")

    await service.run(conn, NOW)

    cursor = await conn.execute(
        "SELECT COUNT(*) AS n FROM result_options WHERE snapshot_id = ?", (str(old),)
    )
    assert (await cursor.fetchone())["n"] == 0


async def test_deleting_a_snapshot_leaves_its_delivery_row_intact(
    service: RetentionService,
    results: ResultRepository,
    notifications: NotificationRepository,
    conn: aiosqlite.Connection,
    make_watch: WatchFactory,
) -> None:
    """The delivery is the record that a user was told; only the pointer goes away."""
    watch = await make_watch()
    old = await snapshot_at(results, conn, watch, OLD, seat_label="A1")
    await snapshot_at(results, conn, watch, RECENT, seat_label="B2")
    delivery = await notifications.create_delivery(conn, "key-1", payload(watch, old), OLD)
    await notifications.mark_delivered(conn, delivery.delivery_id, (option().key,), None, OLD)

    await service.run(conn, NOW)

    reloaded = await notifications.delivery(conn, delivery.delivery_id)
    assert reloaded is not None
    assert reloaded.payload.snapshot_id is None


# ---------------------------------------------------------------------------
# Check runs
# ---------------------------------------------------------------------------


async def test_check_runs_older_than_thirty_days_are_removed(
    service: RetentionService,
    results: ResultRepository,
    conn: aiosqlite.Connection,
    make_watch: WatchFactory,
) -> None:
    watch = await make_watch()
    await results.start_check(
        conn, watch_id=watch.watch_id, trigger=CheckTrigger.SCHEDULED, started_at=ANCIENT
    )
    await results.start_check(
        conn, watch_id=watch.watch_id, trigger=CheckTrigger.SCHEDULED, started_at=OLD
    )

    counts = await service.run(conn, NOW)

    assert await check_run_count(conn) == 1
    assert counts.check_runs_deleted == 1


async def test_deleting_a_check_run_does_not_delete_its_snapshot(
    service: RetentionService,
    results: ResultRepository,
    conn: aiosqlite.Connection,
    make_watch: WatchFactory,
) -> None:
    """The snapshot outlives its check run: the latest result must survive either way."""
    watch = await make_watch()
    latest = await snapshot_at(results, conn, watch, ANCIENT)

    await service.run(conn, NOW)

    assert await snapshot_ids(conn) == {latest}
    assert await check_run_count(conn) == 0
    cursor = await conn.execute(
        "SELECT check_run_id FROM result_snapshots WHERE id = ?", (str(latest),)
    )
    assert (await cursor.fetchone())["check_run_id"] is None


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------


async def test_drafts_older_than_a_day_are_removed(
    service: RetentionService, conn: aiosqlite.Connection
) -> None:
    drafts = DraftRepository()
    await drafts.upsert(conn, 11, "awaiting_url", {"step": "url"}, OLD)
    await drafts.upsert(conn, 22, "awaiting_url", {"step": "url"}, RECENT)

    counts = await service.run(conn, NOW)

    assert await drafts.get(conn, 11) is None
    assert await drafts.get(conn, 22) is not None
    assert counts.drafts_deleted == 1


# ---------------------------------------------------------------------------
# What retention must never touch
# ---------------------------------------------------------------------------


async def test_notified_option_keys_survive_retention(
    service: RetentionService,
    notifications: NotificationRepository,
    conn: aiosqlite.Connection,
    make_watch: WatchFactory,
) -> None:
    """Forgetting a key would re-announce an option the user was already told about."""
    watch = await make_watch()
    delivery = await notifications.create_delivery(
        conn, "key-1", payload(watch, None), ANCIENT
    )
    await notifications.mark_delivered(
        conn, delivery.delivery_id, tuple(item.key for item in options(2)), None, ANCIENT
    )

    await service.run(conn, NOW)

    assert await notifications.known_keys(conn, watch.watch_id) == {
        item.key for item in options(2)
    }


async def test_watches_survive_retention(
    service: RetentionService, conn: aiosqlite.Connection, make_watch: WatchFactory
) -> None:
    watch = await make_watch()
    await service.run(conn, NOW)
    cursor = await conn.execute("SELECT COUNT(*) AS n FROM watches WHERE id = ?", (str(watch.watch_id),))
    assert (await cursor.fetchone())["n"] == 1


async def test_sent_deliveries_survive_retention(
    service: RetentionService,
    notifications: NotificationRepository,
    conn: aiosqlite.Connection,
    make_watch: WatchFactory,
) -> None:
    watch = await make_watch()
    delivery = await notifications.create_delivery(conn, "key-1", payload(watch, None), ANCIENT)
    await notifications.mark_delivered(conn, delivery.delivery_id, (), None, ANCIENT)

    await service.run(conn, NOW)

    assert await notifications.delivery(conn, delivery.delivery_id) is not None


# ---------------------------------------------------------------------------
# Transaction boundary
# ---------------------------------------------------------------------------


async def test_a_run_with_nothing_to_delete_reports_zeroes(
    service: RetentionService, conn: aiosqlite.Connection
) -> None:
    counts = await service.run(conn, NOW)
    assert counts.snapshots_deleted == 0
    assert counts.check_runs_deleted == 0
    assert counts.drafts_deleted == 0


async def test_a_failing_caller_transaction_rolls_the_whole_run_back(
    service: RetentionService,
    results: ResultRepository,
    database: Database,
    conn: aiosqlite.Connection,
    make_watch: WatchFactory,
) -> None:
    """Retention is one unit: a partial sweep would leave a watch with results missing."""
    watch = await make_watch()
    old = await snapshot_at(results, conn, watch, OLD, seat_label="A1")
    latest = await snapshot_at(results, conn, watch, RECENT, seat_label="B2")

    with pytest.raises(RuntimeError):
        async with database.transaction(conn):
            await service.run(conn, NOW)
            raise RuntimeError("caller failed")

    assert await snapshot_ids(conn) == {old, latest}


async def test_retention_uses_the_supplied_instant_not_the_wall_clock(
    service: RetentionService,
    results: ResultRepository,
    conn: aiosqlite.Connection,
    make_watch: WatchFactory,
) -> None:
    watch = await make_watch()
    first = await snapshot_at(results, conn, watch, RECENT - timedelta(minutes=5), seat_label="A1")
    latest = await snapshot_at(results, conn, watch, RECENT, seat_label="B2")

    await service.run(conn, datetime(2026, 1, 3, 12, 0, tzinfo=UTC))

    assert await snapshot_ids(conn) == {latest}
    assert first not in await snapshot_ids(conn)


async def test_retention_requires_an_aware_instant(
    service: RetentionService, conn: aiosqlite.Connection
) -> None:
    with pytest.raises(ValueError):
        await service.run(conn, datetime(2026, 1, 1, 12, 0))  # noqa: DTZ001


async def test_encode_datetime_bounds_are_used_for_comparison(
    service: RetentionService,
    results: ResultRepository,
    conn: aiosqlite.Connection,
    make_watch: WatchFactory,
) -> None:
    """A snapshot exactly on the 24-hour boundary is still inside the window."""
    watch = await make_watch()
    boundary = NOW - timedelta(hours=24)
    on_boundary = await snapshot_at(results, conn, watch, boundary, seat_label="A1")
    latest = await snapshot_at(results, conn, watch, RECENT, seat_label="B2")
    cursor = await conn.execute(
        "SELECT checked_at FROM result_snapshots WHERE id = ?", (str(on_boundary),)
    )
    assert (await cursor.fetchone())["checked_at"] == encode_datetime(boundary)

    await service.run(conn, NOW)

    assert await snapshot_ids(conn) == {on_boundary, latest}
