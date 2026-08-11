"""Tests for cinema_friend.services.check_service.

One check is one unit of work. Every test here drives the real repositories against a
migrated temporary database, so an assertion about "what was persisted" is an assertion
about rows, not about calls on a mock. Only the BFI side is faked: the gateway (or, for
the pagination case, the transport under a real gateway) and the clock/jitter.

The three things these tests exist to pin down are:

- Ordering: eligibility filtering happens before any seat-map fetch, so an ineligible
  performance never costs BFI a request.
- Atomicity: the check run, the snapshot and its options, the watch's new state and
  schedule, and the pending delivery either all land or none of them do -- while the
  host circuit, which is the signal telling everyone to stop hitting BFI, survives a
  rolled-back check because it is written on its own connection.
- Typed failure handling: each BFI error kind moves the watch somewhere different, and
  an unexpected defect is never laundered into an expected failure outcome.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import aiosqlite
import pytest

from cinema_friend.bfi.gateway import BfiGateway
from cinema_friend.bfi.urls import film_page_url, pagination_url, seat_map_url
from cinema_friend.domain.bfi import (
    Performance,
    PerformanceListing,
    PriceZone,
    Seat,
    SeatMap,
    SeatStatus,
)
from cinema_friend.domain.errors import (
    BfiChallengeError,
    BfiContractError,
    BfiNetworkError,
    CircuitOpenError,
    ConflictError,
    InputError,
    PersistenceError,
)
from cinema_friend.domain.results import (
    CircuitState,
    HostCircuit,
    NotificationDelivery,
    NotificationPayload,
    ResultSnapshot,
)
from cinema_friend.domain.state import CheckOutcome, CheckTrigger, WatchMode, WatchStatus
from cinema_friend.domain.watch import Watch, WatchCriteria
from cinema_friend.services.check_service import CheckService
from cinema_friend.services.watch_service import WatchService
from cinema_friend.storage.circuit_repository import SqliteCircuitStore
from cinema_friend.storage.database import Database
from cinema_friend.storage.notification_repository import NotificationRepository
from cinema_friend.storage.result_repository import ResultRepository
from cinema_friend.storage.watch_repository import WatchRepository
from tests.factories.bfi_html import make_article_html, performance_row, seat_map_html
from tests.fakes import FakeClock, FakeTransport, fetched_document

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
INTERVAL = timedelta(minutes=30)
HOST = "whatson.bfi.org.uk"
SLUG = "dog-stars"
SOURCE_URL = film_page_url(SLUG)
ARTICLE_ID = "2152D1E8-CFF7-419F-BE57-F51C1E490F24"
TOKEN = "1,a/b+="

PERF_1 = "2475959F-2B73-4EA6-AD26-AFA8AEB785FD"
PERF_2 = "3586A6AF-3C84-4FA7-BE37-B0B9BFC8960E"

# The performance window every default criteria accepts: 2026-08-08 14:00 Europe/London.
START_UTC = datetime(2026, 8, 8, 13, 0, tzinfo=UTC)

_ZONE = PriceZone(zone_id="z1", label="1 Standard", price=None)


# ---------------------------------------------------------------------------
# BFI-side factories
# ---------------------------------------------------------------------------


def make_performance(performance_id: str = PERF_1, **overrides: object) -> Performance:
    defaults: dict[str, object] = {
        "performance_id": performance_id,
        "event_id": "E8A1B2C3-D4E5-F6A7-B8C9-D0E1F2A3B4C5",
        "start_utc": START_UTC,
        "sales_status_code": "S",
        "availability_code": "A",
        "availability_num": 40,
        "reserved_seating": True,
        "seat_map_url": seat_map_url(performance_id),
        "options": (),
    }
    defaults.update(overrides)
    return Performance(**defaults)  # type: ignore[arg-type]


def _seat(row: str, column: int, x: float, status: SeatStatus) -> Seat:
    return Seat(
        seat_id=f"{row}{column}",
        raw_status_code="A" if status is SeatStatus.AVAILABLE else "S",
        status=status,
        zone=_ZONE,
        note="",
        section="BFI IMAX",
        row=row,
        column=column,
        x=x,
        y=100.0,
    )


def seat_map_with(performance_id: str, available: Sequence[str]) -> SeatMap:
    """A 20-seat row L where only *available* seat ids are purchasable.

    Twenty evenly spaced seats give the block generator the three usable gaps it needs
    to establish the row's normal seat spacing, so adjacency is decided by geometry
    rather than falling back to "no evidence, emit nothing".
    """
    offered = set(available)
    step = (630.0 - 70.0) / 19
    return SeatMap(
        performance_id=performance_id,
        seats=tuple(
            _seat(
                "L",
                column,
                70.0 + (column - 1) * step,
                SeatStatus.AVAILABLE if f"L{column}" in offered else SeatStatus.SOLD,
            )
            for column in range(1, 21)
        ),
    )


def centre_pair_map(performance_id: str = PERF_1) -> SeatMap:
    return seat_map_with(performance_id, ("L17", "L18"))


class FakeGateway:
    """Serves canned performances and seat maps, recording which maps were asked for.

    ``list_gate`` lets a test hold a check inside its network phase: the check has
    already read its watch and has no connection open, which is exactly the window a
    concurrent pause, delete, or edit lands in.
    """

    def __init__(self) -> None:
        self.performances: list[Performance] = []
        self.title: str | None = None
        self.maps: dict[str, SeatMap] = {}
        self.seat_map_calls: list[str] = []
        self.list_error: BaseException | None = None
        self.map_error: BaseException | None = None
        self.list_entered = asyncio.Event()
        self.list_gate: asyncio.Event | None = None

    async def list_performances(self, slug: str) -> PerformanceListing:
        self.list_entered.set()
        if self.list_gate is not None:
            await self.list_gate.wait()
        if self.list_error is not None:
            raise self.list_error
        return PerformanceListing(title=self.title, performances=tuple(self.performances))

    async def load_seat_map(self, performance: Performance) -> SeatMap:
        self.seat_map_calls.append(performance.performance_id)
        if self.map_error is not None:
            raise self.map_error
        return self.maps[performance.performance_id]


class ExplodingNotificationRepository(NotificationRepository):
    """A repository whose delivery write fails, to interrupt a check mid-transaction.

    The delivery is the last write in the check transaction, so failing exactly there
    is what shows the earlier writes -- check run, snapshot, options, watch state -- are
    genuinely part of the same unit rather than separately committed along the way.
    """

    async def create_delivery(
        self,
        conn: aiosqlite.Connection,
        idempotency_key: str,
        payload: NotificationPayload,
        now: datetime,
    ) -> NotificationDelivery:
        raise sqlite3.OperationalError("simulated delivery write failure")


# ---------------------------------------------------------------------------
# Watch factories and harness
# ---------------------------------------------------------------------------


def criteria(**overrides: object) -> WatchCriteria:
    defaults: dict[str, object] = {
        "source_url": SOURCE_URL,
        "slug": SLUG,
        "date_from": date(2026, 8, 8),
        "date_to": date(2026, 8, 8),
        "time_from": time(13, 0),
        "time_to": time(15, 0),
        "quantity": 2,
        "mode": WatchMode.RECURRING,
        "interval": INTERVAL,
    }
    defaults.update(overrides)
    return WatchCriteria(**defaults)  # type: ignore[arg-type]


def one_off_criteria(**overrides: object) -> WatchCriteria:
    overrides.setdefault("interval", None)
    return criteria(mode=WatchMode.ONE_OFF, **overrides)


@dataclass
class Harness:
    database: Database
    clock: FakeClock
    gateway: FakeGateway
    circuits: SqliteCircuitStore
    watches: WatchRepository
    results: ResultRepository
    notifications: NotificationRepository
    service: CheckService

    async def add_watch(
        self,
        *,
        owner_user_id: int = 11,
        watch_criteria: WatchCriteria | None = None,
        status: WatchStatus = WatchStatus.ACTIVE,
        next_check_at: datetime | None = NOW,
    ) -> Watch:
        watch = Watch(
            watch_id=uuid4(),
            user_id=owner_user_id,
            criteria=watch_criteria if watch_criteria is not None else criteria(),
            status=status,
            created_at=NOW,
            updated_at=NOW,
            next_check_at=next_check_at,
        )
        async with self.database.connection() as conn, self.database.transaction(conn):
            await self.watches.create(conn, watch)
        return watch

    async def watch(self, watch_id: UUID) -> Watch:
        async with self.database.connection() as conn:
            stored = await self.watches.get(conn, watch_id)
        assert stored is not None
        return stored

    async def latest_snapshot(self, watch_id: UUID) -> ResultSnapshot | None:
        async with self.database.connection() as conn:
            return await self.results.latest_snapshot(conn, watch_id)

    async def deliveries(self) -> tuple[NotificationDelivery, ...]:
        async with self.database.connection() as conn:
            return await self.notifications.due_deliveries(conn, self.clock.now())

    async def check_runs(self) -> list[aiosqlite.Row]:
        async with self.database.connection() as conn:
            cursor = await conn.execute("SELECT * FROM check_runs ORDER BY started_at, id")
            return list(await cursor.fetchall())

    async def due_watch_ids(self, now: datetime) -> list[UUID]:
        async with self.database.connection() as conn:
            return [w.watch_id for w in await self.watches.list_due(conn, now)]

    def watch_service(self) -> WatchService:
        """A second service over the same database, standing in for a concurrent caller."""
        return WatchService(self.database, WatchRepository(), self.clock)

    async def check_during_fetch(
        self,
        watch_id: UUID,
        trigger: CheckTrigger,
        interleaved: Callable[[], Awaitable[None]],
    ) -> None:
        """Run ``interleaved`` while a check is parked in its network phase.

        The check task blocks inside ``list_performances``, so it has already read its
        watch and holds no connection; ``interleaved`` then runs to completion on its own
        connection before the fetch is released. That is the real interleaving a slow BFI
        response creates, made deterministic rather than timing-dependent.
        """
        gate = asyncio.Event()
        self.gateway.list_gate = gate
        task = asyncio.create_task(self.service.check(watch_id, trigger))
        try:
            await asyncio.wait_for(self.gateway.list_entered.wait(), timeout=5)
            await interleaved()
        finally:
            gate.set()
        await task


def build_service(
    harness_parts: tuple[Database, FakeClock, FakeGateway, SqliteCircuitStore],
    *,
    notifications: NotificationRepository,
    jitter: float,
) -> CheckService:
    database, clock, gateway, circuits = harness_parts
    return CheckService(
        database=database,
        gateway=gateway,
        circuits=circuits,
        watches=WatchRepository(),
        results=ResultRepository(database),
        notifications=notifications,
        clock=clock,
        jitter_source=lambda: jitter,
    )


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    database = Database(tmp_path / "cinema.db")
    async with database.connection() as conn:
        await database.migrate(conn)
    clock = FakeClock(NOW)
    gateway = FakeGateway()
    circuits = SqliteCircuitStore(database)
    notifications = NotificationRepository(database)
    yield Harness(
        database=database,
        clock=clock,
        gateway=gateway,
        circuits=circuits,
        watches=WatchRepository(),
        results=ResultRepository(database),
        notifications=notifications,
        service=build_service(
            (database, clock, gateway, circuits), notifications=notifications, jitter=0.0
        ),
    )


async def seed_circuit(
    harness: Harness,
    *,
    state: CircuitState = CircuitState.OPEN,
    next_probe: datetime | None = None,
    generation: int = 1,
    backoff_step: int = 0,
) -> HostCircuit:
    circuit = HostCircuit(
        host=HOST,
        state=state,
        backoff_step=backoff_step,
        generation=generation,
        next_probe=next_probe,
        updated_at=NOW,
    )
    await harness.circuits.save(circuit)
    return circuit


async def seed_open_circuit(
    harness: Harness, *, next_probe: datetime, generation: int = 1, backoff_step: int = 0
) -> HostCircuit:
    return await seed_circuit(
        harness,
        state=CircuitState.OPEN,
        next_probe=next_probe,
        generation=generation,
        backoff_step=backoff_step,
    )


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


async def test_recurring_check_saves_ranked_snapshot_and_pending_digest(
    harness: Harness,
) -> None:
    watch = await harness.add_watch()
    harness.gateway.performances = [make_performance()]
    harness.gateway.maps[PERF_1] = centre_pair_map()

    result = await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    assert result.outcome is CheckOutcome.SUCCESS
    assert result.watch_id == watch.watch_id
    assert result.trigger is CheckTrigger.SCHEDULED
    assert result.error_detail is None
    snapshot = await harness.latest_snapshot(watch.watch_id)
    assert snapshot is not None
    assert [option.seat_label for option in snapshot.options] == ["L17-L18"]
    assert result.snapshot_id == snapshot.snapshot_id
    assert result.option_count == 1
    assert result.performance_count == 1
    deliveries = await harness.deliveries()
    assert [delivery.payload.kind for delivery in deliveries] == ["results"]
    assert deliveries[0].payload.recipient_user_id == watch.user_id
    assert deliveries[0].payload.snapshot_id == snapshot.snapshot_id
    assert deliveries[0].payload.new_option_count == 1


async def test_check_records_a_completed_check_run(harness: Harness) -> None:
    watch = await harness.add_watch()
    harness.gateway.performances = [make_performance()]
    harness.gateway.maps[PERF_1] = centre_pair_map()

    result = await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    rows = await harness.check_runs()
    assert len(rows) == 1
    assert UUID(rows[0]["id"]) == result.check_run_id
    assert rows[0]["outcome"] == CheckOutcome.SUCCESS.value
    assert rows[0]["trigger"] == CheckTrigger.SCHEDULED.value
    assert rows[0]["option_count"] == 1
    assert rows[0]["error_kind"] is None


async def test_full_pagination_is_consumed_through_the_gateway(tmp_path: Path) -> None:
    """A real gateway over a faked transport: page two is fetched and both rows count."""
    database = Database(tmp_path / "cinema.db")
    async with database.connection() as conn:
        await database.migrate(conn)
    clock = FakeClock(NOW)
    page_2_url = pagination_url(TOKEN, 2, ARTICLE_ID)
    transport = FakeTransport(
        {
            SOURCE_URL: fetched_document(
                make_article_html(
                    rows=[performance_row(performance_id=PERF_1, sales_status="S")],
                    current_page=1,
                    total_pages=2,
                    token=TOKEN,
                    article_id=ARTICLE_ID,
                )
            ),
            page_2_url: fetched_document(
                make_article_html(
                    rows=[performance_row(performance_id=PERF_2, sales_status="S")],
                    current_page=2,
                    total_pages=2,
                    token=TOKEN,
                    article_id=ARTICLE_ID,
                )
            ),
            seat_map_url(PERF_1): fetched_document(seat_map_html(performance_id=PERF_1)),
            seat_map_url(PERF_2): fetched_document(seat_map_html(performance_id=PERF_2)),
        }
    )
    watches = WatchRepository()
    service = CheckService(
        database=database,
        gateway=BfiGateway(transport, clock),
        circuits=SqliteCircuitStore(database),
        watches=watches,
        results=ResultRepository(database),
        notifications=NotificationRepository(database),
        clock=clock,
        jitter_source=lambda: 0.0,
    )
    watch = Watch(
        watch_id=uuid4(),
        user_id=11,
        criteria=criteria(quantity=1),
        status=WatchStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        next_check_at=NOW,
    )
    async with database.connection() as conn, database.transaction(conn):
        await watches.create(conn, watch)

    result = await service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    assert transport.call_count(page_2_url) == 1
    assert result.performance_count == 2
    assert result.option_count == 2


async def test_ineligible_performances_never_reach_a_seat_map_fetch(harness: Harness) -> None:
    """Date, time, on-sale, reserved-seating, and quantity filters all run before I/O."""
    harness.gateway.performances = [
        make_performance("00000000-0000-4000-8000-000000000001", start_utc=START_UTC.replace(day=9)),
        make_performance("00000000-0000-4000-8000-000000000002", start_utc=START_UTC.replace(hour=9)),
        make_performance("00000000-0000-4000-8000-000000000003", sales_status_code="C"),
        make_performance("00000000-0000-4000-8000-000000000004", reserved_seating=False),
        make_performance("00000000-0000-4000-8000-000000000005", availability_num=1),
        make_performance(PERF_1),
    ]
    harness.gateway.maps[PERF_1] = centre_pair_map()
    watch = await harness.add_watch()

    result = await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    assert harness.gateway.seat_map_calls == [PERF_1]
    assert result.performance_count == 1


async def test_every_surviving_map_contributes_its_blocks(harness: Harness) -> None:
    harness.gateway.performances = [make_performance(PERF_1), make_performance(PERF_2)]
    harness.gateway.maps[PERF_1] = seat_map_with(PERF_1, ("L17", "L18"))
    harness.gateway.maps[PERF_2] = seat_map_with(PERF_2, ("L2", "L3"))
    watch = await harness.add_watch()

    result = await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    snapshot = await harness.latest_snapshot(watch.watch_id)
    assert snapshot is not None
    assert {option.performance.performance_id for option in snapshot.options} == {PERF_1, PERF_2}
    assert result.option_count == 2


async def test_no_match_writes_an_empty_latest_snapshot(harness: Harness) -> None:
    harness.gateway.performances = [make_performance()]
    harness.gateway.maps[PERF_1] = seat_map_with(PERF_1, ())
    watch = await harness.add_watch()

    result = await harness.service.check(watch.watch_id, CheckTrigger.CREATION)

    assert result.outcome is CheckOutcome.SUCCESS
    snapshot = await harness.latest_snapshot(watch.watch_id)
    assert snapshot is not None
    assert snapshot.options == ()
    assert snapshot.checked_at == harness.clock.now()
    assert result.option_count == 0


async def test_one_off_success_completes_the_watch(harness: Harness) -> None:
    watch = await harness.add_watch(watch_criteria=one_off_criteria())
    harness.gateway.performances = [make_performance()]
    harness.gateway.maps[PERF_1] = centre_pair_map()

    await harness.service.check(watch.watch_id, CheckTrigger.CREATION)

    stored = await harness.watch(watch.watch_id)
    assert stored.status is WatchStatus.COMPLETED
    assert stored.next_check_at is None
    assert stored.last_check_at == harness.clock.now()


async def test_recurring_creation_check_schedules_interval_plus_positive_jitter(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "cinema.db")
    async with database.connection() as conn:
        await database.migrate(conn)
    clock = FakeClock(NOW)
    gateway = FakeGateway()
    gateway.performances = [make_performance()]
    gateway.maps[PERF_1] = centre_pair_map()
    notifications = NotificationRepository(database)
    harness = Harness(
        database=database,
        clock=clock,
        gateway=gateway,
        circuits=SqliteCircuitStore(database),
        watches=WatchRepository(),
        results=ResultRepository(database),
        notifications=notifications,
        service=build_service(
            (database, clock, gateway, SqliteCircuitStore(database)),
            notifications=notifications,
            jitter=0.1,
        ),
    )
    watch = await harness.add_watch()

    await harness.service.check(watch.watch_id, CheckTrigger.CREATION)

    stored = await harness.watch(watch.watch_id)
    assert stored.status is WatchStatus.ACTIVE
    assert stored.next_check_at == NOW + INTERVAL + timedelta(minutes=3)


async def test_manual_check_preserves_an_existing_recurring_next_run_at(
    harness: Harness,
) -> None:
    scheduled_for = NOW + timedelta(minutes=17)
    watch = await harness.add_watch(next_check_at=scheduled_for)
    harness.gateway.performances = [make_performance()]
    harness.gateway.maps[PERF_1] = centre_pair_map()

    await harness.service.check(watch.watch_id, CheckTrigger.MANUAL)

    stored = await harness.watch(watch.watch_id)
    assert stored.next_check_at == scheduled_for
    assert stored.last_check_at == harness.clock.now()


@pytest.mark.parametrize("trigger", [CheckTrigger.MANUAL, CheckTrigger.CREATION])
async def test_owner_initiated_checks_always_queue_a_delivery(
    harness: Harness, trigger: CheckTrigger
) -> None:
    """Nothing found, nothing changed -- but the owner asked, so they get an answer."""
    harness.gateway.performances = []
    watch = await harness.add_watch()

    await harness.service.check(watch.watch_id, trigger)

    deliveries = await harness.deliveries()
    assert [delivery.payload.kind for delivery in deliveries] == ["results"]


async def test_scheduled_check_with_nothing_new_writes_a_snapshot_but_no_delivery(
    harness: Harness,
) -> None:
    harness.gateway.performances = [make_performance()]
    harness.gateway.maps[PERF_1] = centre_pair_map()
    watch = await harness.add_watch()
    await harness.service.check(watch.watch_id, CheckTrigger.CREATION)
    first = await harness.latest_snapshot(watch.watch_id)
    assert first is not None
    # Send the creation digest exactly as the dispatcher would, so its option becomes
    # known and the next check has genuinely nothing new to report.
    async with harness.database.connection() as conn, harness.database.transaction(conn):
        delivery = (await harness.notifications.due_deliveries(conn, NOW))[0]
        await harness.notifications.mark_delivered(
            conn,
            delivery.delivery_id,
            [option.key for option in first.options],
            first.options[0].rank_vector,
            NOW,
        )

    result = await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    assert result.outcome is CheckOutcome.SUCCESS
    snapshot = await harness.latest_snapshot(watch.watch_id)
    assert snapshot is not None
    assert snapshot.snapshot_id == result.snapshot_id
    assert await harness.deliveries() == ()


async def test_successful_check_populates_the_watch_title_from_the_listing(
    harness: Harness,
) -> None:
    watch = await harness.add_watch()
    harness.gateway.title = "Dog Stars"
    harness.gateway.performances = [make_performance()]
    harness.gateway.maps[PERF_1] = centre_pair_map()

    await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    assert (await harness.watch(watch.watch_id)).title == "Dog Stars"


async def test_successful_check_keeps_the_title_even_when_all_performances_are_filtered_out(
    harness: Harness,
) -> None:
    watch = await harness.add_watch()
    harness.gateway.title = "Dog Stars"
    harness.gateway.performances = [make_performance(start_utc=START_UTC.replace(hour=9))]

    result = await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    assert result.performance_count == 0
    assert (await harness.latest_snapshot(watch.watch_id)).options == ()
    assert (await harness.watch(watch.watch_id)).title == "Dog Stars"


async def test_empty_listing_preserves_the_existing_watch_title(harness: Harness) -> None:
    watch = await harness.add_watch()
    stored = replace(watch, title="Existing title", updated_at=NOW + timedelta(seconds=1))
    async with harness.database.connection() as conn, harness.database.transaction(conn):
        await harness.watches.update(conn, stored)
    harness.gateway.title = "Dog Stars"
    harness.gateway.performances = []

    await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    assert (await harness.watch(watch.watch_id)).title == "Existing title"


# ---------------------------------------------------------------------------
# Typed failures
# ---------------------------------------------------------------------------


async def test_contract_error_pauses_the_watch_and_retains_the_latest_snapshot(
    harness: Harness,
) -> None:
    harness.gateway.performances = [make_performance()]
    harness.gateway.maps[PERF_1] = centre_pair_map()
    watch = await harness.add_watch()
    await harness.service.check(watch.watch_id, CheckTrigger.CREATION)
    good_snapshot = await harness.latest_snapshot(watch.watch_id)
    assert good_snapshot is not None
    harness.gateway.list_error = BfiContractError("articleContext not found")

    result = await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    assert result.outcome is CheckOutcome.CONTRACT_ERROR
    assert result.snapshot_id is None
    assert result.error_detail == "articleContext not found"
    stored = await harness.watch(watch.watch_id)
    assert stored.status is WatchStatus.PAUSED
    assert stored.next_check_at is None
    retained = await harness.latest_snapshot(watch.watch_id)
    assert retained is not None
    assert retained.snapshot_id == good_snapshot.snapshot_id
    outcomes = [row["outcome"] for row in await harness.check_runs()]
    assert outcomes.count(CheckOutcome.CONTRACT_ERROR.value) == 1
    kinds = [row["error_kind"] for row in await harness.check_runs()]
    assert kinds.count("contract") == 1


async def test_contract_error_queues_one_alert_for_the_watch_owner(harness: Harness) -> None:
    """A paused watch is silently dead unless its owner is told why it stopped."""
    harness.gateway.list_error = BfiContractError("articleContext not found")
    watch = await harness.add_watch(owner_user_id=77)

    await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    deliveries = await harness.deliveries()
    assert [delivery.payload.kind for delivery in deliveries] == ["contract_error"]
    payload = deliveries[0].payload
    assert payload.recipient_user_id == 77
    assert payload.watch_id == watch.watch_id
    assert payload.snapshot_id is None
    assert payload.host is None
    assert payload.recovery_text is None
    assert deliveries[0].idempotency_key == f"contract_error:{watch.watch_id}"


async def test_repeated_contract_errors_alert_the_owner_only_once(harness: Harness) -> None:
    harness.gateway.list_error = BfiContractError("articleContext not found")
    watch = await harness.add_watch()

    await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)
    await harness.service.check(watch.watch_id, CheckTrigger.MANUAL)

    deliveries = await harness.deliveries()
    assert [delivery.payload.kind for delivery in deliveries] == ["contract_error"]
    assert [row["outcome"] for row in await harness.check_runs()] == [
        CheckOutcome.CONTRACT_ERROR.value,
        CheckOutcome.CONTRACT_ERROR.value,
    ]


async def test_contract_alert_and_the_pause_are_committed_together(tmp_path: Path) -> None:
    """No half state: either the owner is told and the watch is paused, or neither."""
    database = Database(tmp_path / "cinema.db")
    async with database.connection() as conn:
        await database.migrate(conn)
    clock = FakeClock(NOW)
    gateway = FakeGateway()
    gateway.list_error = BfiContractError("articleContext not found")
    circuits = SqliteCircuitStore(database)
    harness = Harness(
        database=database,
        clock=clock,
        gateway=gateway,
        circuits=circuits,
        watches=WatchRepository(),
        results=ResultRepository(database),
        notifications=NotificationRepository(database),
        service=build_service(
            (database, clock, gateway, circuits),
            notifications=ExplodingNotificationRepository(database),
            jitter=0.0,
        ),
    )
    watch = await harness.add_watch()

    with pytest.raises(PersistenceError):
        await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    assert await harness.check_runs() == []
    assert await harness.watch(watch.watch_id) == watch
    assert await harness.deliveries() == ()


async def test_challenge_backs_off_until_the_persisted_host_probe_time(
    harness: Harness,
) -> None:
    probe_at = NOW + timedelta(minutes=15)
    await seed_open_circuit(harness, next_probe=probe_at)
    harness.gateway.list_error = BfiChallengeError("cf-mitigated")
    watch = await harness.add_watch()

    result = await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    assert result.outcome is CheckOutcome.CHALLENGE
    stored = await harness.watch(watch.watch_id)
    assert stored.status is WatchStatus.BACKOFF
    assert stored.next_check_at == probe_at


async def test_challenge_alerts_each_distinct_owner_once_per_generation(
    harness: Harness,
) -> None:
    await seed_open_circuit(harness, next_probe=NOW + timedelta(minutes=15), generation=4)
    harness.gateway.list_error = BfiChallengeError("cf-mitigated")
    watch = await harness.add_watch(owner_user_id=11)
    await harness.add_watch(owner_user_id=11)
    await harness.add_watch(owner_user_id=22)
    await harness.add_watch(
        owner_user_id=33,
        watch_criteria=criteria(
            source_url="https://example.test/imax/Online/article/dog-stars", slug="other"
        ),
    )

    await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    deliveries = await harness.deliveries()
    assert [delivery.payload.kind for delivery in deliveries] == ["degradation", "degradation"]
    assert {delivery.payload.recipient_user_id for delivery in deliveries} == {11, 22}
    assert {delivery.payload.host for delivery in deliveries} == {HOST}
    assert sorted(delivery.idempotency_key for delivery in deliveries) == [
        f"{HOST}:4:degradation:11",
        f"{HOST}:4:degradation:22",
    ]


async def test_second_challenge_in_one_generation_adds_no_second_alert(
    harness: Harness,
) -> None:
    await seed_open_circuit(harness, next_probe=NOW + timedelta(minutes=15), generation=4)
    harness.gateway.list_error = BfiChallengeError("cf-mitigated")
    watch = await harness.add_watch(owner_user_id=11)

    await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)
    await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    assert len(await harness.deliveries()) == 1


async def test_open_circuit_backs_the_watch_off_without_a_new_alert(
    harness: Harness,
) -> None:
    probe_at = NOW + timedelta(minutes=30)
    await seed_open_circuit(harness, next_probe=probe_at, generation=2)
    harness.gateway.list_error = CircuitOpenError(f"circuit for {HOST} is open")
    watch = await harness.add_watch()

    result = await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    assert result.outcome is CheckOutcome.CIRCUIT_OPEN
    stored = await harness.watch(watch.watch_id)
    assert stored.status is WatchStatus.BACKOFF
    assert stored.next_check_at == probe_at
    assert await harness.deliveries() == ()


async def test_network_error_backs_a_recurring_watch_off_until_its_normal_interval(
    harness: Harness,
) -> None:
    harness.gateway.list_error = BfiNetworkError("connection reset")
    watch = await harness.add_watch()

    result = await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    assert result.outcome is CheckOutcome.NETWORK_ERROR
    stored = await harness.watch(watch.watch_id)
    assert stored.status is WatchStatus.BACKOFF
    assert stored.next_check_at == NOW + INTERVAL
    assert await harness.deliveries() == ()


async def test_network_error_fails_a_one_off_watch(harness: Harness) -> None:
    harness.gateway.list_error = BfiNetworkError("connection reset")
    watch = await harness.add_watch(watch_criteria=one_off_criteria())

    result = await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    assert result.outcome is CheckOutcome.NETWORK_ERROR
    stored = await harness.watch(watch.watch_id)
    assert stored.status is WatchStatus.FAILED
    assert stored.next_check_at is None


async def test_seat_map_failure_is_handled_as_its_own_typed_error(harness: Harness) -> None:
    harness.gateway.performances = [make_performance()]
    harness.gateway.map_error = BfiContractError("seat map identity mismatch")
    watch = await harness.add_watch()

    result = await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    assert result.outcome is CheckOutcome.CONTRACT_ERROR
    assert (await harness.watch(watch.watch_id)).status is WatchStatus.PAUSED


async def test_unexpected_defects_are_not_laundered_into_a_failure_outcome(
    harness: Harness,
) -> None:
    """A bug must surface as a bug, not as a tidy CheckResult a caller would ignore."""
    harness.gateway.list_error = ZeroDivisionError("defect")
    watch = await harness.add_watch()

    with pytest.raises(ZeroDivisionError):
        await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    assert (await harness.watch(watch.watch_id)).status is WatchStatus.ACTIVE
    assert await harness.check_runs() == []


async def test_unknown_watch_is_rejected(harness: Harness) -> None:
    with pytest.raises(InputError, match="watch not found"):
        await harness.service.check(uuid4(), CheckTrigger.SCHEDULED)


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


async def test_recovery_check_reactivates_the_watch_and_restores_normal_scheduling(
    harness: Harness,
) -> None:
    await seed_open_circuit(harness, next_probe=NOW - timedelta(minutes=1), generation=4)
    harness.gateway.performances = [make_performance()]
    harness.gateway.maps[PERF_1] = centre_pair_map()
    watch = await harness.add_watch(status=WatchStatus.BACKOFF, next_check_at=NOW)

    result = await harness.service.check(watch.watch_id, CheckTrigger.RECOVERY)

    assert result.outcome is CheckOutcome.SUCCESS
    stored = await harness.watch(watch.watch_id)
    assert stored.status is WatchStatus.ACTIVE
    assert stored.next_check_at == NOW + INTERVAL


async def test_recovery_check_alerts_each_distinct_owner_once_per_generation(
    harness: Harness,
) -> None:
    await seed_open_circuit(harness, next_probe=NOW - timedelta(minutes=1), generation=4)
    harness.gateway.performances = [make_performance()]
    harness.gateway.maps[PERF_1] = centre_pair_map()
    watch = await harness.add_watch(owner_user_id=11, status=WatchStatus.BACKOFF)
    await harness.add_watch(owner_user_id=11, status=WatchStatus.BACKOFF)
    await harness.add_watch(owner_user_id=22, status=WatchStatus.BACKOFF)

    await harness.service.check(watch.watch_id, CheckTrigger.RECOVERY)

    recoveries = [d for d in await harness.deliveries() if d.payload.kind == "recovery"]
    assert sorted(delivery.idempotency_key for delivery in recoveries) == [
        f"{HOST}:4:recovery:11",
        f"{HOST}:4:recovery:22",
    ]
    assert {delivery.payload.recovery_text for delivery in recoveries} == {"back online"}


async def test_other_backoff_watches_stay_due_after_one_recovers(harness: Harness) -> None:
    """Recovery reactivates only the probed watch; its siblings stay due for their turn."""
    await seed_open_circuit(harness, next_probe=NOW - timedelta(minutes=1), generation=4)
    harness.gateway.performances = [make_performance()]
    harness.gateway.maps[PERF_1] = centre_pair_map()
    probed = await harness.add_watch(status=WatchStatus.BACKOFF, next_check_at=NOW)
    sibling = await harness.add_watch(status=WatchStatus.BACKOFF, next_check_at=NOW)

    await harness.service.check(probed.watch_id, CheckTrigger.RECOVERY)

    assert (await harness.watch(sibling.watch_id)).status is WatchStatus.BACKOFF
    # The probed watch has moved on to its normal interval; the sibling is still due,
    # which is only true because list_due considers BACKOFF rows as well as ACTIVE ones.
    assert await harness.due_watch_ids(NOW) == [sibling.watch_id]


async def test_scheduled_success_on_a_backoff_watch_does_not_alert_recovery(
    harness: Harness,
) -> None:
    """Only a recovery-triggered probe announces the host is back."""
    await seed_open_circuit(harness, next_probe=NOW - timedelta(minutes=1), generation=4)
    harness.gateway.performances = [make_performance()]
    harness.gateway.maps[PERF_1] = centre_pair_map()
    watch = await harness.add_watch(status=WatchStatus.BACKOFF)

    await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    assert [d.payload.kind for d in await harness.deliveries()] == ["results"]
    assert (await harness.watch(watch.watch_id)).status is WatchStatus.ACTIVE


async def test_recovery_without_a_recorded_host_incident_announces_nothing(
    harness: Harness,
) -> None:
    """A watch backed off by exhausted network retries never took the host down with it.

    Nothing has touched the circuit, so it is closed at generation 0. Announcing "back
    online" here would tell every owner on the host about an outage that never happened,
    and would burn the generation-0 idempotency key while doing it.
    """
    harness.gateway.performances = [make_performance()]
    harness.gateway.maps[PERF_1] = centre_pair_map()
    watch = await harness.add_watch(status=WatchStatus.BACKOFF, next_check_at=NOW)

    await harness.service.check(watch.watch_id, CheckTrigger.RECOVERY)

    assert [d.payload.kind for d in await harness.deliveries()] == ["results"]
    assert (await harness.watch(watch.watch_id)).status is WatchStatus.ACTIVE
    assert (await harness.circuits.load(HOST)).generation == 0


async def test_recovery_after_a_prior_incident_alerts_on_that_generation(
    harness: Harness,
) -> None:
    """The transport's probe closes the circuit before the check returns; gen 3 still owns it."""
    await seed_circuit(harness, state=CircuitState.CLOSED, generation=3, next_probe=None)
    harness.gateway.performances = [make_performance()]
    harness.gateway.maps[PERF_1] = centre_pair_map()
    watch = await harness.add_watch(owner_user_id=11, status=WatchStatus.BACKOFF)

    await harness.service.check(watch.watch_id, CheckTrigger.RECOVERY)

    recoveries = [d for d in await harness.deliveries() if d.payload.kind == "recovery"]
    assert [d.idempotency_key for d in recoveries] == [f"{HOST}:3:recovery:11"]


