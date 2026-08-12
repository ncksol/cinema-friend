"""SQLite connection lifecycle, transactions, and migration discovery."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Final

import aiosqlite

from cinema_friend.domain.errors import PersistenceError

_MIGRATIONS_DIR: Final = Path(__file__).parent / "migrations"
_MIGRATION_NAME_RE: Final = re.compile(r"^(\d+)_.*\.sql$")
_SAVEPOINT_NAMES: Final = count()

DEFAULT_BUSY_TIMEOUT_MS: Final = 5000
"""How long a connection waits for a lock before giving up.

Writers here are short and several of them are independent -- checks, circuit-breaker
updates, delivery bookkeeping -- so meeting a held write lock is normal and waiting a
few seconds is the correct response. The SQLite default is zero, which turns every such
overlap into an immediate "database is locked" error.
"""

MINIMUM_SQLITE_VERSION: Final = (3, 38, 0)
"""Snapshot paging queries the stored JSON, and those functions became built-ins in 3.38."""


def ensure_supported_sqlite() -> None:
    """Fail loudly at startup when the runtime's SQLite is too old.

    Left unchecked, an old library gets through configuration and migrations and only
    fails later, when a check tries to page a result snapshot -- long after the process
    looked healthy.
    """
    parts = tuple(int(part) for part in sqlite3.sqlite_version.split(".")[:3])
    padded = parts + (0,) * (3 - len(parts))
    if padded < MINIMUM_SQLITE_VERSION:
        expected = ".".join(str(part) for part in MINIMUM_SQLITE_VERSION)
        raise PersistenceError(
            f"SQLite {sqlite3.sqlite_version} is too old; {expected} or newer is required"
        )


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


async def _run_pragma(conn: aiosqlite.Connection, statement: str) -> None:
    """Execute a ``PRAGMA`` and fully consume its cursor.

    ``PRAGMA`` statements return rows. Leaving that cursor un-stepped keeps a read lock
    open on the connection, which blocks every later connection to the same file.
    """
    cursor = await conn.execute(statement)
    await cursor.fetchall()
    await cursor.close()


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

    def __init__(self, path: Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> None:
        self._path = path
        self._busy_timeout_ms = busy_timeout_ms

    async def connect(self) -> aiosqlite.Connection:
        """Open a fresh connection with row access by name, FKs, WAL, and a busy timeout.

        ``isolation_level=None`` turns off the driver's legacy implicit transactions, so
        this module owns every ``BEGIN``. Two things follow: a write issued outside
        :meth:`transaction` autocommits immediately instead of sitting in an implicit
        transaction that ``close()`` would discard, and :meth:`transaction` can always
        issue ``BEGIN IMMEDIATE`` without hitting "cannot start a transaction within a
        transaction".

        Each ``PRAGMA`` cursor is drained and closed. A row-returning statement left
        un-stepped keeps a read lock on the database, which makes the *next* connection
        to the same file fail with "database is locked".
        """
        conn = await aiosqlite.connect(self._path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await _run_pragma(conn, "PRAGMA foreign_keys = ON")
        await _run_pragma(conn, "PRAGMA journal_mode = WAL")
        await _run_pragma(conn, f"PRAGMA busy_timeout = {self._busy_timeout_ms:d}")
        return conn

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """Open a connection for the duration of the block and always close it.

        This is the entry point for a component that owns its own database access rather
        than joining a caller's unit of work: it cannot be handed a connection that is
        already inside somebody else's transaction, so its writes can never be rolled
        back by a failure that has nothing to do with them.
        """
        conn = await self.connect()
        try:
            yield conn
        finally:
            await conn.close()

    @asynccontextmanager
    async def transaction(self, conn: aiosqlite.Connection) -> AsyncIterator[aiosqlite.Connection]:
        """Run a block atomically on ``conn``, nesting inside an enclosing transaction.

        The outermost block runs as ``BEGIN IMMEDIATE`` and commits on success or rolls
        back on any exception, including ``BaseException`` subclasses such as
        cancellation. A block entered while ``conn`` already has a transaction open runs
        as a ``SAVEPOINT`` instead: failing it unwinds only its own statements, and a
        later failure of the enclosing block still discards everything. That makes a
        service free to wrap repository calls that wrap their own writes.

        Interleaving transactions from concurrent tasks on one connection is not
        supported by SQLite; callers take one connection per unit of work.
        """
        if conn.in_transaction:
            name = f"cf_sp_{next(_SAVEPOINT_NAMES)}"
            await conn.execute(f"SAVEPOINT {name}")
            try:
                yield conn
            except BaseException:
                await conn.execute(f"ROLLBACK TO {name}")
                await conn.execute(f"RELEASE {name}")
                raise
            else:
                await conn.execute(f"RELEASE {name}")
            return

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
