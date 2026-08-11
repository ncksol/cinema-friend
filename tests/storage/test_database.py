"""Tests for cinema_friend.storage.database: connection setup, transactions, migrations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from cinema_friend.storage.database import Database

_ALL_TABLES = {
    "schema_migrations",
    "conversation_drafts",
    "watches",
    "check_runs",
    "result_snapshots",
    "result_options",
    "notified_options",
    "notification_state",
    "notification_deliveries",
    "host_circuits",
}


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    return Database(tmp_path / "cinema-friend.db")


@pytest.fixture
async def conn(database: Database) -> AsyncIterator[aiosqlite.Connection]:
    connection = await database.connect()
    try:
        yield connection
    finally:
        await connection.close()


async def _table_names(connection: aiosqlite.Connection) -> set[str]:
    cursor = await connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )
    rows = await cursor.fetchall()
    return {row["name"] for row in rows}


async def test_connect_enables_wal_mode(database: Database, conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("PRAGMA journal_mode")
    row = await cursor.fetchone()
    assert row[0].lower() == "wal"


async def test_connect_enables_foreign_keys(
    database: Database, conn: aiosqlite.Connection
) -> None:
    cursor = await conn.execute("PRAGMA foreign_keys")
    row = await cursor.fetchone()
    assert row[0] == 1


async def test_connect_uses_row_factory(database: Database, conn: aiosqlite.Connection) -> None:
    await database.migrate(conn)
    await conn.execute(
        "INSERT INTO host_circuits (host, state, step, updated_at) VALUES (?, ?, ?, ?)",
        ("example.test", "closed", 0, "2026-01-01T00:00:00.000000+00:00"),
    )
    cursor = await conn.execute("SELECT host FROM host_circuits")
    row = await cursor.fetchone()
    assert row["host"] == "example.test"


async def test_migrate_creates_every_table(database: Database, conn: aiosqlite.Connection) -> None:
    await database.migrate(conn)
    assert _ALL_TABLES <= await _table_names(conn)


async def test_migrate_records_applied_version(
    database: Database, conn: aiosqlite.Connection
) -> None:
    await database.migrate(conn)
    cursor = await conn.execute("SELECT version FROM schema_migrations")
    rows = await cursor.fetchall()
    assert [row["version"] for row in rows] == [1]


async def test_migrate_is_idempotent(database: Database, conn: aiosqlite.Connection) -> None:
    await database.migrate(conn)
    await database.migrate(conn)
    cursor = await conn.execute("SELECT version FROM schema_migrations")
    rows = await cursor.fetchall()
    assert [row["version"] for row in rows] == [1]


async def test_migrate_is_idempotent_across_connections(
    database: Database, tmp_path: Path
) -> None:
    first = await database.connect()
    try:
        await database.migrate(first)
    finally:
        await first.close()

    second = await database.connect()
    try:
        await database.migrate(second)
        cursor = await second.execute("SELECT version FROM schema_migrations")
        rows = await cursor.fetchall()
        assert [row["version"] for row in rows] == [1]
    finally:
        await second.close()


async def test_transaction_commits_on_success(
    database: Database, conn: aiosqlite.Connection
) -> None:
    await database.migrate(conn)
    async with database.transaction(conn):
        await conn.execute(
            "INSERT INTO host_circuits (host, state, step, updated_at) VALUES (?, ?, ?, ?)",
            ("example.test", "closed", 0, "2026-01-01T00:00:00.000000+00:00"),
        )

    cursor = await conn.execute("SELECT COUNT(*) FROM host_circuits")
    row = await cursor.fetchone()
    assert row[0] == 1


async def test_transaction_rolls_back_on_exception(
    database: Database, conn: aiosqlite.Connection
) -> None:
    await database.migrate(conn)

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        async with database.transaction(conn):
            await conn.execute(
                "INSERT INTO host_circuits (host, state, step, updated_at) VALUES (?, ?, ?, ?)",
                ("example.test", "closed", 0, "2026-01-01T00:00:00.000000+00:00"),
            )
            raise _Boom("simulated failure")

    cursor = await conn.execute("SELECT COUNT(*) FROM host_circuits")
    row = await cursor.fetchone()
    assert row[0] == 0
    # The connection must still be usable after a rollback.
    cursor = await conn.execute("SELECT 1")
    assert (await cursor.fetchone())[0] == 1


async def test_cascade_deletes_dependent_check_run(
    database: Database, conn: aiosqlite.Connection
) -> None:
    await database.migrate(conn)
    now = datetime(2026, 1, 1, tzinfo=UTC).isoformat(timespec="microseconds")
    await conn.execute(
        """
        INSERT INTO watches (
            id, owner_user_id, source_url, slug, criteria_json, mode, status,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("1", 11, "https://whatson.bfi.org.uk/imax/Online/article/dog-stars", "dog-stars",
         "{}", "one_off", "active", now, now),
    )
    await conn.execute(
        """
        INSERT INTO check_runs (
            id, watch_id, trigger, outcome, started_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        ("check-1", "1", "manual", "success", now),
    )
    await conn.commit()

    cursor = await conn.execute("SELECT COUNT(*) FROM check_runs")
    assert (await cursor.fetchone())[0] == 1

    await conn.execute("DELETE FROM watches WHERE id = ?", ("1",))
    await conn.commit()

    cursor = await conn.execute("SELECT COUNT(*) FROM check_runs")
    assert (await cursor.fetchone())[0] == 0
