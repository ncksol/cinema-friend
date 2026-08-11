"""Persists one host circuit per host, with compare-and-swap for contested transitions.

This is the concrete :class:`~cinema_friend.bfi.transport.HostCircuitStore`. Unlike the
other repositories it owns the connection it was given rather than accepting one per
call, because the transport holds it for the process lifetime and its writes are single
statements that never need to join a caller's transaction.

Two write paths exist and they are not interchangeable:

- :meth:`save` is unconditional last-writer-wins. It is for seeding and administrative
  writes where no other writer can be in flight.
- :meth:`compare_and_swap` is the fenced path every transport transition uses. The caller
  passes the circuit it wants to store carrying the ``revision`` it observed; the write
  lands only while the stored row still has that revision. A process whose view has been
  superseded -- because another process claimed the probe or recorded a newer incident --
  loses the swap and drops its write instead of clobbering the newer state.
"""

from __future__ import annotations

from datetime import datetime

import aiosqlite

from cinema_friend.domain.results import CircuitState, HostCircuit
from cinema_friend.storage.database import decode_datetime, encode_datetime

_EPOCH = datetime.fromisoformat("1970-01-01T00:00:00+00:00")


def _row_to_circuit(row: aiosqlite.Row) -> HostCircuit:
    next_probe = row["next_probe_at"]
    return HostCircuit(
        host=row["host"],
        state=CircuitState(row["state"]),
        backoff_step=row["step"],
        generation=row["generation"],
        next_probe=decode_datetime(next_probe) if next_probe is not None else None,
        updated_at=decode_datetime(row["updated_at"]),
        revision=row["revision"],
    )


def _columns(circuit: HostCircuit) -> tuple[str, int, int, str | None, str]:
    return (
        circuit.state.value,
        circuit.backoff_step,
        circuit.generation,
        encode_datetime(circuit.next_probe) if circuit.next_probe is not None else None,
        encode_datetime(circuit.updated_at),
    )


class SqliteCircuitStore:
    """SQLite-backed host circuit storage."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def load(self, host: str) -> HostCircuit:
        """Return the stored circuit, or a closed step-zero generation-zero default.

        The default carries ``revision = 0``, which is how a caller's later
        compare-and-swap declares "I expect no row to exist yet".
        """
        cursor = await self._conn.execute(
            "SELECT * FROM host_circuits WHERE host = ?", (host,)
        )
        row = await cursor.fetchone()
        if row is None:
            return HostCircuit(
                host=host,
                state=CircuitState.CLOSED,
                backoff_step=0,
                generation=0,
                next_probe=None,
                updated_at=_EPOCH,
                revision=0,
            )
        return _row_to_circuit(row)

    async def save(self, circuit: HostCircuit) -> None:
        """Write ``circuit`` unconditionally, advancing the revision.

        Every field is persisted exactly as supplied, including ``generation``: the
        closed-to-open transition rule lives in the transport, and a store that advanced
        the generation itself would double-count each incident and disagree with the
        in-memory store the transport tests run against.
        """
        await self._conn.execute(
            """
            INSERT INTO host_circuits
                (host, state, step, generation, next_probe_at, updated_at, revision)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(host) DO UPDATE SET
                state = excluded.state,
                step = excluded.step,
                generation = excluded.generation,
                next_probe_at = excluded.next_probe_at,
                updated_at = excluded.updated_at,
                revision = host_circuits.revision + 1
            """,
            (circuit.host, *_columns(circuit)),
        )

    async def compare_and_swap(self, circuit: HostCircuit) -> HostCircuit | None:
        """Store ``circuit`` only while its ``revision`` is still the stored one.

        Returns the persisted circuit carrying its new revision, or ``None`` when another
        writer moved the row first. Both branches are a single statement, so the check and
        the write cannot be interleaved by another connection.
        """
        if circuit.revision == 0:
            cursor = await self._conn.execute(
                """
                INSERT INTO host_circuits
                    (host, state, step, generation, next_probe_at, updated_at, revision)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(host) DO NOTHING
                """,
                (circuit.host, *_columns(circuit)),
            )
            return None if cursor.rowcount != 1 else _with_revision(circuit, 1)

        next_revision = circuit.revision + 1
        cursor = await self._conn.execute(
            """
            UPDATE host_circuits
            SET state = ?, step = ?, generation = ?, next_probe_at = ?, updated_at = ?,
                revision = ?
            WHERE host = ? AND revision = ?
            """,
            (*_columns(circuit), next_revision, circuit.host, circuit.revision),
        )
        return None if cursor.rowcount != 1 else _with_revision(circuit, next_revision)


def _with_revision(circuit: HostCircuit, revision: int) -> HostCircuit:
    return HostCircuit(
        host=circuit.host,
        state=circuit.state,
        backoff_step=circuit.backoff_step,
        generation=circuit.generation,
        next_probe=circuit.next_probe,
        updated_at=circuit.updated_at,
        revision=revision,
    )
