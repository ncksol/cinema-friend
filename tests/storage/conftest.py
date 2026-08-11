"""Shared fixtures and factories for storage tests.

Every storage test needs a migrated database and, because most rows are foreign-keyed
to ``watches``, at least one persisted watch. Building those here keeps the individual
test modules focused on the behaviour they assert.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from uuid import UUID

import aiosqlite
import pytest

from cinema_friend.domain.bfi import Performance
from cinema_friend.domain.results import RankedOption, RankVector
from cinema_friend.domain.state import WatchMode, WatchStatus
from cinema_friend.domain.watch import Watch, WatchCriteria
from cinema_friend.storage.database import Database
from cinema_friend.storage.watch_repository import WatchRepository

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def uuid_for(suffix: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{suffix:012d}")


def criteria(**overrides: object) -> WatchCriteria:
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


def performance(performance_id: str = "p1", *, start_utc: datetime | None = None) -> Performance:
    return Performance(
        performance_id=performance_id,
        event_id="e1",
        start_utc=start_utc or datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
        sales_status_code="OPEN*",
        availability_code="A",
        availability_num=50,
        reserved_seating=True,
        seat_map_url="https://whatson.bfi.org.uk/imax/Online/mapSelect.asp?ID=p1",
        options=("opt-a",),
    )


def option(
    seat_label: str = "L17-L18",
    *,
    performance_id: str = "p1",
    raw_view_score: float = 95.5,
    preferred_seat_overlap: int = 1,
    preferred_row_match: int = 1,
    preferred_time_distance_minutes: int = 0,
    price_pence: int | None = 1500,
    start_utc: datetime | None = None,
) -> RankedOption:
    start = start_utc or datetime(2026, 8, 26, 18, 0, tzinfo=UTC)
    return RankedOption(
        performance=performance(performance_id, start_utc=start),
        seat_label=seat_label,
        rank_vector=RankVector(
            preferred_seat_overlap=preferred_seat_overlap,
            preferred_row_match=preferred_row_match,
            view_score_band=int(raw_view_score // 5.0),
            preferred_time_distance_minutes=preferred_time_distance_minutes,
            raw_view_score=raw_view_score,
            performance_start=start,
            seat_label=seat_label,
        ),
        price_pence=price_pence,
    )


def options(count: int, *, performance_id: str = "p1") -> tuple[RankedOption, ...]:
    """Build *count* distinct options in descending quality order."""
    return tuple(
        option(
            f"L{index}",
            performance_id=performance_id,
            raw_view_score=99.0 - index,
        )
        for index in range(count)
    )


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    """A migrated database, for repositories that open their own connections."""
    instance = Database(tmp_path / "cinema.db")
    async with instance.connection() as connection:
        await instance.migrate(connection)
    return instance


@pytest.fixture
async def conn(database: Database) -> AsyncIterator[aiosqlite.Connection]:
    connection = await database.connect()
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture
def make_watch(
    conn: aiosqlite.Connection,
) -> Callable[..., Coroutine[Any, Any, Watch]]:
    """Return a coroutine factory that persists and returns a watch."""
    repository = WatchRepository()

    async def _make(
        suffix: int = 1,
        *,
        owner_user_id: int = 11,
        status: WatchStatus = WatchStatus.ACTIVE,
        **overrides: object,
    ) -> Watch:
        watch = Watch(
            watch_id=uuid_for(suffix),
            user_id=owner_user_id,
            criteria=criteria(**overrides),
            status=status,
            created_at=NOW,
            updated_at=NOW,
            next_check_at=NOW,
        )
        await repository.create(conn, watch)
        return watch

    return _make


async def option_keys_in(conn: aiosqlite.Connection, snapshot_id: UUID) -> Sequence[str]:
    cursor = await conn.execute(
        "SELECT option_key FROM result_options WHERE snapshot_id = ? ORDER BY rank",
        (str(snapshot_id),),
    )
    return [row["option_key"] for row in await cursor.fetchall()]


class FailingConnection:
    """Proxies a real connection, failing the ``nth`` statement matching ``marker``.

    Used to interrupt a multi-statement write after it has already made some of its
    changes, which is the only way to show the whole unit rolls back rather than leaving
    the database in a state no code path was ever meant to produce.
    """

    def __init__(self, conn: aiosqlite.Connection, marker: str, nth: int) -> None:
        self._conn = conn
        self._marker = marker
        self._nth = nth
        self._seen = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    @property
    def in_transaction(self) -> bool:
        return self._conn.in_transaction

    async def execute(self, sql: str, parameters: Any = None) -> Any:
        if self._marker in sql:
            self._seen += 1
            if self._seen == self._nth:
                raise aiosqlite.OperationalError("simulated failure mid-write")
        if parameters is None:
            return await self._conn.execute(sql)
        return await self._conn.execute(sql, parameters)
