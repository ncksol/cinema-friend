"""The loops that make saved watches actually run.

Nothing else in the application is on a timer. A watch is a row that says when it should
next be checked; this module is what notices that the time has come, and what retires a
watch whose dates have passed. Three independent loops run side by side because their
cadences and their failure modes have nothing to do with each other: due checks every
minute, delivery every ten seconds, retention once a day. One of them stalling or
throwing must not hold up the others.

Two decisions are worth stating outright.

*A ``ConflictError`` is not a failure.* It means the watch changed while its check was
in flight -- paused, edited, deleted -- and the check correctly threw its work away. The
run is abandoned, not failed: nothing is written, the schedule is left exactly where the
owner's action put it, and the watch is picked up again whenever that new schedule says
so. Marking it failed would punish the user for editing their own watch, and retrying
immediately would race the very change that caused the conflict.

*Expiry belongs here.* A check has no reason to care that a watch's date range ran out;
it would simply find nothing and reschedule, forever. The scan compares the watch's last
date against today *in London* -- the dates were entered as London dates, and around
midnight the UTC date is already tomorrow -- and retires it instead of checking it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Final, Protocol
from uuid import UUID

from cinema_friend.clock import Clock
from cinema_friend.domain.errors import ConflictError
from cinema_friend.domain.results import CheckResult
from cinema_friend.domain.state import CheckTrigger, WatchStatus
from cinema_friend.domain.time_window import LONDON
from cinema_friend.domain.watch import Watch
from cinema_friend.storage.database import Database
from cinema_friend.storage.retention import RetentionCounts
from cinema_friend.storage.watch_repository import WatchRepository

logger = logging.getLogger(__name__)

Scan = Callable[[], Awaitable[object]]

DUE_INTERVAL_SECONDS: Final = 60.0
DELIVERY_INTERVAL_SECONDS: Final = 10.0
RETENTION_INTERVAL_SECONDS: Final = 24 * 60 * 60.0

DEFAULT_MAX_CONCURRENT_CHECKS: Final = 2
"""Matches the transport's own host concurrency cap; more would only queue behind it."""

_TRIGGERS: Final = {
    WatchStatus.ACTIVE: CheckTrigger.SCHEDULED,
    WatchStatus.BACKOFF: CheckTrigger.RECOVERY,
}


class _Outcome(Enum):
    """What one check did, kept separate from the exception that signalled it."""

    CHECKED = "checked"
    ABANDONED = "abandoned"
    FAILED = "failed"


class CheckRunner(Protocol):
    """The one method the scheduler needs from ``CheckService``."""

    async def check(self, watch_id: UUID, trigger: CheckTrigger) -> CheckResult: ...


class DeliveryOutcome(Protocol):
    @property
    def attempted(self) -> int: ...
    @property
    def sent(self) -> int: ...
    @property
    def retried(self) -> int: ...
    @property
    def failed(self) -> int: ...


class DeliveryRunner(Protocol):
    """The one method the scheduler needs from the Telegram ``DeliveryWorker``.

    Declared structurally rather than imported: ``telegram.bot`` already depends on this
    package, and the scheduler has no business knowing about Telegram beyond "run the
    queue".
    """

    async def run_once(self) -> DeliveryOutcome: ...


class RetentionRunner(Protocol):
    async def run(self, now: datetime) -> RetentionCounts: ...


@dataclass(frozen=True, slots=True)
class SchedulerDependencies:
    """Everything the loops call out to, supplied by the application at startup."""

    database: Database
    watches: WatchRepository
    checks: CheckRunner
    deliveries: DeliveryRunner
    retention: RetentionRunner
    clock: Clock


@dataclass(frozen=True, slots=True)
class DueScan:
    """What one pass over the due watches did, for logging and for tests."""

    checked: int
    expired: int
    abandoned: int
    failed: int


