"""Tests for cinema_friend.storage.draft_repository."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from cinema_friend.storage.database import Database
from cinema_friend.storage.draft_repository import ConversationDraft, DraftRepository

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
async def conn(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    database = Database(tmp_path / "drafts.db")
    connection = await database.connect()
    await database.migrate(connection)
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture
def repo() -> DraftRepository:
    return DraftRepository()


async def test_get_missing_draft_returns_none(
    conn: aiosqlite.Connection, repo: DraftRepository
) -> None:
    assert await repo.get(conn, 11) is None


async def test_upsert_then_get_round_trip(
    conn: aiosqlite.Connection, repo: DraftRepository
) -> None:
    payload = {"source_url": "https://whatson.bfi.org.uk/imax/Online/article/dog-stars"}
    await repo.upsert(conn, 11, "await_date_range", payload, _NOW)

    fetched = await repo.get(conn, 11)

    assert fetched == ConversationDraft(
        user_id=11, state="await_date_range", payload=payload, updated_at=_NOW
    )


async def test_upsert_replaces_existing_draft_for_same_user(
    conn: aiosqlite.Connection, repo: DraftRepository
) -> None:
    await repo.upsert(conn, 11, "await_url", {}, _NOW)
    later = _NOW + timedelta(minutes=5)
    await repo.upsert(conn, 11, "await_quantity", {"quantity": 2}, later)

    fetched = await repo.get(conn, 11)

    assert fetched == ConversationDraft(
        user_id=11, state="await_quantity", payload={"quantity": 2}, updated_at=later
    )


async def test_upsert_is_scoped_per_user(conn: aiosqlite.Connection, repo: DraftRepository) -> None:
    await repo.upsert(conn, 11, "await_url", {"a": 1}, _NOW)
    await repo.upsert(conn, 22, "await_quantity", {"b": 2}, _NOW)

    assert (await repo.get(conn, 11)).state == "await_url"  # type: ignore[union-attr]
    assert (await repo.get(conn, 22)).state == "await_quantity"  # type: ignore[union-attr]


async def test_delete_removes_draft(conn: aiosqlite.Connection, repo: DraftRepository) -> None:
    await repo.upsert(conn, 11, "await_url", {}, _NOW)

    await repo.delete(conn, 11)

    assert await repo.get(conn, 11) is None


async def test_delete_missing_draft_is_a_no_op(
    conn: aiosqlite.Connection, repo: DraftRepository
) -> None:
    await repo.delete(conn, 11)  # must not raise


async def test_list_by_state_returns_only_matching_drafts_in_user_order(
    conn: aiosqlite.Connection, repo: DraftRepository
) -> None:
    """Startup recovery needs to find every draft stuck mid-confirmation, by state."""
    await repo.upsert(conn, 22, "confirming", {"setup_id": "b"}, _NOW)
    await repo.upsert(conn, 11, "confirming", {"setup_id": "a"}, _NOW)
    await repo.upsert(conn, 33, "review", {}, _NOW)

    stuck = await repo.list_by_state(conn, "confirming")

    assert [draft.user_id for draft in stuck] == [11, 22]
    assert stuck[0].payload == {"setup_id": "a"}


async def test_list_by_state_returns_empty_when_nothing_matches(
    conn: aiosqlite.Connection, repo: DraftRepository
) -> None:
    await repo.upsert(conn, 11, "review", {}, _NOW)

    assert await repo.list_by_state(conn, "confirming") == ()


async def test_delete_expired_removes_drafts_older_than_24_hours(
    conn: aiosqlite.Connection, repo: DraftRepository
) -> None:
    stale_updated_at = _NOW - timedelta(hours=24, minutes=1)
    await repo.upsert(conn, 11, "await_url", {}, stale_updated_at)

    removed = await repo.delete_expired(conn, _NOW)

    assert removed == 1
    assert await repo.get(conn, 11) is None


async def test_delete_expired_keeps_drafts_within_24_hours(
    conn: aiosqlite.Connection, repo: DraftRepository
) -> None:
    fresh_updated_at = _NOW - timedelta(hours=23, minutes=59)
    await repo.upsert(conn, 11, "await_url", {}, fresh_updated_at)

    removed = await repo.delete_expired(conn, _NOW)

    assert removed == 0
    assert await repo.get(conn, 11) is not None


async def test_delete_expired_keeps_draft_exactly_at_boundary(
    conn: aiosqlite.Connection, repo: DraftRepository
) -> None:
    boundary_updated_at = _NOW - timedelta(hours=24)
    await repo.upsert(conn, 11, "await_url", {}, boundary_updated_at)

    removed = await repo.delete_expired(conn, _NOW)

    assert removed == 0
    assert await repo.get(conn, 11) is not None
