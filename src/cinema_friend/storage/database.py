"""SQLite connection lifecycle, transactions, and migration discovery."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import aiosqlite

_MIGRATIONS_DIR: Final = Path(__file__).parent / "migrations"
_MIGRATION_NAME_RE: Final = re.compile(r"^(\d+)_.*\.sql$")


def encode_datetime(value: datetime) -> str:
    """Serialize an aware datetime as a fixed-width UTC ISO 8601 string ending in ``+00:00``.

    Microseconds are always included so every encoded value has the same length and
    lexicographic (text) ordering matches chronological ordering, which callers rely on
    for ``WHERE`` clauses comparing stored timestamps.
    """
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def decode_datetime(value: str) -> datetime:
    """Parse a UTC ISO 8601 string produced by :func:`encode_datetime`."""
    return datetime.fromisoformat(value)


def _split_statements(script: str) -> list[str]:
    """Split a migration file into individual statements on ``;`` boundaries.

    Migration files are authored by this project and contain no string literals or
    identifiers with embedded semicolons, so a naive split is safe and keeps migration
    execution under our own explicit transaction control instead of relying on
    ``executescript``'s implicit-commit behaviour.
    """
    return [statement.strip() for statement in script.split(";") if statement.strip()]


def _discover_migrations() -> list[tuple[int, Path]]:
    migrations: list[tuple[int, Path]] = []
    for path in _MIGRATIONS_DIR.glob("*.sql"):
        match = _MIGRATION_NAME_RE.match(path.name)
        if match is None:
            raise ValueError(f"migration file name is not numbered: {path.name}")
        migrations.append((int(match.group(1)), path))
    migrations.sort(key=lambda item: item[0])
    return migrations


class Database:
    """Owns connection setup, transactions, and migrations for one SQLite file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def connect(self) -> aiosqlite.Connection:
        """Open a fresh connection with row access by name, FKs, and WAL enabled."""
        conn = await aiosqlite.connect(self._path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @asynccontextmanager
    async def transaction(self, conn: aiosqlite.Connection) -> AsyncIterator[aiosqlite.Connection]:
        """Run a block as one ``BEGIN IMMEDIATE`` transaction on ``conn``.

        Commits when the block completes normally; rolls back and re-raises on any
        exception, including ``BaseException`` subclasses such as cancellation.
        """
        await conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            await conn.rollback()
            raise
        else:
            await conn.commit()

    async def migrate(self, conn: aiosqlite.Connection) -> None:
        """Bootstrap ``schema_migrations`` and apply any unapplied numbered migration."""
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        await conn.commit()
        cursor = await conn.execute("SELECT version FROM schema_migrations")
        applied = {row["version"] for row in await cursor.fetchall()}
        for version, path in _discover_migrations():
            if version in applied:
                continue
            statements = _split_statements(path.read_text())
            async with self.transaction(conn):
                for statement in statements:
                    await conn.execute(statement)
                await conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, encode_datetime(datetime.now(UTC))),
                )
