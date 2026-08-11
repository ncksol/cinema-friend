"""Tests for cinema_friend.storage.circuit_repository.

The circuit is the one row two independent processes race over: both may see an elapsed
OPEN circuit and both may try to become the single permitted prober, and a writer from a
resolved incident may replay its transition over a newer one. The transport's in-process
locks cannot arbitrate either case, so these tests drive two stores on two separate
connections -- the persistence-level stand-in for two processes -- and assert exactly one
writer wins every contested transition.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from cinema_friend.bfi.transport import BfiTransport, DocumentKind, HostCircuitStore
from cinema_friend.config import Settings
from cinema_friend.domain.errors import BfiChallengeError, CircuitOpenError
from cinema_friend.domain.results import CircuitState, HostCircuit
from cinema_friend.storage.circuit_repository import SqliteCircuitStore
from cinema_friend.storage.database import Database
from tests.fakes import FakeClock, FakeSession, response

HOST = "whatson.bfi.org.uk"
FILM_URL = "https://whatson.bfi.org.uk/imax/Online/article/dog-stars"
SEAT_MAP_URL = "https://whatson.bfi.org.uk/imax/Online/mapSelect.asp?performanceId=1"
ARTICLE = "<html><body>dog stars</body></html>"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_DEFAULT_PROBE = NOW + timedelta(minutes=30)


@pytest.fixture
def store(conn: aiosqlite.Connection) -> SqliteCircuitStore:
    return SqliteCircuitStore(conn)


def circuit(
    *,
    state: CircuitState = CircuitState.OPEN,
    backoff_step: int = 1,
    generation: int = 2,
    next_probe: datetime | None = _DEFAULT_PROBE,
    updated_at: datetime = NOW,
    revision: int = 0,
) -> HostCircuit:
    return HostCircuit(
        host=HOST,
        state=state,
        backoff_step=backoff_step,
        generation=generation,
        next_probe=next_probe,
        updated_at=updated_at,
        revision=revision,
    )


def with_revision(value: HostCircuit, revision: int) -> HostCircuit:
    return HostCircuit(
        host=value.host,
        state=value.state,
        backoff_step=value.backoff_step,
        generation=value.generation,
        next_probe=value.next_probe,
        updated_at=value.updated_at,
        revision=revision,
    )


def settings() -> Settings:
    return Settings(
        telegram_bot_token="token",
        allowed_user_ids=frozenset({11}),
        database_path=Path("unused.db"),  # never opened by the transport under test
    )


def elapsed_open(clock: FakeClock) -> HostCircuit:
    """An OPEN circuit whose probe window has already passed."""
    return HostCircuit(
        host=HOST,
        state=CircuitState.OPEN,
        backoff_step=0,
        generation=1,
        next_probe=clock.now() - timedelta(seconds=1),
        updated_at=clock.now() - timedelta(minutes=15),
    )


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


async def test_load_returns_a_closed_zero_circuit_when_no_row_exists(
    store: SqliteCircuitStore,
) -> None:
    loaded = await store.load(HOST)
    assert loaded.host == HOST
    assert loaded.state is CircuitState.CLOSED
    assert loaded.backoff_step == 0
    assert loaded.generation == 0
    assert loaded.next_probe is None
    assert loaded.revision == 0


async def test_save_round_trips_every_field(store: SqliteCircuitStore) -> None:
    await store.save(circuit(next_probe=NOW + timedelta(hours=2), updated_at=NOW))
    loaded = await store.load(HOST)
    assert loaded.state is CircuitState.OPEN
    assert loaded.backoff_step == 1
    assert loaded.generation == 2
    assert loaded.next_probe == NOW + timedelta(hours=2)
    assert loaded.updated_at == NOW


async def test_save_round_trips_a_closed_circuit_without_a_probe_time(
    store: SqliteCircuitStore,
) -> None:
    await store.save(circuit(state=CircuitState.CLOSED, backoff_step=0, next_probe=None))
    loaded = await store.load(HOST)
    assert loaded.state is CircuitState.CLOSED
    assert loaded.next_probe is None


async def test_save_persists_the_supplied_generation_verbatim(
    store: SqliteCircuitStore,
) -> None:
    """Generation is the transport's transition rule, not a hidden store side effect.

    If the store also advanced it, a closed-circuit trip would land two generations on
    and the transport's incident-coalescing comparison would never match.
    """
    await store.save(circuit(state=CircuitState.CLOSED, generation=3, next_probe=None))
    await store.save(circuit(state=CircuitState.OPEN, generation=4))
    assert (await store.load(HOST)).generation == 4


async def test_save_advances_the_revision_on_every_write(store: SqliteCircuitStore) -> None:
    await store.save(circuit())
    first = await store.load(HOST)
    await store.save(circuit())
    second = await store.load(HOST)
    assert first.revision == 1
    assert second.revision == 2


async def test_circuits_for_different_hosts_are_independent(
    store: SqliteCircuitStore,
) -> None:
    await store.save(circuit())
    other = await store.load("example.test")
    assert other.state is CircuitState.CLOSED
    assert other.revision == 0


async def test_a_row_written_before_the_fencing_migration_adopts_revision_one(
    conn: aiosqlite.Connection,
) -> None:
    """An upgraded deployment's existing circuit must be swappable, not stuck at zero.

    Revision 0 means "no row yet", so a migrated row defaulting to 0 would make every
    caller take the insert branch and lose against a row that already exists.
    """
    await conn.execute(
        """
        INSERT INTO host_circuits (host, state, step, generation, next_probe_at, updated_at)
        VALUES (?, 'open', 1, 2, NULL, ?)
        """,
        (HOST, "2026-01-01T12:00:00.000000+00:00"),
    )
    store = SqliteCircuitStore(conn)
    existing = await store.load(HOST)
    assert existing.revision == 1
    assert await store.compare_and_swap(circuit(revision=existing.revision)) is not None


# ---------------------------------------------------------------------------
# Compare and swap
# ---------------------------------------------------------------------------

async def test_compare_and_swap_creates_the_row_when_none_exists(
    store: SqliteCircuitStore,
) -> None:
    observed = await store.load(HOST)
    written = await store.compare_and_swap(circuit(generation=1, revision=observed.revision))
    assert written is not None
    assert written.revision == 1
    assert (await store.load(HOST)).generation == 1


async def test_compare_and_swap_advances_the_revision(store: SqliteCircuitStore) -> None:
    await store.save(circuit())
    observed = await store.load(HOST)
    written = await store.compare_and_swap(
        circuit(state=CircuitState.HALF_OPEN, revision=observed.revision)
    )
    assert written is not None
    assert written.revision == observed.revision + 1
    assert (await store.load(HOST)).revision == observed.revision + 1


async def test_compare_and_swap_rejects_a_writer_holding_a_stale_revision(
    store: SqliteCircuitStore,
) -> None:
    await store.save(circuit())
    stale = await store.load(HOST)
    winner = await store.compare_and_swap(
        circuit(state=CircuitState.HALF_OPEN, revision=stale.revision)
    )
    assert winner is not None

    loser = await store.compare_and_swap(
        circuit(state=CircuitState.CLOSED, backoff_step=0, revision=stale.revision)
    )
    assert loser is None
    assert (await store.load(HOST)).state is CircuitState.HALF_OPEN


async def test_a_stale_writer_cannot_overwrite_a_newer_generation(
    store: SqliteCircuitStore,
) -> None:
    """The exact cross-process hazard: an old incident replaying over a newer one."""
    await store.save(circuit(state=CircuitState.OPEN, backoff_step=0, generation=1))
    stale = await store.load(HOST)

    recovered = await store.compare_and_swap(
        circuit(state=CircuitState.CLOSED, backoff_step=0, generation=1, revision=stale.revision)
    )
    assert recovered is not None
    fresh_trip = await store.compare_and_swap(
        circuit(state=CircuitState.OPEN, backoff_step=0, generation=2, revision=recovered.revision)
    )
    assert fresh_trip is not None

    replayed = await store.compare_and_swap(
        circuit(state=CircuitState.OPEN, backoff_step=1, generation=1, revision=stale.revision)
    )
    assert replayed is None
    current = await store.load(HOST)
    assert current.generation == 2
    assert current.backoff_step == 0


async def test_compare_and_swap_from_absent_loses_to_a_concurrent_creator(
    database: Database, store: SqliteCircuitStore
) -> None:
    other_conn = await database.connect()
    try:
        other = SqliteCircuitStore(other_conn)
        first_view = await store.load(HOST)
        second_view = await other.load(HOST)
        assert first_view.revision == second_view.revision == 0

        assert await store.compare_and_swap(circuit(generation=1, revision=0)) is not None
        assert await other.compare_and_swap(circuit(generation=9, revision=0)) is None
        assert (await store.load(HOST)).generation == 1
    finally:
        await other_conn.close()


async def test_two_connections_claiming_one_probe_produce_a_single_winner(
    database: Database, store: SqliteCircuitStore
) -> None:
    await store.save(circuit(state=CircuitState.OPEN, backoff_step=1, generation=2))
    other_conn = await database.connect()
    try:
        other = SqliteCircuitStore(other_conn)
        first_view = await store.load(HOST)
        second_view = await other.load(HOST)

        claim = circuit(state=CircuitState.HALF_OPEN, updated_at=NOW + timedelta(seconds=1))
        outcomes = [
            await store.compare_and_swap(with_revision(claim, first_view.revision)),
            await other.compare_and_swap(with_revision(claim, second_view.revision)),
        ]
        assert [item is not None for item in outcomes] == [True, False]
        assert (await other.load(HOST)).state is CircuitState.HALF_OPEN
    finally:
        await other_conn.close()


# ---------------------------------------------------------------------------
# Transport integration across two connections (two-process stand-in)
# ---------------------------------------------------------------------------


class RendezvousStore:
    """Holds each racer's first compare-and-swap until both have read the circuit.

    Two transports have no visibility of each other's locks, so the dangerous
    interleaving is "both read, then both write". Waiting on a shared barrier
    immediately before the first write reproduces it deterministically instead of
    hoping the event loop happens to schedule it that way.
    """

    def __init__(self, inner: SqliteCircuitStore, barrier: asyncio.Barrier) -> None:
        self._inner = inner
        self._barrier = barrier
        self._armed = True

    async def load(self, host: str) -> HostCircuit:
        return await self._inner.load(host)

    async def save(self, circuit: HostCircuit) -> None:
        await self._inner.save(circuit)

    async def compare_and_swap(self, circuit: HostCircuit) -> HostCircuit | None:
        if self._armed:
            self._armed = False
            await self._barrier.wait()
        return await self._inner.compare_and_swap(circuit)


async def test_two_transports_sharing_a_database_run_exactly_one_probe(
    database: Database, conn: aiosqlite.Connection
) -> None:
    """Two transports, two connections, one elapsed circuit: one probe, one rejection."""
    clock = FakeClock(NOW)
    seed = SqliteCircuitStore(conn)
    await seed.save(elapsed_open(clock))

    connections = [await database.connect(), await database.connect()]
    try:
        barrier = asyncio.Barrier(2)
        sessions = [FakeSession([response(200, ARTICLE)]) for _ in connections]
        stores: list[HostCircuitStore] = [
            RendezvousStore(SqliteCircuitStore(connection), barrier)
            for connection in connections
        ]
        transports = [
            BfiTransport(settings(), store, clock, session=session, jitter_source=lambda: 0.0)
            for store, session in zip(stores, sessions, strict=True)
        ]
        outcomes = await asyncio.gather(
            transports[0].get(FILM_URL, DocumentKind.ARTICLE),
            transports[1].get(SEAT_MAP_URL, DocumentKind.SEAT_MAP),
            return_exceptions=True,
        )

        assert sorted(type(item).__name__ for item in outcomes) == [
            "CircuitOpenError",
            "FetchedDocument",
        ]
        assert sum(len(session.calls) for session in sessions) == 1
        final = await seed.load(HOST)
        assert final.state is CircuitState.CLOSED
        assert final.generation == 1
    finally:
        for connection in connections:
            await connection.close()


async def test_a_superseded_prober_cannot_close_a_circuit_another_process_reopened(
    database: Database, conn: aiosqlite.Connection
) -> None:
    """A slow prober's success must not erase a newer incident recorded elsewhere."""
    clock = FakeClock(NOW)
    seed = SqliteCircuitStore(conn)
    await seed.save(elapsed_open(clock))

    prober_conn = await database.connect()
    try:
        prober = BfiTransport(
            settings(),
            SqliteCircuitStore(prober_conn),
            clock,
            session=FakeSession([response(200, ARTICLE)]),
            jitter_source=lambda: 0.0,
        )
        claimed = await prober._begin_circuit_check(HOST)
        assert claimed.probing

        # Another process treats the claim as abandoned and records a new incident,
        # advancing the persisted revision past the prober's claim.
        superseding = await seed.load(HOST)
        assert await seed.compare_and_swap(
            HostCircuit(
                host=HOST,
                state=CircuitState.OPEN,
                backoff_step=1,
                generation=2,
                next_probe=clock.now() + timedelta(minutes=30),
                updated_at=clock.now(),
                revision=superseding.revision,
            )
        )

        await prober._close_circuit(HOST, claimed)

        current = await seed.load(HOST)
        assert current.state is CircuitState.OPEN
        assert current.generation == 2
        assert current.backoff_step == 1
    finally:
        await prober_conn.close()


