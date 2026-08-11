"""Persists notification deliveries, per-watch notification state, and known options.

Two invariants drive the shape of this module.

The first is that one event produces one message. Every delivery carries a caller-chosen
idempotency key and :meth:`NotificationRepository.create_delivery` returns the existing
row when that key is already present, so a check that re-runs -- after a crash, a retry,
or a duplicate schedule tick -- re-uses the queued or already-sent delivery instead of
queueing a second copy.

The second is that an option is only "known" once the message announcing it has actually
been sent. :meth:`NotificationRepository.mark_delivered` therefore performs the status
change, the option marking, and the state update in one transaction: if any part fails,
the delivery stays pending and is retried, rather than being silently forgotten because
its options were already recorded as announced.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import aiosqlite

from cinema_friend.domain.errors import InputError
from cinema_friend.domain.results import (
    DeliveryStatus,
    NotificationDelivery,
    NotificationPayload,
    NotificationState,
    RankVector,
)
from cinema_friend.storage.database import Database, decode_datetime, encode_datetime

# Kinds whose delivery moves the per-watch degradation flags. Any other kind leaves them
# exactly as they were, so announcing new options never cancels an owed recovery message.
_DEGRADATION_KIND = "degradation"
_RECOVERY_KIND = "recovery"


def _encode_payload(payload: NotificationPayload) -> str:
    return json.dumps(
        {
            "kind": payload.kind,
            "recipient_user_id": payload.recipient_user_id,
            "watch_id": None if payload.watch_id is None else str(payload.watch_id),
            "snapshot_id": None if payload.snapshot_id is None else str(payload.snapshot_id),
            "new_option_count": payload.new_option_count,
            "host": payload.host,
            "recovery_text": payload.recovery_text,
        },
        sort_keys=True,
    )


def _decode_payload(data: str, snapshot_id: str | None) -> NotificationPayload:
    """Rebuild a payload, trusting the column over the blob for the snapshot pointer.

    Retention nulls ``notification_deliveries.snapshot_id`` when the snapshot it points
    at is pruned. The JSON blob still holds the old id, so reading it back would hand a
    caller a snapshot reference that no longer resolves.
    """
    payload: dict[str, Any] = json.loads(data)
    return NotificationPayload(
        kind=payload["kind"],
        recipient_user_id=payload["recipient_user_id"],
        watch_id=None if payload["watch_id"] is None else UUID(payload["watch_id"]),
        snapshot_id=None if snapshot_id is None else UUID(snapshot_id),
        new_option_count=payload["new_option_count"],
        host=payload["host"],
        recovery_text=payload["recovery_text"],
    )


def _encode_rank(vector: RankVector) -> str:
    return json.dumps(
        {
            "preferred_seat_overlap": vector.preferred_seat_overlap,
            "preferred_row_match": vector.preferred_row_match,
            "view_score_band": vector.view_score_band,
            "preferred_time_distance_minutes": vector.preferred_time_distance_minutes,
            "raw_view_score": vector.raw_view_score,
            "performance_start": encode_datetime(vector.performance_start),
            "seat_label": vector.seat_label,
        },
        sort_keys=True,
    )


def _decode_rank(data: str) -> RankVector:
    payload: dict[str, Any] = json.loads(data)
    return RankVector(
        preferred_seat_overlap=payload["preferred_seat_overlap"],
        preferred_row_match=payload["preferred_row_match"],
        view_score_band=payload["view_score_band"],
        preferred_time_distance_minutes=payload["preferred_time_distance_minutes"],
        raw_view_score=payload["raw_view_score"],
        performance_start=decode_datetime(payload["performance_start"]),
        seat_label=payload["seat_label"],
    )


def _row_to_delivery(row: aiosqlite.Row) -> NotificationDelivery:
    delivered_at = row["delivered_at"]
    return NotificationDelivery(
        delivery_id=UUID(row["id"]),
        idempotency_key=row["idempotency_key"],
        payload=_decode_payload(row["payload_json"], row["snapshot_id"]),
        status=DeliveryStatus(row["status"]),
        attempt_count=row["attempt_count"],
        next_attempt_at=decode_datetime(row["next_attempt_at"]),
        created_at=decode_datetime(row["created_at"]),
        delivered_at=None if delivered_at is None else decode_datetime(delivered_at),
    )


class NotificationRepository:
    """Reads and writes everything about what a user has been told.

    Holds the :class:`Database` so its multi-statement writes can open a transaction that
    nests as a savepoint inside a caller's, rather than committing halfway through work a
    service still intends to roll back.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    async def create_delivery(
        self,
        conn: aiosqlite.Connection,
        idempotency_key: str,
        payload: NotificationPayload,
        now: datetime,
    ) -> NotificationDelivery:
        """Queue a delivery, or return the existing one for ``idempotency_key``.

        The insert and the re-read are one statement plus one read against a UNIQUE
        column, so a concurrent caller either loses the insert and reads the winner's
        row, or wins and reads its own. Neither can produce a duplicate message.
        """
        await conn.execute(
            """
            INSERT INTO notification_deliveries
                (id, idempotency_key, recipient_user_id, watch_id, snapshot_id, kind,
                 payload_json, status, attempt_count, next_attempt_at, created_at,
                 delivered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL)
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (
                str(uuid4()),
                idempotency_key,
                payload.recipient_user_id,
                None if payload.watch_id is None else str(payload.watch_id),
                None if payload.snapshot_id is None else str(payload.snapshot_id),
                payload.kind,
                _encode_payload(payload),
                DeliveryStatus.PENDING.value,
                encode_datetime(now),
                encode_datetime(now),
            ),
        )
        cursor = await conn.execute(
            "SELECT * FROM notification_deliveries WHERE idempotency_key = ?",
            (idempotency_key,),
        )
        row = await cursor.fetchone()
        if row is None:  # pragma: no cover - the insert above guarantees a row
            raise InputError(f"delivery {idempotency_key} vanished after insert")
        return _row_to_delivery(row)

    async def delivery(
        self, conn: aiosqlite.Connection, delivery_id: UUID
    ) -> NotificationDelivery | None:
        cursor = await conn.execute(
            "SELECT * FROM notification_deliveries WHERE id = ?", (str(delivery_id),)
        )
        row = await cursor.fetchone()
        return None if row is None else _row_to_delivery(row)

    async def due_deliveries(
        self, conn: aiosqlite.Connection, now: datetime
    ) -> tuple[NotificationDelivery, ...]:
        """Return pending deliveries whose next attempt is due, oldest attempt first."""
        cursor = await conn.execute(
            """
            SELECT * FROM notification_deliveries
            WHERE status = ? AND next_attempt_at <= ?
            ORDER BY next_attempt_at, created_at, id
            """,
            (DeliveryStatus.PENDING.value, encode_datetime(now)),
        )
        return tuple(_row_to_delivery(row) for row in await cursor.fetchall())

    async def reschedule(
        self, conn: aiosqlite.Connection, delivery_id: UUID, next_attempt_at: datetime
    ) -> None:
        """Count a failed attempt and put the delivery back in the queue."""
        cursor = await conn.execute(
            """
            UPDATE notification_deliveries
            SET attempt_count = attempt_count + 1, next_attempt_at = ?
            WHERE id = ? AND status = ?
            """,
            (encode_datetime(next_attempt_at), str(delivery_id), DeliveryStatus.PENDING.value),
        )
        if cursor.rowcount != 1:
            raise InputError(f"no pending delivery {delivery_id} to reschedule")

    async def mark_failed(
        self, conn: aiosqlite.Connection, delivery_id: UUID, now: datetime
    ) -> None:
        """Give up on a delivery.

        Without a terminal failure state an unsendable message stays pending forever, and
        because retention keeps any snapshot a pending delivery points at, one poisoned
        message would pin a snapshot in the database indefinitely.
        """
        cursor = await conn.execute(
            """
            UPDATE notification_deliveries
            SET status = ?, attempt_count = attempt_count + 1, next_attempt_at = ?
            WHERE id = ? AND status = ?
            """,
            (
                DeliveryStatus.FAILED.value,
                encode_datetime(now),
                str(delivery_id),
                DeliveryStatus.PENDING.value,
            ),
        )
        if cursor.rowcount != 1:
            raise InputError(f"no pending delivery {delivery_id} to fail")

    async def mark_delivered(
        self,
        conn: aiosqlite.Connection,
        delivery_id: UUID,
        option_keys: Sequence[str],
        last_best_rank: RankVector | None,
        now: datetime,
    ) -> None:
        """Record that the message went out, in one transaction with its consequences.

        Marking the delivery sent, recording its options as known, and moving the watch's
        notification flags are the same fact. Splitting them would let a crash leave
        options marked known with no message ever sent, which silently drops results.

        ``option_keys`` are inserted with ``INSERT OR IGNORE`` so a re-announced option
        keeps its original ``first_notified_at``. ``last_best_rank`` of ``None`` leaves
        the stored rank untouched rather than clearing it, because a delivery that says
        nothing about ranking -- a host alert, say -- is not evidence the ranking changed.
        """
        async with self._database.transaction(conn):
            cursor = await conn.execute(
                "SELECT * FROM notification_deliveries WHERE id = ?", (str(delivery_id),)
            )
            row = await cursor.fetchone()
            if row is None:
                raise InputError(f"unknown delivery {delivery_id}")
            watch_id = row["watch_id"]
            kind = row["kind"]

            await conn.execute(
                """
                UPDATE notification_deliveries
                SET status = ?, delivered_at = ?, attempt_count = attempt_count + 1
                WHERE id = ?
                """,
                (DeliveryStatus.SENT.value, encode_datetime(now), str(delivery_id)),
            )
            if watch_id is None:
                # A host-wide alert belongs to no watch, so there is no per-watch state
                # to move and no options to mark. Writing one would invent a watch
                # association the delivery never had.
                return
            await self._mark_options_known(conn, watch_id, option_keys, now)
            await self._update_state(conn, watch_id, kind, last_best_rank, now)

    async def known_keys(self, conn: aiosqlite.Connection, watch_id: UUID) -> frozenset[str]:
        cursor = await conn.execute(
            "SELECT option_key FROM notified_options WHERE watch_id = ?", (str(watch_id),)
        )
        return frozenset(row["option_key"] for row in await cursor.fetchall())

    async def state(self, conn: aiosqlite.Connection, watch_id: UUID) -> NotificationState:
        """Return the watch's notification state, defaulting to "nothing said yet"."""
        cursor = await conn.execute(
            "SELECT * FROM notification_state WHERE watch_id = ?", (str(watch_id),)
        )
        row = await cursor.fetchone()
        if row is None:
            return NotificationState(
                watch_id=watch_id,
                last_best_rank=None,
                degradation_notified=False,
                recovery_pending=False,
            )
        rank = row["last_best_rank_json"]
        return NotificationState(
            watch_id=watch_id,
            last_best_rank=None if rank is None else _decode_rank(rank),
            degradation_notified=bool(row["degradation_notified"]),
            recovery_pending=bool(row["recovery_pending"]),
        )

    async def _mark_options_known(
        self,
        conn: aiosqlite.Connection,
        watch_id: str,
        option_keys: Iterable[str],
        now: datetime,
    ) -> None:
        encoded = encode_datetime(now)
        for option_key in option_keys:
            await conn.execute(
                """
                INSERT OR IGNORE INTO notified_options
                    (watch_id, option_key, first_notified_at)
                VALUES (?, ?, ?)
                """,
                (watch_id, option_key, encoded),
            )

    async def _update_state(
        self,
        conn: aiosqlite.Connection,
        watch_id: str,
        kind: str,
        last_best_rank: RankVector | None,
        now: datetime,
    ) -> None:
        encoded_rank = None if last_best_rank is None else _encode_rank(last_best_rank)
        degradation = 1 if kind == _DEGRADATION_KIND else 0
        recovery = 1 if kind == _DEGRADATION_KIND else 0
        moves_flags = kind in (_DEGRADATION_KIND, _RECOVERY_KIND)
        await conn.execute(
            """
            INSERT INTO notification_state
                (watch_id, last_best_rank_json, degradation_notified, recovery_pending,
                 updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(watch_id) DO UPDATE SET
                last_best_rank_json =
                    COALESCE(excluded.last_best_rank_json, notification_state.last_best_rank_json),
                degradation_notified = CASE WHEN ? THEN excluded.degradation_notified
                                            ELSE notification_state.degradation_notified END,
                recovery_pending = CASE WHEN ? THEN excluded.recovery_pending
                                        ELSE notification_state.recovery_pending END,
                updated_at = excluded.updated_at
            """,
            (
                watch_id,
                encoded_rank,
                degradation,
                recovery,
                encode_datetime(now),
                1 if moves_flags else 0,
                1 if moves_flags else 0,
            ),
        )
