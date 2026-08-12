"""Tests for cinema_friend.services.scheduler.

The scheduler is the only thing that makes a saved watch actually do anything, so these
tests drive the real database and the real watch repository and fake only what the
scheduler *calls out to*: the check service, the delivery worker, and retention. What
gets asserted is therefore what a restarted process would really find in SQLite --
which watches were picked up, with which trigger, and what state they were left in.

Three properties matter most here:

- Selection: only ``ACTIVE`` and ``BACKOFF`` watches whose time has come are run, an
  overdue watch left behind by a dead process is picked up on the first scan, and a
  watch whose London date range has run out is retired instead of checked forever.
- Isolation: a check that raises never takes the scan, the loop, or its sibling watches
  down with it, and a ``ConflictError`` -- a watch legitimately changed underneath a
  running check -- is an abandoned run, not a failure and not an immediate retry.
- Termination: a stop event ends every loop promptly, and a scan already in flight is
  awaited rather than dropped, which is what lets shutdown bound the wait for it.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import Callable, Coroutine
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import aiosqlite
import pytest

from cinema_friend.bfi.urls import film_page_url
from cinema_friend.domain.errors import ConflictError, PersistenceError
from cinema_friend.domain.results import CheckResult
from cinema_friend.domain.state import CheckOutcome, CheckTrigger, WatchMode, WatchStatus
from cinema_friend.domain.watch import Watch, WatchCriteria
from cinema_friend.logging_config import configure_logging, current_correlation_id
from cinema_friend.services.scheduler import (
    DELIVERY_INTERVAL_SECONDS,
    DUE_INTERVAL_SECONDS,
    RETENTION_INTERVAL_SECONDS,
    Scheduler,
    SchedulerDependencies,
)
from cinema_friend.storage.database import Database
from cinema_friend.storage.retention import RetentionCounts
from cinema_friend.storage.watch_repository import WatchRepository
from tests.fakes import FakeClock

# 13:00 UTC is 14:00 in London, comfortably inside a window that ends on 2026-08-30.
NOW = datetime(2026, 8, 27, 13, 0, tzinfo=UTC)
SLUG = "dog-stars"
SOURCE_URL = film_page_url(SLUG)
INTERVAL = timedelta(minutes=30)
LOGGER_NAME = "cinema_friend.services.scheduler"

pytestmark = pytest.mark.usefixtures("no_leaked_tasks")


async def until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    """Await *predicate* becoming true, failing the test rather than hanging forever."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition was never met")
        await asyncio.sleep(0.002)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeChecks:
    """Records every check the scheduler asks for, and can stall or fail them.

    ``gate`` holds every started check open, which is how the concurrency and shutdown
    tests observe how many the scheduler let run at once.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[UUID, CheckTrigger]] = []
        self.errors: dict[UUID, BaseException] = {}
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.active = 0
        self.max_active = 0

    async def check(self, watch_id: UUID, trigger: CheckTrigger) -> CheckResult:
        self.calls.append((watch_id, trigger))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            error = self.errors.get(watch_id)
            if error is not None:
                raise error
            return CheckResult(
                check_run_id=uuid4(),
                watch_id=watch_id,
                trigger=trigger,
                outcome=CheckOutcome.SUCCESS,
                snapshot_id=uuid4(),
                performance_count=1,
                option_count=1,
                error_detail=None,
            )
        finally:
            self.active -= 1


class FakeDeliveryRun:
    """Structural stand-in for ``DeliveryRun``: the counters the scheduler logs."""

    def __init__(self, attempted: int, sent: int) -> None:
        self._attempted = attempted
        self._sent = sent

    @property
    def attempted(self) -> int:
        return self._attempted

    @property
    def sent(self) -> int:
        return self._sent

    @property
    def retried(self) -> int:
        return 0

    @property
    def failed(self) -> int:
        return 0


class FakeDeliveries:
    def __init__(self) -> None:
        self.runs = 0
        self.error: BaseException | None = None

    async def run_once(self) -> FakeDeliveryRun:
        self.runs += 1
        if self.error is not None:
            raise self.error
        return FakeDeliveryRun(attempted=1, sent=1)


class FakeRetention:
    def __init__(self) -> None:
        self.calls: list[datetime] = []

    async def run(self, now: datetime) -> RetentionCounts:
        self.calls.append(now)
        return RetentionCounts(snapshots_deleted=1, check_runs_deleted=2, drafts_deleted=3)


class CorrelatedChecks:
    def __init__(self, *, logger_name: str = LOGGER_NAME) -> None:
        self.logger = logging.getLogger(logger_name)
        self.calls: list[tuple[UUID, CheckTrigger]] = []
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.active = 0
        self.max_active = 0
        self.error: BaseException | None = None

    async def check(self, watch_id: UUID, trigger: CheckTrigger) -> CheckResult:
        self.calls.append((watch_id, trigger))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            self.logger.info(
                "check started",
                extra={"watch_id": str(watch_id), "trigger": trigger.value},
            )
            assert current_correlation_id() == str(watch_id)
            await self.release.wait()
            if self.error is not None:
                raise self.error
            self.logger.info(
                "check finished",
                extra={"watch_id": str(watch_id), "trigger": trigger.value},
            )
            return CheckResult(
                check_run_id=uuid4(),
                watch_id=watch_id,
                trigger=trigger,
                outcome=CheckOutcome.SUCCESS,
                snapshot_id=uuid4(),
                performance_count=1,
                option_count=1,
                error_detail=None,
            )
        finally:
            self.active -= 1


class StaleListingRepository(WatchRepository):
    """Reports watches exactly as some earlier scan saw them, however stale that is.

    Stands in for the real race: the scan lists due watches on one connection and only
    later opens the transaction that acts on one, and the owner is free to change the
    row in between.
    """

    def __init__(self, stale: tuple[Watch, ...]) -> None:
        self._stale = stale

    async def list_due(self, conn: aiosqlite.Connection, now: datetime) -> tuple[Watch, ...]:
        return self._stale


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    instance = Database(tmp_path / "cinema-friend.db")
    async with instance.connection() as conn:
        await instance.migrate(conn)
    return instance


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def checks() -> FakeChecks:
    return FakeChecks()


@pytest.fixture
def deliveries() -> FakeDeliveries:
    return FakeDeliveries()


@pytest.fixture
def retention() -> FakeRetention:
    return FakeRetention()


@pytest.fixture
def make_scheduler(
    database: Database,
    clock: FakeClock,
    checks: FakeChecks,
    deliveries: FakeDeliveries,
    retention: FakeRetention,
) -> Callable[..., Scheduler]:
    def _make(
        *,
        watches: WatchRepository | None = None,
        checks_runner: Any | None = None,
        **overrides: Any,
    ) -> Scheduler:
        return Scheduler(
            SchedulerDependencies(
                database=database,
                watches=watches if watches is not None else WatchRepository(),
                checks=checks_runner if checks_runner is not None else checks,
                deliveries=deliveries,
                retention=retention,
                clock=clock,
            ),
            **overrides,
        )

    return _make


@pytest.fixture
def scheduler(make_scheduler: Callable[..., Scheduler]) -> Scheduler:
    return make_scheduler()


def criteria(**overrides: Any) -> WatchCriteria:
    defaults: dict[str, Any] = {
        "source_url": SOURCE_URL,
        "slug": SLUG,
        "date_from": date(2026, 8, 26),
        "date_to": date(2026, 8, 30),
        "time_from": time(18, 0),
        "time_to": time(23, 0),
        "quantity": 2,
        "mode": WatchMode.RECURRING,
        "interval": INTERVAL,
    }
    defaults.update(overrides)
    return WatchCriteria(**defaults)


@pytest.fixture
def make_watch(database: Database) -> Callable[..., Coroutine[Any, Any, Watch]]:
    repository = WatchRepository()

    async def _make(
        *,
        status: WatchStatus = WatchStatus.ACTIVE,
        next_check_at: datetime | None = NOW,
        user_id: int = 11,
        **criteria_overrides: Any,
    ) -> Watch:
        watch = Watch(
            watch_id=uuid4(),
            user_id=user_id,
            criteria=criteria(**criteria_overrides),
            status=status,
            created_at=NOW - timedelta(days=1),
            updated_at=NOW - timedelta(days=1),
            next_check_at=next_check_at,
        )
        async with database.connection() as conn, database.transaction(conn):
            await repository.create(conn, watch)
        return watch

    return _make


async def reload(database: Database, watch_id: UUID) -> Watch:
    async with database.connection() as conn:
        stored = await WatchRepository().get(conn, watch_id)
    assert stored is not None
    return stored


def parse_records(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


# ---------------------------------------------------------------------------
# Due selection
# ---------------------------------------------------------------------------


async def test_due_scan_runs_an_active_watch_as_scheduled(
    scheduler: Scheduler, checks: FakeChecks, make_watch: Callable[..., Any]
) -> None:
    watch = await make_watch()

    scan = await scheduler.run_due_once()

    assert checks.calls == [(watch.watch_id, CheckTrigger.SCHEDULED)]
    assert scan.checked == 1


async def test_due_scan_runs_a_backoff_watch_as_recovery(
    scheduler: Scheduler, checks: FakeChecks, make_watch: Callable[..., Any]
) -> None:
    watch = await make_watch(status=WatchStatus.BACKOFF)

    await scheduler.run_due_once()

    assert checks.calls == [(watch.watch_id, CheckTrigger.RECOVERY)]


async def test_due_scan_picks_up_a_watch_left_overdue_by_a_dead_process(
    scheduler: Scheduler, checks: FakeChecks, make_watch: Callable[..., Any]
) -> None:
    """A restart must recover the backlog, not wait a full interval for it."""
    watch = await make_watch(next_check_at=NOW - timedelta(hours=6))

    await scheduler.run_due_once()

    assert checks.calls == [(watch.watch_id, CheckTrigger.SCHEDULED)]


async def test_due_scan_ignores_watches_that_are_not_due_yet(
    scheduler: Scheduler, checks: FakeChecks, make_watch: Callable[..., Any]
) -> None:
    await make_watch(next_check_at=NOW + timedelta(minutes=1))

    await scheduler.run_due_once()

    assert checks.calls == []


@pytest.mark.parametrize(
    "status",
    [WatchStatus.PAUSED, WatchStatus.COMPLETED, WatchStatus.EXPIRED, WatchStatus.FAILED],
)
async def test_due_scan_ignores_paused_and_terminal_watches(
    scheduler: Scheduler,
    checks: FakeChecks,
    make_watch: Callable[..., Any],
    status: WatchStatus,
) -> None:
    await make_watch(status=status)

    await scheduler.run_due_once()

    assert checks.calls == []


async def test_due_scan_bounds_how_many_checks_run_at_once(
    make_scheduler: Callable[..., Scheduler], checks: FakeChecks, make_watch: Callable[..., Any]
) -> None:
    for _ in range(5):
        await make_watch()
    checks.gate = asyncio.Event()
    scheduler = make_scheduler(max_concurrent_checks=2)

    task = asyncio.create_task(scheduler.run_due_once())
    await until(lambda: checks.active == 2)
    for _ in range(20):
        await asyncio.sleep(0)
    assert checks.active == 2

    checks.gate.set()
    await asyncio.wait_for(task, timeout=2)
    assert checks.max_active == 2
    assert len(checks.calls) == 5


async def test_due_scan_tags_concurrent_check_logs_and_resets_outside_scope(
    make_watch: Callable[..., Any],
    database: Database,
    clock: FakeClock,
    deliveries: FakeDeliveries,
    retention: FakeRetention,
) -> None:
    first = await make_watch()
    second = await make_watch()
    stream = io.StringIO()
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    try:
        configure_logging("INFO", stream=stream)
        checks = CorrelatedChecks()
        scheduler = Scheduler(
            SchedulerDependencies(
                database=database,
                watches=WatchRepository(),
                checks=checks,
                deliveries=deliveries,
                retention=retention,
                clock=clock,
            ),
            max_concurrent_checks=2,
        )

        task = asyncio.create_task(scheduler.run_due_once())
        await until(lambda: len(checks.calls) == 2)
        checks.release.set()
        await asyncio.wait_for(task, timeout=2)

        logging.getLogger(LOGGER_NAME).info("outside scope")
        payloads = parse_records(stream)
        scoped = [
            payload
            for payload in payloads
            if payload["event"] in {"check started", "check finished"}
        ]
        assert {payload["watch_id"] for payload in scoped} == {
            str(first.watch_id),
            str(second.watch_id),
        }
        assert {
            payload["correlation_id"]
            for payload in scoped
        } == {
            str(first.watch_id),
            str(second.watch_id),
        }
        assert all(payload["correlation_id"] == payload["watch_id"] for payload in scoped)
        assert not any(
            payload["event"] == "outside scope" and "correlation_id" in payload
            for payload in payloads
        )
        assert not any(
            payload["event"] == "due scan completed" and "correlation_id" in payload
            for payload in payloads
        )
    finally:
        for handler in list(root.handlers):
            if handler not in original_handlers:
                root.removeHandler(handler)
                handler.close()
        for handler in original_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(original_level)


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


async def test_due_scan_expires_a_recurring_watch_after_its_london_date_to_day(
    make_scheduler: Callable[..., Scheduler],
    checks: FakeChecks,
    make_watch: Callable[..., Any],
    clock: FakeClock,
    database: Database,
) -> None:
    """23:30 UTC on the final day is already the next day in London, so it is over.

    Chosen deliberately: the UTC calendar date is still ``date_to``, so a scheduler
    comparing UTC dates would keep this watch running for another hour.
    """
    watch = await make_watch()
    clock.current = datetime(2026, 8, 30, 23, 30, tzinfo=UTC)

    scan = await make_scheduler().run_due_once()

    assert checks.calls == []
    assert scan.expired == 1
    stored = await reload(database, watch.watch_id)
    assert stored.status is WatchStatus.EXPIRED
    assert stored.next_check_at is None
    assert stored.updated_at == clock.current


async def test_due_scan_still_runs_a_watch_on_its_final_london_day(
    make_scheduler: Callable[..., Scheduler],
    checks: FakeChecks,
    make_watch: Callable[..., Any],
    clock: FakeClock,
    database: Database,
) -> None:
    watch = await make_watch()
    clock.current = datetime(2026, 8, 30, 22, 30, tzinfo=UTC)

    await make_scheduler().run_due_once()

    assert checks.calls == [(watch.watch_id, CheckTrigger.SCHEDULED)]
    assert (await reload(database, watch.watch_id)).status is WatchStatus.ACTIVE


async def test_due_scan_expires_a_one_off_watch_whose_window_has_passed(
    make_scheduler: Callable[..., Scheduler],
    checks: FakeChecks,
    make_watch: Callable[..., Any],
    clock: FakeClock,
    database: Database,
) -> None:
    """Mode does not change the arithmetic: a window in the past can never match."""
    watch = await make_watch(mode=WatchMode.ONE_OFF, interval=None)
    clock.current = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)

    await make_scheduler().run_due_once()

    assert checks.calls == []
    assert (await reload(database, watch.watch_id)).status is WatchStatus.EXPIRED


async def test_expiry_leaves_a_watch_alone_when_it_changed_under_the_scan(
    make_scheduler: Callable[..., Scheduler],
    make_watch: Callable[..., Any],
    clock: FakeClock,
    database: Database,
) -> None:
    """The listing is a snapshot; a row that moved since belongs to whoever moved it."""
    watch = await make_watch()
    stale = replace(watch, next_check_at=NOW - timedelta(hours=9))
    clock.current = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    scheduler = make_scheduler(watches=StaleListingRepository((stale,)))

    scan = await scheduler.run_due_once()

    assert scan.expired == 0
    stored = await reload(database, watch.watch_id)
    assert stored.status is WatchStatus.ACTIVE
    assert stored.next_check_at == watch.next_check_at


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


async def test_conflict_is_an_abandoned_run_not_a_failure(
    scheduler: Scheduler,
    checks: FakeChecks,
    make_watch: Callable[..., Any],
    database: Database,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A watch changed mid-check is somebody else's decision; leave it exactly alone."""
    watch = await make_watch()
    checks.errors[watch.watch_id] = ConflictError("watch changed while its check was running")

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        scan = await scheduler.run_due_once()

    assert scan.abandoned == 1
    assert scan.failed == 0
    assert len(checks.calls) == 1
    stored = await reload(database, watch.watch_id)
    assert stored.status is WatchStatus.ACTIVE
    assert stored.next_check_at == watch.next_check_at
    assert any("abandoned" in record.getMessage().lower() for record in caplog.records)
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.parametrize(
    "error, expected",
    [
        (ConflictError("watch changed while its check was running"), "ABANDONED"),
        (PersistenceError("disk went away"), "FAILED"),
        (asyncio.CancelledError(), "CANCELLED"),
    ],
)
async def test_run_check_opens_and_clears_the_correlation_scope_for_all_outcomes(
    database: Database,
    clock: FakeClock,
    deliveries: FakeDeliveries,
    retention: FakeRetention,
    make_watch: Callable[..., Any],
    error: BaseException,
    expected: str,
) -> None:
    watch = await make_watch()
    checks = CorrelatedChecks()
    checks.error = error
    checks.release.set()
    scheduler = Scheduler(
        SchedulerDependencies(
            database=database,
            watches=WatchRepository(),
            checks=checks,
            deliveries=deliveries,
            retention=retention,
            clock=clock,
        )
    )

    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await scheduler._run_check(watch)
    else:
        outcome = await scheduler._run_check(watch)
        assert outcome.name == expected

    assert current_correlation_id() is None


