"""Persists check runs, result snapshots, and their ranked options.

Every method accepts an existing connection so a service can wrap calls across several
repositories in one :meth:`Database.transaction`. The multi-statement writes here open
their own transaction, which nests as a savepoint when a caller already has one open.

Requires SQLite 3.38 or newer for the built-in ``json_extract`` used to count the
distinct performances behind a snapshot without re-decoding every stored option.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import aiosqlite

from cinema_friend.domain.bfi import Performance
from cinema_friend.domain.errors import InputError
from cinema_friend.domain.results import RankedOption, RankVector, ResultSnapshot, SnapshotPage
from cinema_friend.domain.state import CheckOutcome, CheckTrigger
from cinema_friend.storage.database import Database, decode_datetime, encode_datetime

DEFAULT_PAGE_SIZE = 10


def snapshot_fingerprint(options: Sequence[RankedOption]) -> str:
    """Return the SHA-256 of the snapshot's option keys in rank order.

    Order is part of the identity: the same option set in a different ranking is a
    different result to a user, so the digest must change with it.
    """
    joined = "\n".join(option.key for option in options)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _encode_option(option: RankedOption) -> str:
    performance = option.performance
    vector = option.rank_vector
    payload = {
        "performance": {
            "performance_id": performance.performance_id,
            "event_id": performance.event_id,
            "start_utc": encode_datetime(performance.start_utc),
            "sales_status_code": performance.sales_status_code,
            "availability_code": performance.availability_code,
            "availability_num": performance.availability_num,
            "reserved_seating": performance.reserved_seating,
            "seat_map_url": performance.seat_map_url,
            "options": list(performance.options),
        },
        "seat_label": option.seat_label,
        "seat_ids": list(option.seat_ids),
        "seat_categories": list(option.seat_categories),
        "title": option.title,
        "rank_vector": {
            "preferred_seat_overlap": vector.preferred_seat_overlap,
            "preferred_row_match": vector.preferred_row_match,
            "view_score_band": vector.view_score_band,
            "preferred_time_distance_minutes": vector.preferred_time_distance_minutes,
            "raw_view_score": vector.raw_view_score,
            "performance_start": encode_datetime(vector.performance_start),
            "seat_key": vector.seat_key,
        },
        "price_pence": option.price_pence,
    }
    return json.dumps(payload, sort_keys=True)


def _decode_option(data: str) -> RankedOption:
    payload: dict[str, Any] = json.loads(data)
    performance = payload["performance"]
    vector = payload["rank_vector"]
    return RankedOption(
        performance=Performance(
            performance_id=performance["performance_id"],
            event_id=performance["event_id"],
            start_utc=decode_datetime(performance["start_utc"]),
            sales_status_code=performance["sales_status_code"],
            availability_code=performance["availability_code"],
            availability_num=performance["availability_num"],
            reserved_seating=performance["reserved_seating"],
            seat_map_url=performance["seat_map_url"],
            options=tuple(performance["options"]),
        ),
        seat_label=payload["seat_label"],
        seat_ids=tuple(payload["seat_ids"]),
        rank_vector=RankVector(
            preferred_seat_overlap=vector["preferred_seat_overlap"],
            preferred_row_match=vector["preferred_row_match"],
            view_score_band=vector["view_score_band"],
            preferred_time_distance_minutes=vector["preferred_time_distance_minutes"],
            raw_view_score=vector["raw_view_score"],
            performance_start=decode_datetime(vector["performance_start"]),
            seat_key=vector["seat_key"],
        ),
        price_pence=payload["price_pence"],
        seat_categories=tuple(payload["seat_categories"]),
        title=payload["title"],
    )


class ResultRepository:
    """Check-run lifecycle and immutable result snapshots."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def start_check(
        self,
        conn: aiosqlite.Connection,
        *,
        watch_id: UUID,
        trigger: CheckTrigger,
        started_at: datetime,
    ) -> UUID:
        """Record an in-flight check run and return its id."""
        check_run_id = uuid4()
        await conn.execute(
            """
            INSERT INTO check_runs (id, watch_id, trigger, outcome, started_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(check_run_id),
                str(watch_id),
                trigger.value,
                CheckOutcome.RUNNING.value,
                encode_datetime(started_at),
            ),
        )
        return check_run_id

    async def complete_with_snapshot(
        self,
        conn: aiosqlite.Connection,
        *,
        check_run_id: UUID,
        watch_id: UUID,
        options: Sequence[RankedOption],
        checked_at: datetime,
        outcome: CheckOutcome,
        performance_count: int,
    ) -> ResultSnapshot:
        """Store a new snapshot and make it the watch's latest, atomically.

        The new row is inserted with ``is_latest = 0`` and promoted last. Demoting the
        previous latest first and promoting only after every option is written means the
        partial unique index is never violated part-way through, and a failure anywhere
        in the unit rolls back to the previous latest snapshot rather than leaving a
        watch with no current result or a half-populated one.
        """
        ranked = tuple(options)
        snapshot_id = uuid4()
        async with self._database.transaction(conn):
            await conn.execute(
                "UPDATE result_snapshots SET is_latest = 0 WHERE watch_id = ? AND is_latest = 1",
                (str(watch_id),),
            )
            await conn.execute(
                """
                INSERT INTO result_snapshots
                    (id, watch_id, check_run_id, checked_at, fingerprint, is_latest)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (
                    str(snapshot_id),
                    str(watch_id),
                    str(check_run_id),
                    encode_datetime(checked_at),
                    snapshot_fingerprint(ranked),
                ),
            )
            for rank, option in enumerate(ranked):
                await conn.execute(
                    """
                    INSERT INTO result_options (snapshot_id, rank, option_key, payload_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (str(snapshot_id), rank, option.key, _encode_option(option)),
                )
            await conn.execute(
                """
                UPDATE check_runs
                SET outcome = ?, completed_at = ?, performance_count = ?, option_count = ?,
                    error_kind = NULL, error_message = NULL
                WHERE id = ?
                """,
                (
                    outcome.value,
                    encode_datetime(checked_at),
                    performance_count,
                    len(ranked),
                    str(check_run_id),
                ),
            )
            await conn.execute(
                "UPDATE result_snapshots SET is_latest = 1 WHERE id = ?", (str(snapshot_id),)
            )
        return ResultSnapshot(
            snapshot_id=snapshot_id,
            watch_id=watch_id,
            checked_at=checked_at,
            options=ranked,
        )

    async def fail_check(
        self,
        conn: aiosqlite.Connection,
        *,
        check_run_id: UUID,
        outcome: CheckOutcome,
        error_kind: str,
        error_message: str,
        completed_at: datetime,
    ) -> None:
        """Close a check run with a typed failure, leaving the latest snapshot intact."""
        await conn.execute(
            """
            UPDATE check_runs
            SET outcome = ?, completed_at = ?, error_kind = ?, error_message = ?
            WHERE id = ?
            """,
            (
                outcome.value,
                encode_datetime(completed_at),
                error_kind,
                error_message,
                str(check_run_id),
            ),
        )

    async def latest_snapshot(
        self, conn: aiosqlite.Connection, watch_id: UUID
    ) -> ResultSnapshot | None:
        cursor = await conn.execute(
            "SELECT id, watch_id, checked_at FROM result_snapshots "
            "WHERE watch_id = ? AND is_latest = 1",
            (str(watch_id),),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        snapshot_id = UUID(row["id"])
        return ResultSnapshot(
            snapshot_id=snapshot_id,
            watch_id=UUID(row["watch_id"]),
            checked_at=decode_datetime(row["checked_at"]),
            options=await self._options(conn, snapshot_id),
        )

    async def snapshot_page(
        self,
        conn: aiosqlite.Connection,
        snapshot_id: UUID,
        page: int,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> SnapshotPage:
        """Return one page of an immutable snapshot plus its totals.

        An empty snapshot still has one addressable page so a "no matches" result stays
        browsable instead of rejecting every page a callback could ask for.
        """
        if page_size < 1:
            raise InputError("page size must be at least 1")
        cursor = await conn.execute(
            """
            SELECT result_snapshots.checked_at AS checked_at, watches.title AS watch_title
            FROM result_snapshots
            LEFT JOIN watches ON watches.id = result_snapshots.watch_id
            WHERE result_snapshots.id = ?
            """,
            (str(snapshot_id),),
        )
        row = await cursor.fetchone()
        if row is None:
            raise InputError("snapshot not found")
        checked_at = decode_datetime(row["checked_at"])
        watch_title = row["watch_title"]

        cursor = await conn.execute(
            """
            SELECT COUNT(*) AS total_options,
                   COUNT(DISTINCT json_extract(payload_json, '$.performance.performance_id'))
                       AS total_performances
            FROM result_options WHERE snapshot_id = ?
            """,
            (str(snapshot_id),),
        )
        totals = await cursor.fetchone()
        assert totals is not None
        total_options = totals["total_options"]
        total_pages = max(1, -(-total_options // page_size))
        if not 1 <= page <= total_pages:
            raise InputError(f"page must be between 1 and {total_pages}")

        cursor = await conn.execute(
            """
            SELECT payload_json FROM result_options
            WHERE snapshot_id = ? ORDER BY rank LIMIT ? OFFSET ?
            """,
            (str(snapshot_id), page_size, (page - 1) * page_size),
        )
        rows = await cursor.fetchall()
        return SnapshotPage(
            snapshot_id=snapshot_id,
            checked_at=checked_at,
            options=tuple(_decode_option(item["payload_json"]) for item in rows),
            page=page,
            total_pages=total_pages,
            total_options=total_options,
            total_performances=totals["total_performances"],
            watch_title=watch_title,
        )

    async def _options(
        self, conn: aiosqlite.Connection, snapshot_id: UUID
    ) -> tuple[RankedOption, ...]:
        cursor = await conn.execute(
            "SELECT payload_json FROM result_options WHERE snapshot_id = ? ORDER BY rank",
            (str(snapshot_id),),
        )
        rows = await cursor.fetchall()
        return tuple(_decode_option(row["payload_json"]) for row in rows)