async def test_a_second_recovery_in_one_generation_adds_no_second_alert(
    harness: Harness,
) -> None:
    await seed_circuit(harness, state=CircuitState.CLOSED, generation=3)
    harness.gateway.performances = [make_performance()]
    harness.gateway.maps[PERF_1] = centre_pair_map()
    first = await harness.add_watch(owner_user_id=11, status=WatchStatus.BACKOFF)
    second = await harness.add_watch(owner_user_id=11, status=WatchStatus.BACKOFF)

    await harness.service.check(first.watch_id, CheckTrigger.RECOVERY)
    await harness.service.check(second.watch_id, CheckTrigger.RECOVERY)

    recoveries = [d for d in await harness.deliveries() if d.payload.kind == "recovery"]
    assert len(recoveries) == 1


# ---------------------------------------------------------------------------
# Concurrent lifecycle changes during the fetch
# ---------------------------------------------------------------------------


async def test_a_watch_paused_mid_fetch_is_not_dragged_back_onto_the_schedule(
    harness: Harness,
) -> None:
    """Pause wins: the check that started before it must not resurrect the schedule."""
    harness.gateway.performances = [make_performance()]
    harness.gateway.maps[PERF_1] = centre_pair_map()
    watch = await harness.add_watch(owner_user_id=11)

    async def pause() -> None:
        await harness.watch_service().pause(11, watch.watch_id)

    with pytest.raises(ConflictError):
        await harness.check_during_fetch(watch.watch_id, CheckTrigger.SCHEDULED, pause)

    stored = await harness.watch(watch.watch_id)
    assert stored.status is WatchStatus.PAUSED
    assert stored.next_check_at is None
    assert stored.last_check_at is None
    assert await harness.check_runs() == []
    assert await harness.latest_snapshot(watch.watch_id) is None
    assert await harness.deliveries() == ()