async def test_one_failing_check_does_not_stop_the_others(
    scheduler: Scheduler,
    checks: FakeChecks,
    make_watch: Callable[..., Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken = await make_watch()
    healthy = await make_watch()
    checks.errors[broken.watch_id] = PersistenceError("disk went away")

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        scan = await scheduler.run_due_once()

    assert scan.failed == 1
    assert scan.checked == 1
    assert {call[0] for call in checks.calls} == {broken.watch_id, healthy.watch_id}
    assert caplog.records


# ---------------------------------------------------------------------------
# Final fix wave: an unexpected defect must not strand the watch
# ---------------------------------------------------------------------------


class MutatingChecks:
    """Applies a caller-supplied change to the stored watch, then raises.

    This is the race the deferral has to lose: an owner pauses or deletes the watch
    while its check is in flight, and the check then fails for an unrelated reason. The
    mutation is applied on the database the scheduler is about to write to, so what the
    deferral sees is exactly what a concurrent user action would have left there.
    """

    def __init__(
        self,
        database: Database,
        mutate: Callable[[Database, UUID], Coroutine[Any, Any, None]],
        error: BaseException,
    ) -> None:
        self._database = database
        self._mutate = mutate
        self._error = error
        self.calls: list[tuple[UUID, CheckTrigger]] = []

    async def check(self, watch_id: UUID, trigger: CheckTrigger) -> CheckResult:
        self.calls.append((watch_id, trigger))
        await self._mutate(self._database, watch_id)
        raise self._error


async def pause(database: Database, watch_id: UUID) -> None:
    async with database.connection() as conn, database.transaction(conn):
        repository = WatchRepository()
        stored = await repository.get(conn, watch_id)
        assert stored is not None
        await repository.update(
            conn,
            replace(
                stored,
                status=WatchStatus.PAUSED,
                next_check_at=None,
                updated_at=NOW + timedelta(seconds=1),
            ),
        )


async def remove(database: Database, watch_id: UUID) -> None:
    async with database.connection() as conn, database.transaction(conn):
        await WatchRepository().delete(conn, watch_id)


async def test_an_unexpected_defect_backs_a_recurring_watch_off_by_fifteen_minutes(
    scheduler: Scheduler,
    checks: FakeChecks,
    make_watch: Callable[..., Any],
    database: Database,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A watch whose check blew up must still have a next run, or it never runs again.

    ``CheckService`` deliberately lets an unknown defect propagate rather than
    laundering it into a tidy outcome row -- but nothing had then moved the watch, so
    its ``next_check_at`` stayed in the past and every later scan picked it up, failed
    the same way, and hammered the same fault once a minute forever.
    """
    watch = await make_watch()
    checks.errors[watch.watch_id] = RuntimeError("something nobody anticipated")

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        scan = await scheduler.run_due_once()

    assert scan.failed == 1
    stored = await reload(database, watch.watch_id)
    assert stored.status is WatchStatus.BACKOFF
    assert stored.next_check_at == NOW + timedelta(minutes=15)
    assert stored.updated_at == NOW
    assert caplog.records


async def test_an_unexpected_defect_fails_a_one_off_watch_with_no_next_run(
    scheduler: Scheduler,
    checks: FakeChecks,
    make_watch: Callable[..., Any],
    database: Database,
) -> None:
    """A one-off has no cadence to fall back on, so it stops and waits for its owner."""
    watch = await make_watch(mode=WatchMode.ONE_OFF, interval=None)
    checks.errors[watch.watch_id] = RuntimeError("something nobody anticipated")

    scan = await scheduler.run_due_once()

    assert scan.failed == 1
    stored = await reload(database, watch.watch_id)
    assert stored.status is WatchStatus.FAILED
    assert stored.next_check_at is None


async def test_a_deferred_watch_is_not_picked_up_again_by_the_next_scan(
    scheduler: Scheduler,
    checks: FakeChecks,
    make_watch: Callable[..., Any],
) -> None:
    watch = await make_watch()
    checks.errors[watch.watch_id] = RuntimeError("something nobody anticipated")

    await scheduler.run_due_once()
    await scheduler.run_due_once()

    assert checks.calls == [(watch.watch_id, CheckTrigger.SCHEDULED)]


async def test_a_conflict_still_leaves_the_watch_exactly_where_it_was(
    scheduler: Scheduler,
    checks: FakeChecks,
    make_watch: Callable[..., Any],
    database: Database,
) -> None:
    """The deferral is for defects only; an abandoned run writes nothing at all."""
    watch = await make_watch()
    checks.errors[watch.watch_id] = ConflictError("watch changed while its check was running")

    scan = await scheduler.run_due_once()

    assert scan.abandoned == 1
    assert scan.failed == 0
    stored = await reload(database, watch.watch_id)
    assert stored == watch


async def test_a_watch_paused_during_a_failing_check_keeps_the_owners_decision(
    make_scheduler: Callable[..., Scheduler],
    make_watch: Callable[..., Any],
    database: Database,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The owner paused it; a failed check must not drag it back into a schedule."""
    watch = await make_watch()
    checks = MutatingChecks(database, pause, RuntimeError("something nobody anticipated"))
    scheduler = make_scheduler(checks_runner=checks)

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        scan = await scheduler.run_due_once()

    assert scan.failed == 1
    stored = await reload(database, watch.watch_id)
    assert stored.status is WatchStatus.PAUSED
    assert stored.next_check_at is None


async def test_a_watch_deleted_during_a_failing_check_is_not_resurrected(
    make_scheduler: Callable[..., Scheduler],
    make_watch: Callable[..., Any],
    database: Database,
) -> None:
    watch = await make_watch()
    checks = MutatingChecks(database, remove, RuntimeError("something nobody anticipated"))
    scheduler = make_scheduler(checks_runner=checks)

    scan = await scheduler.run_due_once()

    assert scan.failed == 1
    async with database.connection() as conn:
        assert await WatchRepository().get(conn, watch.watch_id) is None


async def test_a_cancelled_check_is_not_swallowed(
    scheduler: Scheduler, checks: FakeChecks, make_watch: Callable[..., Any]
) -> None:
    """Cancellation is shutdown, not a check failure, so it must keep unwinding."""
    watch = await make_watch()
    checks.errors[watch.watch_id] = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await scheduler.run_due_once()


async def test_a_cancelled_check_leaves_the_watch_exactly_where_it_was(
    scheduler: Scheduler,
    checks: FakeChecks,
    make_watch: Callable[..., Any],
    database: Database,
) -> None:
    """Being shut down mid-check must cost a repeated check, not a lost one.

    ``app.SHUTDOWN_GRACE_SECONDS`` is deliberately too short to cover a slow check, and
    that is only safe because of this: a cancelled check writes nothing and leaves its
    watch due in the past, so the next process to start picks it up and runs it again.
    If cancellation ever went down the defect path instead, an ordinary restart would
    silently move a watch to BACKOFF -- or a one-off to FAILED.
    """
    watch = await make_watch()
    checks.errors[watch.watch_id] = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await scheduler.run_due_once()

    stored = await reload(database, watch.watch_id)
    assert stored == watch


# ---------------------------------------------------------------------------
# Delivery and retention
# ---------------------------------------------------------------------------


async def test_delivery_scan_runs_the_worker(
    scheduler: Scheduler, deliveries: FakeDeliveries
) -> None:
    await scheduler.run_delivery_once()

    assert deliveries.runs == 1


async def test_retention_scan_prunes_against_the_current_instant(
    scheduler: Scheduler, retention: FakeRetention, clock: FakeClock
) -> None:
    counts = await scheduler.run_retention_once()

    assert retention.calls == [clock.current]
    assert counts.drafts_deleted == 3


# ---------------------------------------------------------------------------
# Loops
# ---------------------------------------------------------------------------


def test_default_intervals_match_the_specified_cadences() -> None:
    assert DUE_INTERVAL_SECONDS == 60.0
    assert DELIVERY_INTERVAL_SECONDS == 10.0
    assert RETENTION_INTERVAL_SECONDS == 24 * 60 * 60.0


async def test_run_scans_each_kind_of_work_repeatedly(
    make_scheduler: Callable[..., Scheduler],
    checks: FakeChecks,
    deliveries: FakeDeliveries,
    retention: FakeRetention,
    make_watch: Callable[..., Any],
) -> None:
    await make_watch()
    scheduler = make_scheduler(
        due_interval_seconds=0.01,
        delivery_interval_seconds=0.01,
        retention_interval_seconds=0.01,
    )
    stop = asyncio.Event()

    task = asyncio.create_task(scheduler.run(stop))
    try:
        await until(lambda: deliveries.runs >= 2 and len(retention.calls) >= 2 and bool(checks.calls))
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)


async def test_run_returns_promptly_when_stopped(
    make_scheduler: Callable[..., Scheduler],
) -> None:
    """Every loop waits on the stop event, so nobody sits out a full interval."""
    scheduler = make_scheduler(
        due_interval_seconds=3600.0,
        delivery_interval_seconds=3600.0,
        retention_interval_seconds=3600.0,
    )
    stop = asyncio.Event()

    task = asyncio.create_task(scheduler.run(stop))
    await asyncio.sleep(0.01)
    stop.set()

    await asyncio.wait_for(task, timeout=2)


async def test_run_awaits_a_check_already_in_flight_before_returning(
    make_scheduler: Callable[..., Scheduler],
    checks: FakeChecks,
    make_watch: Callable[..., Any],
) -> None:
    """Shutdown can only bound the wait for active checks if the loop actually waits."""
    await make_watch()
    checks.gate = asyncio.Event()
    scheduler = make_scheduler(due_interval_seconds=3600.0)
    stop = asyncio.Event()

    task = asyncio.create_task(scheduler.run(stop))
    await asyncio.wait_for(checks.started.wait(), timeout=2)
    stop.set()
    await asyncio.sleep(0.02)
    assert not task.done()

    checks.gate.set()
    await asyncio.wait_for(task, timeout=2)


async def test_a_loop_survives_a_failing_scan(
    make_scheduler: Callable[..., Scheduler],
    deliveries: FakeDeliveries,
    caplog: pytest.LogCaptureFixture,
) -> None:
    deliveries.error = RuntimeError("telegram exploded")
    scheduler = make_scheduler(
        due_interval_seconds=3600.0,
        delivery_interval_seconds=0.01,
        retention_interval_seconds=3600.0,
    )
    stop = asyncio.Event()

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        task = asyncio.create_task(scheduler.run(stop))
        try:
            await until(lambda: deliveries.runs >= 2)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=2)

    assert caplog.records


async def test_run_cancels_cleanly(
    make_scheduler: Callable[..., Scheduler], checks: FakeChecks, make_watch: Callable[..., Any]
) -> None:
    await make_watch()
    checks.gate = asyncio.Event()
    scheduler = make_scheduler()
    stop = asyncio.Event()

    task = asyncio.create_task(scheduler.run(stop))
    await asyncio.wait_for(checks.started.wait(), timeout=2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
