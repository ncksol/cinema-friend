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


async def test_transaction_starts_after_an_unwrapped_write(
    database: Database, conn: aiosqlite.Connection
) -> None:
    """A write made outside ``transaction()`` must not leave a transaction open."""
    await database.migrate(conn)
    await conn.execute(
        "INSERT INTO host_circuits (host, state, step, updated_at) VALUES (?, ?, ?, ?)",
        ("unwrapped.test", "closed", 0, "2026-01-01T00:00:00.000000+00:00"),
    )

    async with database.transaction(conn):
        await conn.execute(
            "INSERT INTO host_circuits (host, state, step, updated_at) VALUES (?, ?, ?, ?)",
            ("wrapped.test", "closed", 0, "2026-01-01T00:00:00.000000+00:00"),
        )

    cursor = await conn.execute("SELECT host FROM host_circuits ORDER BY host")
    assert [row["host"] for row in await cursor.fetchall()] == ["unwrapped.test", "wrapped.test"]


async def test_unwrapped_write_survives_connection_close(database: Database) -> None:
    """Writes outside ``transaction()`` autocommit; closing must not discard them."""
    first = await database.connect()
    try:
        await database.migrate(first)
        await first.execute(
            "INSERT INTO host_circuits (host, state, step, updated_at) VALUES (?, ?, ?, ?)",
            ("example.test", "closed", 0, "2026-01-01T00:00:00.000000+00:00"),
        )
    finally:
        await first.close()

    second = await database.connect()
    try:
        cursor = await second.execute("SELECT COUNT(*) FROM host_circuits")
        assert (await cursor.fetchone())[0] == 1
    finally:
        await second.close()


async def test_a_second_connection_can_be_opened_while_the_first_is_alive(
    database: Database, conn: aiosqlite.Connection
) -> None:
    """Connection setup must not leave an open cursor holding a read lock on the file."""
    second = await database.connect()
    try:
        cursor = await second.execute("SELECT 1")
        assert (await cursor.fetchone())[0] == 1
    finally:
        await second.close()


async def test_committed_write_is_visible_to_a_second_connection(database: Database) -> None:
    writer = await database.connect()
    reader = await database.connect()
    try:
        await database.migrate(writer)
        async with database.transaction(writer):
            await writer.execute(
                "INSERT INTO host_circuits (host, state, step, updated_at) VALUES (?, ?, ?, ?)",
                ("example.test", "closed", 0, "2026-01-01T00:00:00.000000+00:00"),
            )

        cursor = await reader.execute("SELECT host FROM host_circuits")
        row = await cursor.fetchone()
        assert row is not None
        assert row["host"] == "example.test"
    finally:
        await reader.close()
        await writer.close()


async def test_nested_transaction_commits_with_the_outer_transaction(
    database: Database, conn: aiosqlite.Connection
) -> None:
    await database.migrate(conn)

    async with database.transaction(conn):
        await conn.execute(
            "INSERT INTO host_circuits (host, state, step, updated_at) VALUES (?, ?, ?, ?)",
            ("outer.test", "closed", 0, "2026-01-01T00:00:00.000000+00:00"),
        )
        async with database.transaction(conn):
            await conn.execute(
                "INSERT INTO host_circuits (host, state, step, updated_at) VALUES (?, ?, ?, ?)",
                ("inner.test", "closed", 0, "2026-01-01T00:00:00.000000+00:00"),
            )

    cursor = await conn.execute("SELECT host FROM host_circuits ORDER BY host")
    assert [row["host"] for row in await cursor.fetchall()] == ["inner.test", "outer.test"]


async def test_nested_transaction_rollback_keeps_outer_work(
    database: Database, conn: aiosqlite.Connection
) -> None:
    """A failed inner block unwinds only its own statements."""
    await database.migrate(conn)

    class _Boom(Exception):
        pass

    async with database.transaction(conn):
        await conn.execute(
            "INSERT INTO host_circuits (host, state, step, updated_at) VALUES (?, ?, ?, ?)",
            ("outer.test", "closed", 0, "2026-01-01T00:00:00.000000+00:00"),
        )
        with pytest.raises(_Boom):
            async with database.transaction(conn):
                await conn.execute(
                    "INSERT INTO host_circuits (host, state, step, updated_at) VALUES (?, ?, ?, ?)",
                    ("inner.test", "closed", 0, "2026-01-01T00:00:00.000000+00:00"),
                )
                raise _Boom("simulated inner failure")
        await conn.execute(
            "INSERT INTO host_circuits (host, state, step, updated_at) VALUES (?, ?, ?, ?)",
            ("after.test", "closed", 0, "2026-01-01T00:00:00.000000+00:00"),
        )

    cursor = await conn.execute("SELECT host FROM host_circuits ORDER BY host")
    assert [row["host"] for row in await cursor.fetchall()] == ["after.test", "outer.test"]


async def test_outer_rollback_discards_committed_nested_work(
    database: Database, conn: aiosqlite.Connection
) -> None:
    """A nested block that succeeded is still undone when the outer block fails."""
    await database.migrate(conn)

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        async with database.transaction(conn):
            async with database.transaction(conn):
                await conn.execute(
                    "INSERT INTO host_circuits (host, state, step, updated_at) VALUES (?, ?, ?, ?)",
                    ("inner.test", "closed", 0, "2026-01-01T00:00:00.000000+00:00"),
                )
            raise _Boom("simulated outer failure")

    cursor = await conn.execute("SELECT COUNT(*) FROM host_circuits")
    assert (await cursor.fetchone())[0] == 0


async def test_sequential_transactions_reuse_one_connection(
    database: Database, conn: aiosqlite.Connection
) -> None:
    """Committing one transaction must leave the connection ready for the next."""
    await database.migrate(conn)
    for host in ("first.test", "second.test"):
        async with database.transaction(conn):
            await conn.execute(
                "INSERT INTO host_circuits (host, state, step, updated_at) VALUES (?, ?, ?, ?)",
                (host, "closed", 0, "2026-01-01T00:00:00.000000+00:00"),
            )

    cursor = await conn.execute("SELECT COUNT(*) FROM host_circuits")
    assert (await cursor.fetchone())[0] == 2


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
