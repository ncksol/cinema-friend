"""Prunes aged rows the application no longer needs, in one transaction it owns.

Every rule here is a rule about what survives. A watch's newest snapshot is kept
regardless of age, because it is what ``/results`` shows and a dormant watch would
otherwise render an empty page. A snapshot a still-pending delivery points at is kept
until that delivery resolves, because the queued message has to render it when it is
finally sent. Notified option keys are never pruned at all: forgetting one would
re-announce an option the user was already told about.

:meth:`RetentionService.run` takes only the instant to prune against. It opens its own
connection and its own transaction, so the sweep is atomic on its own terms and cannot
be undone by a caller whose unrelated work happens to fail afterwards. A scheduler
firing this on a timer has no unit of work to enlist it in and should not have to invent
one, and a housekeeping sweep that a failing check could roll back would silently stop
pruning under exactly the conditions that make pruning matter.
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

    async def run(self, now: datetime) -> RetentionCounts:
        """Prune everything past its retention window as of ``now``.

        The cutoffs are encoded before a connection is opened, so a naive ``now`` is
        rejected without touching the database at all.

        Snapshots are deleted before check runs so the sweep is one statement per table
        rather than order-sensitive: ``result_snapshots.check_run_id`` is
        ``ON DELETE SET NULL``, so removing a check run detaches its snapshot instead of
        cascading into it, and either order leaves the same rows behind. Doing snapshots
        first also means the check-run delete never touches rows that just went away.
        """
        snapshot_cutoff = encode_datetime(now - SNAPSHOT_TTL)
        check_run_cutoff = encode_datetime(now - CHECK_RUN_TTL)
        draft_cutoff = encode_datetime(now - DRAFT_TTL)
        async with self._database.connection() as conn, self._database.transaction(conn):
            snapshots = await self._delete_old_snapshots(conn, snapshot_cutoff)
            check_runs = await self._delete_old_check_runs(conn, check_run_cutoff)
            drafts = await self._delete_expired_drafts(conn, draft_cutoff)
        return RetentionCounts(
            snapshots_deleted=snapshots,
            check_runs_deleted=check_runs,
            drafts_deleted=drafts,
        )

    async def _delete_old_snapshots(self, conn: aiosqlite.Connection, cutoff: str) -> int:
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
            (cutoff, DeliveryStatus.PENDING.value),
        )
        return cursor.rowcount

    async def _delete_old_check_runs(self, conn: aiosqlite.Connection, cutoff: str) -> int:
        cursor = await conn.execute("DELETE FROM check_runs WHERE started_at < ?", (cutoff,))
        return cursor.rowcount

    async def _delete_expired_drafts(self, conn: aiosqlite.Connection, cutoff: str) -> int:
        cursor = await conn.execute(
            "DELETE FROM conversation_drafts WHERE updated_at < ?", (cutoff,)
        )
        return cursor.rowcount
