"""Persists Telegram wizard drafts; every method accepts an existing connection.

One draft belongs to one user, not one watch, and holds the wizard's current step name
and the payload validated so far so an interrupted process can resume it after restart.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import aiosqlite

from cinema_friend.storage.database import decode_datetime, encode_datetime

_DEFAULT_TTL: timedelta = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class ConversationDraft:
    """One user's in-progress wizard state and validated payload so far."""

    user_id: int
    state: str
    payload: Mapping[str, Any]
    updated_at: datetime


def _row_to_draft(row: aiosqlite.Row) -> ConversationDraft:
    return ConversationDraft(
        user_id=row["user_id"],
        state=row["state"],
        payload=json.loads(row["payload_json"]),
        updated_at=decode_datetime(row["updated_at"]),
    )


class DraftRepository:
    """CRUD and expiry over the ``conversation_drafts`` table."""

    async def get(self, conn: aiosqlite.Connection, user_id: int) -> ConversationDraft | None:
        cursor = await conn.execute(
            "SELECT user_id, state, payload_json, updated_at FROM conversation_drafts "
            "WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return _row_to_draft(row) if row is not None else None

    async def list_by_state(
        self, conn: aiosqlite.Connection, state: str
    ) -> tuple[ConversationDraft, ...]:
        """Every draft currently in ``state``, oldest user first.

        Startup recovery needs to find drafts left mid-flight by a process that died,
        which is the one case where no user id is known up front.
        """
        cursor = await conn.execute(
            "SELECT user_id, state, payload_json, updated_at FROM conversation_drafts "
            "WHERE state = ? ORDER BY user_id",
            (state,),
        )
        rows = await cursor.fetchall()
        return tuple(_row_to_draft(row) for row in rows)

    async def upsert(
        self,
        conn: aiosqlite.Connection,
        user_id: int,
        state: str,
        payload: Mapping[str, Any],
        updated_at: datetime,
    ) -> None:
        """Insert or fully replace one user's draft with the given state/payload/timestamp."""
        await conn.execute(
            """
            INSERT INTO conversation_drafts (user_id, state, payload_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                state = excluded.state,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                state,
                json.dumps(dict(payload), sort_keys=True),
                encode_datetime(updated_at),
            ),
        )

    async def delete(self, conn: aiosqlite.Connection, user_id: int) -> None:
        await conn.execute("DELETE FROM conversation_drafts WHERE user_id = ?", (user_id,))

    async def delete_expired(
        self,
        conn: aiosqlite.Connection,
        now: datetime,
        *,
        ttl: timedelta = _DEFAULT_TTL,
    ) -> int:
        """Delete drafts last updated more than ``ttl`` before ``now``; return rows removed."""
        cutoff = encode_datetime(now - ttl)
        cursor = await conn.execute(
            "DELETE FROM conversation_drafts WHERE updated_at < ?", (cutoff,)
        )
        return cursor.rowcount
