"""Persists watches; every method accepts an existing connection.

Callers open one connection per unit of work (via ``Database.connect()``) and pass it to
every repository call they need, so a service can wrap several calls — across this
repository and others — in a single ``Database.transaction()``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from urllib.parse import urlsplit
from uuid import UUID

import aiosqlite

from cinema_friend.domain.state import WatchMode, WatchStatus
from cinema_friend.domain.watch import Watch, WatchCriteria
from cinema_friend.storage.database import decode_datetime, encode_datetime


def _encode_criteria(criteria: WatchCriteria) -> str:
    """Serialize ``WatchCriteria`` deterministically: enums as strings, sets as sorted lists."""
    payload = {
        "source_url": criteria.source_url,
        "slug": criteria.slug,
        "date_from": criteria.date_from.isoformat(),
        "date_to": criteria.date_to.isoformat(),
        "time_from": criteria.time_from.isoformat(),
        "time_to": criteria.time_to.isoformat(),
        "quantity": criteria.quantity,
        "mode": criteria.mode.value,
        "interval_seconds": (
            int(criteria.interval.total_seconds()) if criteria.interval is not None else None
        ),
        "preferred_seats": sorted(criteria.preferred_seats),
        "excluded_seats": sorted(criteria.excluded_seats),
        "preferred_rows": sorted(criteria.preferred_rows),
        "excluded_rows": sorted(criteria.excluded_rows),
        "preferred_utc_instant": (
            encode_datetime(criteria.preferred_utc_instant)
            if criteria.preferred_utc_instant is not None
            else None
        ),
    }
    return json.dumps(payload, sort_keys=True)


def _decode_criteria(data: str) -> WatchCriteria:
    payload = json.loads(data)
    interval_seconds = payload["interval_seconds"]
    preferred_instant = payload["preferred_utc_instant"]
    return WatchCriteria(
        source_url=payload["source_url"],
        slug=payload["slug"],
        date_from=date.fromisoformat(payload["date_from"]),
        date_to=date.fromisoformat(payload["date_to"]),
        time_from=time.fromisoformat(payload["time_from"]),
        time_to=time.fromisoformat(payload["time_to"]),
        quantity=payload["quantity"],
        mode=WatchMode(payload["mode"]),
        interval=timedelta(seconds=interval_seconds) if interval_seconds is not None else None,
        preferred_seats=frozenset(payload["preferred_seats"]),
        excluded_seats=frozenset(payload["excluded_seats"]),
        preferred_rows=frozenset(payload["preferred_rows"]),
        excluded_rows=frozenset(payload["excluded_rows"]),
        preferred_utc_instant=(
            decode_datetime(preferred_instant) if preferred_instant is not None else None
        ),
    )


def _row_to_watch(row: aiosqlite.Row) -> Watch:
    next_run_at = row["next_run_at"]
    last_check_at = row["last_check_at"]
    return Watch(
        watch_id=UUID(row["id"]),
        user_id=row["owner_user_id"],
        criteria=_decode_criteria(row["criteria_json"]),
        status=WatchStatus(row["status"]),
        created_at=decode_datetime(row["created_at"]),
        updated_at=decode_datetime(row["updated_at"]),
        next_check_at=decode_datetime(next_run_at) if next_run_at is not None else None,
        title=row["title"],
        last_check_at=decode_datetime(last_check_at) if last_check_at is not None else None,
    )


class WatchRepository:
    """CRUD and scheduling queries over the ``watches`` table."""

    async def create(self, conn: aiosqlite.Connection, watch: Watch) -> None:
        criteria = watch.criteria
        await conn.execute(
            """
            INSERT INTO watches (
                id, owner_user_id, source_url, slug, title, criteria_json, mode,
                interval_seconds, status, next_run_at, last_check_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(watch.watch_id),
                watch.user_id,
                criteria.source_url,
                criteria.slug,
                watch.title,
                _encode_criteria(criteria),
                criteria.mode.value,
                (
                    int(criteria.interval.total_seconds())
                    if criteria.interval is not None
                    else None
                ),
                watch.status.value,
                encode_datetime(watch.next_check_at) if watch.next_check_at is not None else None,
                encode_datetime(watch.last_check_at) if watch.last_check_at is not None else None,
                encode_datetime(watch.created_at),
                encode_datetime(watch.updated_at),
            ),
        )

    async def get(self, conn: aiosqlite.Connection, watch_id: UUID) -> Watch | None:
        cursor = await conn.execute("SELECT * FROM watches WHERE id = ?", (str(watch_id),))
        row = await cursor.fetchone()
        return _row_to_watch(row) if row is not None else None

    async def list_for_owner(
        self, conn: aiosqlite.Connection, owner_user_id: int
    ) -> tuple[Watch, ...]:
        cursor = await conn.execute(
            "SELECT * FROM watches WHERE owner_user_id = ? ORDER BY created_at, id",
            (owner_user_id,),
        )
        rows = await cursor.fetchall()
        return tuple(_row_to_watch(row) for row in rows)

    async def list_due(self, conn: aiosqlite.Connection, now: datetime) -> tuple[Watch, ...]:
        """Return watches whose ``next_run_at`` has arrived, earliest first.

        Both ``ACTIVE`` and ``BACKOFF`` rows qualify. A watch in backoff is not broken,
        it is waiting out a host problem, and its ``next_run_at`` is precisely the time
        the host circuit said it was worth probing again. Excluding it would leave every
        backed-off watch stranded, since nothing else re-arms them. The scheduler tells
        the two apart by status and triggers a backed-off watch as a ``RECOVERY`` check.
        """
        cursor = await conn.execute(
            """
            SELECT * FROM watches
            WHERE status IN (?, ?) AND next_run_at IS NOT NULL AND next_run_at <= ?
            ORDER BY next_run_at, id
            """,
            (WatchStatus.ACTIVE.value, WatchStatus.BACKOFF.value, encode_datetime(now)),
        )
        rows = await cursor.fetchall()
        return tuple(_row_to_watch(row) for row in rows)

    async def list_active_owner_ids(self, conn: aiosqlite.Connection, host: str) -> frozenset[int]:
        """Return distinct owners of live watches targeting ``host``.

        Live means ``ACTIVE`` or ``BACKOFF``: a watch waiting out host backoff is still
        one its owner expects to hear about, so it must receive the host degradation and
        recovery alerts. Used to send one alert per affected user rather than one per
        watch. ``watches`` has no dedicated host column, so the host is parsed from each
        row's ``source_url`` the same way the transport layer keys its circuit breaker.
        """
        cursor = await conn.execute(
            "SELECT DISTINCT owner_user_id, source_url FROM watches WHERE status IN (?, ?)",
            (WatchStatus.ACTIVE.value, WatchStatus.BACKOFF.value),
        )
        rows = await cursor.fetchall()
        return frozenset(
            row["owner_user_id"] for row in rows if urlsplit(row["source_url"]).hostname == host
        )

    async def update(self, conn: aiosqlite.Connection, watch: Watch) -> None:
        """Persist ``watch``'s mutable fields; ``created_at`` and ``id`` never change.

        Every mutable column, including ``title`` and ``last_check_at``, is written from
        the passed ``Watch``, so the domain object is the single source of truth: callers
        read, replace what changed, and write back rather than passing side-channel
        keywords that could disagree with the object they also persist.
        """
        criteria = watch.criteria
        await conn.execute(
            """
            UPDATE watches
            SET source_url = ?, slug = ?, title = ?, criteria_json = ?, mode = ?,
                interval_seconds = ?, status = ?, next_run_at = ?, last_check_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                criteria.source_url,
                criteria.slug,
                watch.title,
                _encode_criteria(criteria),
                criteria.mode.value,
                (
                    int(criteria.interval.total_seconds())
                    if criteria.interval is not None
                    else None
                ),
                watch.status.value,
                encode_datetime(watch.next_check_at) if watch.next_check_at is not None else None,
                encode_datetime(watch.last_check_at) if watch.last_check_at is not None else None,
                encode_datetime(watch.updated_at),
                str(watch.watch_id),
            ),
        )

    async def delete(self, conn: aiosqlite.Connection, watch_id: UUID) -> None:
        """Delete a watch; ``ON DELETE CASCADE`` removes its dependent rows."""
        await conn.execute("DELETE FROM watches WHERE id = ?", (str(watch_id),))