async def test_a_watch_deleted_mid_fetch_is_not_resurrected(harness: Harness) -> None:
    """Deletion must abort the check outright, not surface as a foreign-key failure."""
    harness.gateway.performances = [make_performance()]
    harness.gateway.maps[PERF_1] = centre_pair_map()
    watch = await harness.add_watch(owner_user_id=11)

    async def delete() -> None:
        await harness.watch_service().delete(11, watch.watch_id)

    with pytest.raises(ConflictError):
        await harness.check_during_fetch(watch.watch_id, CheckTrigger.MANUAL, delete)

    async with harness.database.connection() as conn:
        assert await harness.watches.get(conn, watch.watch_id) is None
    assert await harness.check_runs() == []
    assert await harness.deliveries() == ()


async def test_a_watch_deleted_mid_fetch_records_no_failure_either(harness: Harness) -> None:
    """The same guard covers the failure path, which writes a check run of its own."""
    harness.gateway.list_error = BfiNetworkError("connection reset")
    watch = await harness.add_watch(owner_user_id=11)

    async def delete() -> None:
        await harness.watch_service().delete(11, watch.watch_id)

    with pytest.raises(ConflictError):
        await harness.check_during_fetch(watch.watch_id, CheckTrigger.SCHEDULED, delete)

    assert await harness.check_runs() == []