async def test_a_superseded_prober_cannot_advance_a_newer_incident(
    database: Database, conn: aiosqlite.Connection
) -> None:
    """The failure-path mirror: a stale probe failure must not advance the newer backoff."""
    clock = FakeClock(NOW)
    seed = SqliteCircuitStore(conn)
    await seed.save(elapsed_open(clock))

    prober_conn = await database.connect()
    try:
        prober = BfiTransport(
            settings(),
            SqliteCircuitStore(prober_conn),
            clock,
            session=FakeSession([]),
            jitter_source=lambda: 0.0,
        )
        claimed = await prober._begin_circuit_check(HOST)
        superseding = await seed.load(HOST)
        assert await seed.compare_and_swap(
            HostCircuit(
                host=HOST,
                state=CircuitState.OPEN,
                backoff_step=3,
                generation=2,
                next_probe=clock.now() + timedelta(hours=2),
                updated_at=clock.now(),
                revision=superseding.revision,
            )
        )

        await prober._advance_circuit_after_challenge(HOST, claimed)

        current = await seed.load(HOST)
        assert current.generation == 2
        assert current.backoff_step == 3
    finally:
        await prober_conn.close()


async def test_open_circuit_still_blocks_when_read_from_sqlite(
    conn: aiosqlite.Connection,
) -> None:
    clock = FakeClock(NOW)
    store = SqliteCircuitStore(conn)
    await store.save(
        HostCircuit(
            host=HOST,
            state=CircuitState.OPEN,
            backoff_step=0,
            generation=1,
            next_probe=clock.now() + timedelta(minutes=15),
            updated_at=clock.now(),
        )
    )
    session = FakeSession([])
    transport = BfiTransport(settings(), store, clock, session=session, jitter_source=lambda: 0.0)
    with pytest.raises(CircuitOpenError):
        await transport.get(FILM_URL, DocumentKind.ARTICLE)
    assert session.calls == []


async def test_a_challenge_trips_the_persisted_circuit_for_every_process(
    database: Database, conn: aiosqlite.Connection
) -> None:
    """The trip must be visible to another connection, not just to the transport that saw it."""
    clock = FakeClock(NOW)
    other_conn = await database.connect()
    try:
        transport = BfiTransport(
            settings(),
            SqliteCircuitStore(other_conn),
            clock,
            session=FakeSession([response(403, "", headers={"cf-mitigated": "challenge"})]),
            jitter_source=lambda: 0.0,
        )
        with pytest.raises(BfiChallengeError):
            await transport.get(FILM_URL, DocumentKind.ARTICLE)

        observer = await SqliteCircuitStore(conn).load(HOST)
        assert observer.state is CircuitState.OPEN
        assert observer.generation == 1
        assert observer.backoff_step == 0
        assert observer.next_probe == NOW + timedelta(minutes=15)
    finally:
        await other_conn.close()