class Scheduler:
    """Runs due checks, the delivery queue, and retention, each on its own cadence."""

    def __init__(
        self,
        dependencies: SchedulerDependencies,
        *,
        max_concurrent_checks: int = DEFAULT_MAX_CONCURRENT_CHECKS,
        due_interval_seconds: float = DUE_INTERVAL_SECONDS,
        delivery_interval_seconds: float = DELIVERY_INTERVAL_SECONDS,
        retention_interval_seconds: float = RETENTION_INTERVAL_SECONDS,
    ) -> None:
        self._deps = dependencies
        self._due_interval = due_interval_seconds
        self._delivery_interval = delivery_interval_seconds
        self._retention_interval = retention_interval_seconds
        self._checks_in_flight = asyncio.Semaphore(max_concurrent_checks)

    async def run(self, stop_event: asyncio.Event) -> None:
        """Run every loop until *stop_event* is set, then return.

        The loops are gathered rather than left as loose tasks so that returning from
        this coroutine means all of them have finished -- including any check still in
        flight. That is what lets the application put a bound on shutdown: it awaits
        this one task and knows exactly what it is waiting for.
        """
        await asyncio.gather(
            self._loop("due", self._due_interval, self.run_due_once, stop_event),
            self._loop("delivery", self._delivery_interval, self.run_delivery_once, stop_event),
            self._loop("retention", self._retention_interval, self.run_retention_once, stop_event),
        )

    async def _loop(
        self,
        name: str,
        interval: float,
        work: Scan,
        stop_event: asyncio.Event,
    ) -> None:
        """Do the work, then wait out the interval or the stop event, whichever first.

        Work comes first so a restart clears its backlog immediately instead of idling
        through a full interval while overdue watches sit there. A failing pass is
        logged and the loop continues: a transient database or network fault must not
        permanently stop scheduling.
        """
        while not stop_event.is_set():
            try:
                await work()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduler loop failed", extra={"loop": name})
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue

    # ------------------------------------------------------------------
    # Due checks
    # ------------------------------------------------------------------

    async def run_due_once(self) -> DueScan:
        """Check every watch whose time has come, retiring the ones that have run out."""
        now = self._deps.clock.now()
        async with self._deps.database.connection() as conn:
            due = await self._deps.watches.list_due(conn, now)

        expired = 0
        runnable: list[Watch] = []
        for watch in due:
            if _has_expired(watch, now):
                if await self._expire(watch, now):
                    expired += 1
            else:
                runnable.append(watch)

        outcomes = await asyncio.gather(
            *(self._run_check(watch) for watch in runnable), return_exceptions=True
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
                # CancelledError and friends are shutdown, not a check result.
                raise outcome

        checked = sum(1 for outcome in outcomes if outcome is _Outcome.CHECKED)
        abandoned = sum(1 for outcome in outcomes if outcome is _Outcome.ABANDONED)
        failed = sum(1 for outcome in outcomes if outcome is _Outcome.FAILED)
        scan = DueScan(checked=checked, expired=expired, abandoned=abandoned, failed=failed)
        if due:
            logger.info(
                "due scan completed",
                extra={
                    "due_count": len(due),
                    "checked": checked,
                    "expired": expired,
                    "abandoned": abandoned,
                    "failed": failed,
                },
            )
        return scan

    async def _run_check(self, watch: Watch) -> _Outcome:
        """Run one check, translating its failure mode into an outcome for the scan."""
        trigger = _TRIGGERS[watch.status]
        async with self._checks_in_flight:
            try:
                await self._deps.checks.check(watch.watch_id, trigger)
            except ConflictError:
                # The watch moved under the check. Whoever moved it owns its schedule
                # now, so there is nothing to record and nothing to retry.
                logger.info(
                    "check abandoned because its watch changed",
                    extra={"watch_id": str(watch.watch_id), "trigger": trigger.value},
                )
                return _Outcome.ABANDONED
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "check failed",
                    extra={"watch_id": str(watch.watch_id), "trigger": trigger.value},
                )
                return _Outcome.FAILED
        return _Outcome.CHECKED

    async def _expire(self, observed: Watch, now: datetime) -> bool:
        """Retire a watch whose last day has passed, unless it changed since listing.

        The re-read inside the write transaction is the same guard a check uses: the
        listing is a snapshot taken on another connection, and an owner who paused or
        edited the watch in the meantime must win.
        """
        async with self._deps.database.connection() as conn, self._deps.database.transaction(conn):
            current = await self._deps.watches.get(conn, observed.watch_id)
            if current != observed:
                logger.info(
                    "expiry skipped because its watch changed",
                    extra={"watch_id": str(observed.watch_id)},
                )
                return False
            await self._deps.watches.update(
                conn,
                replace(current, status=WatchStatus.EXPIRED, next_check_at=None, updated_at=now),
            )
        logger.info(
            "watch expired",
            extra={"watch_id": str(observed.watch_id), "status": WatchStatus.EXPIRED.value},
        )
        return True

    # ------------------------------------------------------------------
    # Delivery and retention
    # ------------------------------------------------------------------

    async def run_delivery_once(self) -> DeliveryOutcome:
        """Drain whatever the delivery queue can send right now."""
        run = await self._deps.deliveries.run_once()
        if run.attempted:
            logger.info(
                "delivery scan completed",
                extra={
                    "attempted": run.attempted,
                    "sent": run.sent,
                    "retried": run.retried,
                    "failed": run.failed,
                },
            )
        return run

    async def run_retention_once(self) -> RetentionCounts:
        """Prune aged rows against the current instant."""
        counts = await self._deps.retention.run(self._deps.clock.now())
        logger.info(
            "retention sweep completed",
            extra={
                "snapshots_deleted": counts.snapshots_deleted,
                "check_runs_deleted": counts.check_runs_deleted,
                "drafts_deleted": counts.drafts_deleted,
            },
        )
        return counts


def _has_expired(watch: Watch, now: datetime) -> bool:
    """True once London has moved past the watch's final date.

    London, not UTC: the user picked a London date from a London cinema's listings, and
    between 23:00 and midnight BST the UTC date is already the next day. Comparing in
    UTC would retire a watch an hour early on its last evening -- exactly when a
    late-release ticket is most likely to appear.
    """
    return now.astimezone(LONDON).date() > watch.criteria.date_to