async def test_criteria_edited_mid_fetch_abort_the_check_and_survive_it(
    harness: Harness,
) -> None:
    """Results ranked against the old criteria must not be stored as the new ones' answer."""
    harness.gateway.performances = [make_performance()]
    harness.gateway.maps[PERF_1] = centre_pair_map()
    watch = await harness.add_watch()
    edited = replace(watch, criteria=criteria(quantity=4), updated_at=NOW + timedelta(seconds=1))

    async def edit() -> None:
        async with harness.database.connection() as conn, harness.database.transaction(conn):
            await harness.watches.update(conn, edited)

    with pytest.raises(ConflictError):
        await harness.check_during_fetch(watch.watch_id, CheckTrigger.SCHEDULED, edit)

    stored = await harness.watch(watch.watch_id)
    assert stored == edited
    assert await harness.check_runs() == []
    assert await harness.latest_snapshot(watch.watch_id) is None


async def test_a_concurrent_check_that_lands_first_aborts_the_slower_one(
    harness: Harness,
) -> None:
    """Two checks on one watch produce one snapshot, not a blind overwrite of the newer."""
    harness.gateway.performances = [make_performance()]
    harness.gateway.maps[PERF_1] = centre_pair_map()
    watch = await harness.add_watch()
    fast_gateway = FakeGateway()
    fast_gateway.performances = [make_performance()]
    fast_gateway.maps[PERF_1] = centre_pair_map()
    fast_service = build_service(
        (harness.database, harness.clock, fast_gateway, harness.circuits),
        notifications=harness.notifications,
        jitter=0.0,
    )

    async def run_other_check() -> None:
        await fast_service.check(watch.watch_id, CheckTrigger.MANUAL)

    with pytest.raises(ConflictError):
        await harness.check_during_fetch(watch.watch_id, CheckTrigger.SCHEDULED, run_other_check)

    assert [row["trigger"] for row in await harness.check_runs()] == [CheckTrigger.MANUAL.value]
    assert len(await harness.deliveries()) == 1
    stored = await harness.watch(watch.watch_id)
    assert stored.next_check_at == NOW
    assert stored.last_check_at == NOW


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


