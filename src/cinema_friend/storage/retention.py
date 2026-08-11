"""Prunes aged rows the application no longer needs, in one transaction.

Every rule here is a rule about what survives. A watch's newest snapshot is kept
regardless of age, because it is what ``/results`` shows and a dormant watch would
otherwise render an empty page. A snapshot a still-pending delivery points at is kept
until that delivery resolves, because the queued message has to render it when it is
finally sent. Notified option keys are never pruned at all: forgetting one would
re-announce an option the user was already told about.

The three deletions run inside one :meth:`Database.transaction`, so a sweep that fails
part-way leaves the database exactly as it found it rather than half-pruned.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import aiosqlite

from cinema_friend.domain.results import DeliveryStatus
from cinema_friend.storage.database import Database, encode_datetime

SNAPSHOT_TTL: timedelta = timedelta(hours=24)
CHECK_RUN_TTL: timedelta = timedelta(days=30)
DRAFT_TTL: timedelta = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class RetentionCounts:
    """How many rows one sweep removed, for logging and for tests to assert against."""

    snapshots_deleted: int
    check_runs_deleted: int
    drafts_deleted: int


class RetentionService:
    """Deletes aged snapshots, check runs, and conversation drafts."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def run(self, conn: aiosqlite.Connection, now: datetime) -> RetentionCounts:
        """Prune everything past its retention window as of ``now``.

        Snapshots are deleted before check runs so the sweep is one statement per table
        rather than order-sensitive: ``result_snapshots.check_run_id`` is
        ``ON DELETE SET NULL``, so removing a check run detaches its snapshot instead of
        cascading into it, and either order leaves the same rows behind. Doing snapshots
        first also means the check-run delete never touches rows that just went away.
        """
        async with self._database.transaction(conn):
            snapshots = await self._delete_old_snapshots(conn, now)
            check_runs = await self._delete_old_check_runs(conn, now)
            drafts = await self._delete_expired_drafts(conn, now)
        return RetentionCounts(
            snapshots_deleted=snapshots,
            check_runs_deleted=check_runs,
            drafts_deleted=drafts,
        )

    async def _delete_old_snapshots(self, conn: aiosqlite.Connection, now: datetime) -> int:
        cursor = await conn.execute(
            """
            DELETE FROM result_snapshots
            WHERE checked_at < ?
              AND is_latest = 0
              AND id NOT IN (
                SELECT snapshot_id FROM notification_deliveries
                WHERE snapshot_id IS NOT NULL AND status = ?
              )
            """,
            (encode_datetime(now - SNAPSHOT_TTL), DeliveryStatus.PENDING.value),
        )
        return cursor.rowcount

    async def _delete_old_check_runs(self, conn: aiosqlite.Connection, now: datetime) -> int:
        cursor = await conn.execute(
            "DELETE FROM check_runs WHERE started_at < ?",
            (encode_datetime(now - CHECK_RUN_TTL),),
        )
        return cursor.rowcount

    async def _delete_expired_drafts(self, conn: aiosqlite.Connection, now: datetime) -> int:
        cursor = await conn.execute(
            "DELETE FROM conversation_drafts WHERE updated_at < ?",
            (encode_datetime(now - DRAFT_TTL),),
        )
        return cursor.rowcount
