"""Tests for cinema_friend.storage.watch_repository."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import aiosqlite
import pytest

from cinema_friend.domain.state import WatchMode, WatchStatus
from cinema_friend.domain.watch import Watch, WatchCriteria
from cinema_friend.storage.database import Database
from cinema_friend.storage.watch_repository import WatchRepository

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _criteria(**overrides: object) -> WatchCriteria:
    defaults: dict[str, object] = {
        "source_url": "https://whatson.bfi.org.uk/imax/Online/article/dog-stars",
        "slug": "dog-stars",
        "date_from": date(2026, 8, 26),
        "date_to": date(2026, 8, 30),
        "time_from": time(18, 0),
        "time_to": time(23, 0),
        "quantity": 2,
        "mode": WatchMode.ONE_OFF,
    }
    defaults.update(overrides)
    return WatchCriteria(**defaults)  # type: ignore[arg-type]


def _watch(**overrides: object) -> Watch:
    defaults: dict[str, object] = {
        "watch_id": 1,
        "user_id": 11,
        "criteria": _criteria(),
        "status": WatchStatus.ACTIVE,
        "created_at": _NOW,
        "updated_at": _NOW,
        "next_check_at": _NOW,
    }
    defaults.update(overrides)
    return Watch(**defaults)  # type: ignore[arg-type]


@pytest.fixture
async def conn(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    database = Database(tmp_path / "watches.db")
    connection = await database.connect()
    await database.migrate(connection)
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture
def repo() -> WatchRepository:
    return WatchRepository()


async def test_create_and_get_round_trip(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    criteria = _criteria(
        mode=WatchMode.RECURRING,
        interval=timedelta(minutes=30),
        preferred_seats=frozenset({"L18", "L17"}),
        excluded_seats=frozenset({"L1"}),
        preferred_rows=frozenset({"L", "K"}),
        excluded_rows=frozenset({"A"}),
        preferred_utc_instant=datetime(2026, 8, 27, 19, 0, tzinfo=UTC),
    )
    watch = _watch(criteria=criteria)

    await repo.create(conn, watch)
    fetched = await repo.get(conn, watch.watch_id)

    assert fetched == watch


async def test_get_missing_watch_returns_none(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    assert await repo.get(conn, 999) is None


async def test_list_for_owner_excludes_other_owners_ordered_by_created_at(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    earlier = _watch(watch_id=1, user_id=11, created_at=_NOW, updated_at=_NOW)
    later = _watch(
        watch_id=2,
        user_id=11,
        created_at=_NOW + timedelta(hours=1),
        updated_at=_NOW + timedelta(hours=1),
    )
    other_owner = _watch(watch_id=3, user_id=22, created_at=_NOW, updated_at=_NOW)
    for watch in (later, earlier, other_owner):
        await repo.create(conn, watch)

    result = await repo.list_for_owner(conn, 11)

    assert [w.watch_id for w in result] == [1, 2]


async def test_list_due_includes_active_watch_past_due(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    due = _watch(watch_id=1, next_check_at=_NOW - timedelta(minutes=1))
    await repo.create(conn, due)

    result = await repo.list_due(conn, _NOW)

    assert [w.watch_id for w in result] == [1]


async def test_list_due_excludes_future_next_run_at(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    future = _watch(watch_id=1, next_check_at=_NOW + timedelta(minutes=1))
    await repo.create(conn, future)

    result = await repo.list_due(conn, _NOW)

    assert result == ()


async def test_list_due_excludes_paused_watch(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    paused = _watch(
        watch_id=1, status=WatchStatus.PAUSED, next_check_at=_NOW - timedelta(minutes=1)
    )
    await repo.create(conn, paused)

    result = await repo.list_due(conn, _NOW)

    assert result == ()


async def test_list_due_excludes_watch_with_no_next_run_at(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    completed = _watch(
        watch_id=1, status=WatchStatus.COMPLETED, next_check_at=None
    )
    await repo.create(conn, completed)

    result = await repo.list_due(conn, _NOW)

    assert result == ()


async def test_list_active_owner_ids_returns_distinct_owners_for_host(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    bfi_url = "https://whatson.bfi.org.uk/imax/Online/article/dog-stars"
    other_host_criteria = _criteria(source_url="https://example.test/article/dog-stars")
    await repo.create(conn, _watch(watch_id=1, user_id=11, criteria=_criteria(source_url=bfi_url)))
    await repo.create(conn, _watch(watch_id=2, user_id=11, criteria=_criteria(source_url=bfi_url)))
    await repo.create(conn, _watch(watch_id=3, user_id=22, criteria=_criteria(source_url=bfi_url)))
    await repo.create(
        conn,
        _watch(watch_id=4, user_id=33, status=WatchStatus.PAUSED, criteria=_criteria(source_url=bfi_url)),
    )
    await repo.create(conn, _watch(watch_id=5, user_id=44, criteria=other_host_criteria))

    result = await repo.list_active_owner_ids(conn, "whatson.bfi.org.uk")

    assert result == frozenset({11, 22})
    assert await repo.list_active_owner_ids(conn, "example.test") == frozenset({44})


async def test_update_changes_status_and_updated_at_without_touching_created_at(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    created = _NOW
    watch = _watch(watch_id=1, created_at=created, updated_at=created)
    await repo.create(conn, watch)

    changed = Watch(
        watch_id=1,
        user_id=watch.user_id,
        criteria=watch.criteria,
        status=WatchStatus.PAUSED,
        created_at=created,
        updated_at=_NOW + timedelta(hours=2),
        next_check_at=None,
    )
    await repo.update(conn, changed)

    fetched = await repo.get(conn, 1)
    assert fetched is not None
    assert fetched.status is WatchStatus.PAUSED
    assert fetched.updated_at == _NOW + timedelta(hours=2)
    assert fetched.created_at == created
    assert fetched.next_check_at is None


async def test_update_without_last_check_at_preserves_existing_value(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    watch = _watch(watch_id=1)
    await repo.create(conn, watch)
    await repo.update(conn, watch, last_check_at=_NOW)

    cursor = await conn.execute("SELECT last_check_at FROM watches WHERE id = ?", ("1",))
    row = await cursor.fetchone()
    assert row["last_check_at"] == _NOW.isoformat(timespec="microseconds")

    await repo.update(conn, watch)

    cursor = await conn.execute("SELECT last_check_at FROM watches WHERE id = ?", ("1",))
    row = await cursor.fetchone()
    assert row["last_check_at"] == _NOW.isoformat(timespec="microseconds")


async def test_delete_removes_watch(conn: aiosqlite.Connection, repo: WatchRepository) -> None:
    watch = _watch(watch_id=1)
    await repo.create(conn, watch)

    await repo.delete(conn, 1)

    assert await repo.get(conn, 1) is None


async def test_delete_cascades_to_check_runs(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    watch = _watch(watch_id=1)
    await repo.create(conn, watch)
    await conn.execute(
        """
        INSERT INTO check_runs (id, watch_id, trigger, outcome, started_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("check-1", "1", "manual", "success", _NOW.isoformat(timespec="microseconds")),
    )
    await conn.commit()

    await repo.delete(conn, 1)

    cursor = await conn.execute("SELECT COUNT(*) FROM check_runs")
    assert (await cursor.fetchone())[0] == 0