async def test_database_failure_rolls_back_the_check_snapshot_and_watch_changes(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "cinema.db")
    async with database.connection() as conn:
        await database.migrate(conn)
    clock = FakeClock(NOW)
    gateway = FakeGateway()
    gateway.performances = [make_performance()]
    gateway.maps[PERF_1] = centre_pair_map()
    circuits = SqliteCircuitStore(database)
    harness = Harness(
        database=database,
        clock=clock,
        gateway=gateway,
        circuits=circuits,
        watches=WatchRepository(),
        results=ResultRepository(database),
        notifications=NotificationRepository(database),
        service=build_service(
            (database, clock, gateway, circuits),
            notifications=ExplodingNotificationRepository(database),
            jitter=0.0,
        ),
    )
    watch = await harness.add_watch()

    with pytest.raises(PersistenceError):
        await harness.service.check(watch.watch_id, CheckTrigger.CREATION)

    assert await harness.check_runs() == []
    assert await harness.latest_snapshot(watch.watch_id) is None
    assert await harness.watch(watch.watch_id) == watch
    assert await harness.deliveries() == ()


async def test_a_rolled_back_check_leaves_the_persisted_circuit_intact(
    tmp_path: Path,
) -> None:
    """The stop-hitting-this-host signal must outlive the check that discovered it."""
    database = Database(tmp_path / "cinema.db")
    async with database.connection() as conn:
        await database.migrate(conn)
    clock = FakeClock(NOW)
    gateway = FakeGateway()
    gateway.list_error = BfiChallengeError("cf-mitigated")
    circuits = SqliteCircuitStore(database)
    harness = Harness(
        database=database,
        clock=clock,
        gateway=gateway,
        circuits=circuits,
        watches=WatchRepository(),
        results=ResultRepository(database),
        notifications=NotificationRepository(database),
        service=build_service(
            (database, clock, gateway, circuits),
            notifications=ExplodingNotificationRepository(database),
            jitter=0.0,
        ),
    )
    probe_at = NOW + timedelta(minutes=15)
    await seed_open_circuit(harness, next_probe=probe_at)
    watch = await harness.add_watch()

    with pytest.raises(PersistenceError):
        await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    assert (await harness.watch(watch.watch_id)).status is WatchStatus.ACTIVE
    circuit = await circuits.load(HOST)
    assert circuit.state is CircuitState.OPEN
    assert circuit.next_probe == probe_at


