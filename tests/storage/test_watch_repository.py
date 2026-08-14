"""Tests for cinema_friend.storage.watch_repository."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import UUID

import aiosqlite
import pytest

from cinema_friend.domain.errors import InputError
from cinema_friend.domain.state import SeatPreferenceStrategy, WatchMode, WatchStatus
from cinema_friend.domain.watch import Watch, WatchCriteria
from cinema_friend.storage.database import Database
from cinema_friend.storage.watch_repository import WatchRepository

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _uuid(suffix: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{suffix:012d}")


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
        "watch_id": _uuid(1),
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


@pytest.mark.parametrize(
    "strategy",
    [
        SeatPreferenceStrategy.ONLY_BEST,
        SeatPreferenceStrategy.BEST_AND_GOOD,
    ],
)
async def test_simple_seat_preference_strategy_round_trips(
    conn: aiosqlite.Connection,
    repo: WatchRepository,
    strategy: SeatPreferenceStrategy,
) -> None:
    watch = _watch(criteria=_criteria(seat_preference_strategy=strategy))

    await repo.create(conn, watch)

    assert await repo.get(conn, watch.watch_id) == watch


async def test_new_criteria_json_writes_an_explicit_advanced_strategy(
    conn: aiosqlite.Connection,
    repo: WatchRepository,
) -> None:
    await repo.create(conn, _watch())

    cursor = await conn.execute(
        "SELECT criteria_json FROM watches WHERE id = ?", (str(_uuid(1)),)
    )
    row = await cursor.fetchone()

    assert row is not None
    assert json.loads(row["criteria_json"])["seat_preference_strategy"] == "advanced"


async def test_legacy_criteria_without_a_strategy_decode_as_advanced(
    conn: aiosqlite.Connection,
    repo: WatchRepository,
) -> None:
    watch = _watch()
    await repo.create(conn, watch)
    cursor = await conn.execute("SELECT criteria_json FROM watches WHERE id = ?", (str(watch.watch_id),))
    row = await cursor.fetchone()
    assert row is not None
    legacy_payload = json.loads(row["criteria_json"])
    legacy_payload.pop("seat_preference_strategy", None)
    await conn.execute(
        "UPDATE watches SET criteria_json = ? WHERE id = ?",
        (json.dumps(legacy_payload, sort_keys=True), str(watch.watch_id)),
    )

    fetched = await repo.get(conn, watch.watch_id)

    assert fetched is not None
    assert fetched.criteria.seat_preference_strategy is SeatPreferenceStrategy.ADVANCED


async def test_weekend_time_override_round_trips(
    conn: aiosqlite.Connection,
    repo: WatchRepository,
) -> None:
    watch = _watch(
        criteria=_criteria(
            weekend_time_from=time(12, 0),
            weekend_time_to=time(16, 0),
        )
    )

    await repo.create(conn, watch)

    assert await repo.get(conn, watch.watch_id) == watch


async def test_uniform_schedule_writes_null_weekend_bounds(
    conn: aiosqlite.Connection,
    repo: WatchRepository,
) -> None:
    watch = _watch()
    await repo.create(conn, watch)

    cursor = await conn.execute(
        "SELECT criteria_json FROM watches WHERE id = ?",
        (str(watch.watch_id),),
    )
    row = await cursor.fetchone()

    assert row is not None
    payload = json.loads(row["criteria_json"])
    assert payload["weekend_time_from"] is None
    assert payload["weekend_time_to"] is None


async def test_legacy_criteria_without_weekend_bounds_keep_the_default_window(
    conn: aiosqlite.Connection,
    repo: WatchRepository,
) -> None:
    watch = _watch()
    await repo.create(conn, watch)
    cursor = await conn.execute(
        "SELECT criteria_json FROM watches WHERE id = ?",
        (str(watch.watch_id),),
    )
    row = await cursor.fetchone()
    assert row is not None
    payload = json.loads(row["criteria_json"])
    payload.pop("weekend_time_from")
    payload.pop("weekend_time_to")
    await conn.execute(
        "UPDATE watches SET criteria_json = ? WHERE id = ?",
        (json.dumps(payload, sort_keys=True), str(watch.watch_id)),
    )

    fetched = await repo.get(conn, watch.watch_id)

    assert fetched is not None
    assert fetched.criteria.weekend_time_from is None
    assert fetched.criteria.weekend_time_to is None


async def test_partial_persisted_weekend_window_is_rejected(
    conn: aiosqlite.Connection,
    repo: WatchRepository,
) -> None:
    watch = _watch()
    await repo.create(conn, watch)
    cursor = await conn.execute(
        "SELECT criteria_json FROM watches WHERE id = ?",
        (str(watch.watch_id),),
    )
    row = await cursor.fetchone()
    assert row is not None
    payload = json.loads(row["criteria_json"])
    payload["weekend_time_from"] = "12:00:00"
    payload["weekend_time_to"] = None
    await conn.execute(
        "UPDATE watches SET criteria_json = ? WHERE id = ?",
        (json.dumps(payload, sort_keys=True), str(watch.watch_id)),
    )

    with pytest.raises(InputError, match="weekend"):
        await repo.get(conn, watch.watch_id)


async def test_create_stores_the_canonical_uuid_string(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    """The TEXT primary key holds the canonical UUID text, not an opaque encoding."""
    watch = _watch()
    await repo.create(conn, watch)

    cursor = await conn.execute("SELECT id FROM watches")
    row = await cursor.fetchone()
    assert row is not None
    assert row["id"] == str(watch.watch_id)


async def test_title_and_last_check_at_round_trip(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    last_check = _NOW - timedelta(minutes=5)
    watch = _watch(title="Dog Stars", last_check_at=last_check)

    await repo.create(conn, watch)
    fetched = await repo.get(conn, watch.watch_id)

    assert fetched is not None
    assert fetched.title == "Dog Stars"
    assert fetched.last_check_at == last_check


async def test_title_and_last_check_at_default_to_none(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    await repo.create(conn, _watch())

    fetched = await repo.get(conn, _uuid(1))

    assert fetched is not None
    assert fetched.title is None
    assert fetched.last_check_at is None


async def test_get_missing_watch_returns_none(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    assert await repo.get(conn, _uuid(999)) is None


async def test_list_for_owner_excludes_other_owners_ordered_by_created_at(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    earlier = _watch(watch_id=_uuid(1), user_id=11, created_at=_NOW, updated_at=_NOW)
    later = _watch(
        watch_id=_uuid(2),
        user_id=11,
        created_at=_NOW + timedelta(hours=1),
        updated_at=_NOW + timedelta(hours=1),
    )
    other_owner = _watch(watch_id=_uuid(3), user_id=22, created_at=_NOW, updated_at=_NOW)
    for watch in (later, earlier, other_owner):
        await repo.create(conn, watch)

    result = await repo.list_for_owner(conn, 11)

    assert [w.watch_id for w in result] == [_uuid(1), _uuid(2)]


async def test_list_due_includes_active_watch_past_due(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    due = _watch(watch_id=_uuid(1), next_check_at=_NOW - timedelta(minutes=1))
    await repo.create(conn, due)

    result = await repo.list_due(conn, _NOW)

    assert [w.watch_id for w in result] == [_uuid(1)]


async def test_list_due_excludes_future_next_run_at(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    future = _watch(watch_id=_uuid(1), next_check_at=_NOW + timedelta(minutes=1))
    await repo.create(conn, future)

    result = await repo.list_due(conn, _NOW)

    assert result == ()


async def test_list_due_includes_backoff_watch_whose_probe_time_has_arrived(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    backoff = _watch(
        watch_id=_uuid(1), status=WatchStatus.BACKOFF, next_check_at=_NOW - timedelta(minutes=1)
    )
    await repo.create(conn, backoff)

    result = await repo.list_due(conn, _NOW)

    assert [w.watch_id for w in result] == [_uuid(1)]


async def test_list_due_excludes_backoff_watch_still_waiting_out_its_probe_time(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    backoff = _watch(
        watch_id=_uuid(1), status=WatchStatus.BACKOFF, next_check_at=_NOW + timedelta(minutes=1)
    )
    await repo.create(conn, backoff)

    result = await repo.list_due(conn, _NOW)

    assert result == ()


async def test_list_due_orders_active_and_backoff_watches_together(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    await repo.create(
        conn,
        _watch(
            watch_id=_uuid(1), status=WatchStatus.ACTIVE, next_check_at=_NOW - timedelta(minutes=1)
        ),
    )
    await repo.create(
        conn,
        _watch(
            watch_id=_uuid(2), status=WatchStatus.BACKOFF, next_check_at=_NOW - timedelta(minutes=5)
        ),
    )

    result = await repo.list_due(conn, _NOW)

    assert [w.watch_id for w in result] == [_uuid(2), _uuid(1)]


async def test_list_due_excludes_paused_watch(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    paused = _watch(
        watch_id=_uuid(1), status=WatchStatus.PAUSED, next_check_at=_NOW - timedelta(minutes=1)
    )
    await repo.create(conn, paused)

    result = await repo.list_due(conn, _NOW)

    assert result == ()


async def test_list_due_excludes_watch_with_no_next_run_at(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    completed = _watch(watch_id=_uuid(1), status=WatchStatus.COMPLETED, next_check_at=None)
    await repo.create(conn, completed)

    result = await repo.list_due(conn, _NOW)

    assert result == ()


async def test_list_active_owner_ids_returns_distinct_owners_for_host(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    bfi_url = "https://whatson.bfi.org.uk/imax/Online/article/dog-stars"
    other_host_criteria = _criteria(source_url="https://example.test/article/dog-stars")
    await repo.create(
        conn, _watch(watch_id=_uuid(1), user_id=11, criteria=_criteria(source_url=bfi_url))
    )
    await repo.create(
        conn, _watch(watch_id=_uuid(2), user_id=11, criteria=_criteria(source_url=bfi_url))
    )
    await repo.create(
        conn, _watch(watch_id=_uuid(3), user_id=22, criteria=_criteria(source_url=bfi_url))
    )
    await repo.create(
        conn,
        _watch(
            watch_id=_uuid(4),
            user_id=33,
            status=WatchStatus.PAUSED,
            criteria=_criteria(source_url=bfi_url),
        ),
    )
    await repo.create(conn, _watch(watch_id=_uuid(5), user_id=44, criteria=other_host_criteria))

    result = await repo.list_active_owner_ids(conn, "whatson.bfi.org.uk")

    assert result == frozenset({11, 22})
    assert await repo.list_active_owner_ids(conn, "example.test") == frozenset({44})


async def test_list_active_owner_ids_includes_backoff_owners(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    """A watch waiting out host backoff still needs its owner told when the host recovers."""
    bfi_url = "https://whatson.bfi.org.uk/imax/Online/article/dog-stars"
    await repo.create(
        conn,
        _watch(
            watch_id=_uuid(1),
            user_id=11,
            status=WatchStatus.BACKOFF,
            criteria=_criteria(source_url=bfi_url),
        ),
    )
    terminal = ((2, WatchStatus.PAUSED), (3, WatchStatus.EXPIRED), (4, WatchStatus.FAILED))
    for suffix, status in terminal:
        await repo.create(
            conn,
            _watch(
                watch_id=_uuid(suffix),
                user_id=20 + suffix,
                status=status,
                criteria=_criteria(source_url=bfi_url),
            ),
        )

    result = await repo.list_active_owner_ids(conn, "whatson.bfi.org.uk")

    assert result == frozenset({11})


async def test_update_changes_status_and_updated_at_without_touching_created_at(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    created = _NOW
    watch = _watch(watch_id=_uuid(1), created_at=created, updated_at=created)
    await repo.create(conn, watch)

    changed = Watch(
        watch_id=_uuid(1),
        user_id=watch.user_id,
        criteria=watch.criteria,
        status=WatchStatus.PAUSED,
        created_at=created,
        updated_at=_NOW + timedelta(hours=2),
        next_check_at=None,
    )
    await repo.update(conn, changed)

    fetched = await repo.get(conn, _uuid(1))
    assert fetched is not None
    assert fetched.status is WatchStatus.PAUSED
    assert fetched.updated_at == _NOW + timedelta(hours=2)
    assert fetched.created_at == created
    assert fetched.next_check_at is None


async def test_update_persists_title_and_last_check_at_from_the_watch(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    """The domain object is the only source of truth for both columns."""
    await repo.create(conn, _watch(watch_id=_uuid(1)))

    checked = _NOW + timedelta(minutes=30)
    await repo.update(
        conn,
        _watch(watch_id=_uuid(1), title="Dog Stars", last_check_at=checked, updated_at=checked),
    )

    fetched = await repo.get(conn, _uuid(1))
    assert fetched is not None
    assert fetched.title == "Dog Stars"
    assert fetched.last_check_at == checked


async def test_update_round_trip_preserves_title_and_last_check_at(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    """Read-modify-write carries stored values forward without a separate keyword."""
    checked = _NOW - timedelta(minutes=5)
    await repo.create(conn, _watch(watch_id=_uuid(1), title="Dog Stars", last_check_at=checked))

    stored = await repo.get(conn, _uuid(1))
    assert stored is not None
    await repo.update(conn, replace(stored, status=WatchStatus.PAUSED))

    fetched = await repo.get(conn, _uuid(1))
    assert fetched is not None
    assert fetched.status is WatchStatus.PAUSED
    assert fetched.title == "Dog Stars"
    assert fetched.last_check_at == checked


async def test_delete_removes_watch(conn: aiosqlite.Connection, repo: WatchRepository) -> None:
    await repo.create(conn, _watch(watch_id=_uuid(1)))

    await repo.delete(conn, _uuid(1))

    assert await repo.get(conn, _uuid(1)) is None


async def test_delete_cascades_to_check_runs(
    conn: aiosqlite.Connection, repo: WatchRepository
) -> None:
    await repo.create(conn, _watch(watch_id=_uuid(1)))
    await conn.execute(
        """
        INSERT INTO check_runs (id, watch_id, trigger, outcome, started_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("check-1", str(_uuid(1)), "manual", "success", _NOW.isoformat(timespec="microseconds")),
    )
    await conn.commit()

    await repo.delete(conn, _uuid(1))

    cursor = await conn.execute("SELECT COUNT(*) FROM check_runs")
    assert (await cursor.fetchone())[0] == 0