async def test_each_snapshot_gets_exactly_one_result_delivery(harness: Harness) -> None:
    """The delivery key is snapshot-scoped: re-queueing one snapshot is a no-op."""
    harness.gateway.performances = [make_performance()]
    harness.gateway.maps[PERF_1] = centre_pair_map()
    watch = await harness.add_watch()

    first = await harness.service.check(watch.watch_id, CheckTrigger.MANUAL)
    second = await harness.service.check(watch.watch_id, CheckTrigger.MANUAL)

    deliveries = await harness.deliveries()
    assert first.snapshot_id != second.snapshot_id
    assert {delivery.payload.snapshot_id for delivery in deliveries} == {
        first.snapshot_id,
        second.snapshot_id,
    }
    original = next(
        delivery for delivery in deliveries if delivery.payload.snapshot_id == first.snapshot_id
    )
    async with harness.database.connection() as conn, harness.database.transaction(conn):
        replayed = await harness.notifications.create_delivery(
            conn,
            original.idempotency_key,
            NotificationPayload(
                kind="results",
                recipient_user_id=watch.user_id,
                watch_id=watch.watch_id,
                snapshot_id=first.snapshot_id,
                new_option_count=1,
                host=None,
                recovery_text=None,
            ),
            NOW,
        )
    assert replayed.delivery_id == original.delivery_id
    assert len(await harness.deliveries()) == 2
