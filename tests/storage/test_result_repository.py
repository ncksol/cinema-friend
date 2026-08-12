"""Tests for cinema_friend.storage.result_repository."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Coroutine
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID

import aiosqlite
import pytest

from cinema_friend.domain.errors import InputError
from cinema_friend.domain.state import CheckOutcome, CheckTrigger
from cinema_friend.domain.watch import Watch
from cinema_friend.storage.database import Database
from cinema_friend.storage.result_repository import ResultRepository, snapshot_fingerprint
from cinema_friend.storage.watch_repository import WatchRepository
from tests.storage.conftest import (
    NOW,
    FailingConnection,
    option,
    option_keys_in,
    options,
)

MakeWatch = Callable[..., Coroutine[Any, Any, Watch]]


@pytest.fixture
def repo(database: Database) -> ResultRepository:
    return ResultRepository(database)


async def _save_snapshot(
    repo: ResultRepository,
    conn: aiosqlite.Connection,
    watch: Watch,
    *,
    ranked: tuple[Any, ...],
    checked_at: Any = NOW,
) -> Any:
    check_run_id = await repo.start_check(
        conn, watch_id=watch.watch_id, trigger=CheckTrigger.SCHEDULED, started_at=checked_at
    )
    return await repo.complete_with_snapshot(
        conn,
        check_run_id=check_run_id,
        watch_id=watch.watch_id,
        options=ranked,
        checked_at=checked_at,
        outcome=CheckOutcome.SUCCESS,
        performance_count=1,
    )


# ---------------------------------------------------------------------------
# Check runs
# ---------------------------------------------------------------------------


async def test_start_check_records_a_running_check_run(
    conn: aiosqlite.Connection, repo: ResultRepository, make_watch: MakeWatch
) -> None:
    watch = await make_watch()
    check_run_id = await repo.start_check(
        conn, watch_id=watch.watch_id, trigger=CheckTrigger.MANUAL, started_at=NOW
    )
    cursor = await conn.execute("SELECT * FROM check_runs WHERE id = ?", (str(check_run_id),))
    row = await cursor.fetchone()
    assert row is not None
    assert row["trigger"] == CheckTrigger.MANUAL.value
    assert row["outcome"] == CheckOutcome.RUNNING.value
    assert row["completed_at"] is None


async def test_fail_check_records_typed_error_and_keeps_latest_snapshot(
    conn: aiosqlite.Connection, repo: ResultRepository, make_watch: MakeWatch
) -> None:
    watch = await make_watch()
    kept = await _save_snapshot(repo, conn, watch, ranked=(option("A"),))

    check_run_id = await repo.start_check(
        conn, watch_id=watch.watch_id, trigger=CheckTrigger.SCHEDULED, started_at=NOW
    )
    await repo.fail_check(
        conn,
        check_run_id=check_run_id,
        outcome=CheckOutcome.CONTRACT_ERROR,
        error_kind="BfiContractError",
        error_message="articleContext missing",
        completed_at=NOW + timedelta(seconds=5),
    )

    cursor = await conn.execute("SELECT * FROM check_runs WHERE id = ?", (str(check_run_id),))
    row = await cursor.fetchone()
    assert row is not None
    assert row["outcome"] == CheckOutcome.CONTRACT_ERROR.value
    assert row["error_kind"] == "BfiContractError"
    assert row["error_message"] == "articleContext missing"
    assert row["completed_at"] is not None

    latest = await repo.latest_snapshot(conn, watch.watch_id)
    assert latest is not None
    assert latest.snapshot_id == kept.snapshot_id


# ---------------------------------------------------------------------------
# Atomic latest-snapshot replacement
# ---------------------------------------------------------------------------


async def test_completing_check_replaces_latest_snapshot_atomically(
    conn: aiosqlite.Connection, repo: ResultRepository, make_watch: MakeWatch
) -> None:
    watch = await make_watch()
    first = await _save_snapshot(repo, conn, watch, ranked=(option("A"),))
    second = await _save_snapshot(repo, conn, watch, ranked=(option("B"),))

    latest = await repo.latest_snapshot(conn, watch.watch_id)
    assert latest is not None
    assert latest.snapshot_id == second.snapshot_id
    assert [item.seat_label for item in latest.options] == ["B"]
    assert await option_keys_in(conn, first.snapshot_id) == ["p1:A"]


async def test_only_one_snapshot_per_watch_is_marked_latest(
    conn: aiosqlite.Connection, repo: ResultRepository, make_watch: MakeWatch
) -> None:
    watch = await make_watch()
    for label in ("A", "B", "C"):
        await _save_snapshot(repo, conn, watch, ranked=(option(label),))
    cursor = await conn.execute(
        "SELECT COUNT(*) AS total FROM result_snapshots WHERE watch_id = ? AND is_latest = 1",
        (str(watch.watch_id),),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["total"] == 1


async def test_failed_option_insert_leaves_previous_latest_snapshot_unchanged(
    conn: aiosqlite.Connection, repo: ResultRepository, make_watch: MakeWatch
) -> None:
    watch = await make_watch()
    kept = await _save_snapshot(repo, conn, watch, ranked=(option("A"),))

    check_run_id = await repo.start_check(
        conn, watch_id=watch.watch_id, trigger=CheckTrigger.SCHEDULED, started_at=NOW
    )
    failing = FailingConnection(conn, "INSERT INTO result_options", nth=2)
    with pytest.raises(aiosqlite.OperationalError):
        await repo.complete_with_snapshot(
            failing,  # type: ignore[arg-type]
            check_run_id=check_run_id,
            watch_id=watch.watch_id,
            options=(option("B"), option("C")),
            checked_at=NOW + timedelta(minutes=1),
            outcome=CheckOutcome.SUCCESS,
            performance_count=1,
        )

    latest = await repo.latest_snapshot(conn, watch.watch_id)
    assert latest is not None
    assert latest.snapshot_id == kept.snapshot_id
    assert [item.seat_label for item in latest.options] == ["A"]
    cursor = await conn.execute("SELECT COUNT(*) AS total FROM result_snapshots")
    row = await cursor.fetchone()
    assert row is not None
    assert row["total"] == 1


async def test_caller_transaction_rollback_discards_the_whole_snapshot(
    conn: aiosqlite.Connection,
    repo: ResultRepository,
    database: Database,
    make_watch: MakeWatch,
) -> None:
    """complete_with_snapshot composes inside an enclosing transaction."""
    watch = await make_watch()
    kept = await _save_snapshot(repo, conn, watch, ranked=(option("A"),))

    with pytest.raises(RuntimeError):
        async with database.transaction(conn):
            await _save_snapshot(repo, conn, watch, ranked=(option("B"),))
            raise RuntimeError("caller aborted")

    latest = await repo.latest_snapshot(conn, watch.watch_id)
    assert latest is not None
    assert latest.snapshot_id == kept.snapshot_id
    cursor = await conn.execute("SELECT COUNT(*) AS total FROM result_snapshots")
    row = await cursor.fetchone()
    assert row is not None
    assert row["total"] == 1


# ---------------------------------------------------------------------------
# Fingerprints and payload codecs
# ---------------------------------------------------------------------------


def test_fingerprint_is_sha256_over_ordered_option_keys() -> None:
    ranked = (option("A"), option("B"))
    expected = hashlib.sha256(b"p1:A\np1:B").hexdigest()
    assert snapshot_fingerprint(ranked) == expected


def test_fingerprint_depends_on_option_order() -> None:
    assert snapshot_fingerprint((option("A"), option("B"))) != snapshot_fingerprint(
        (option("B"), option("A"))
    )


def test_fingerprint_of_empty_snapshot_is_stable() -> None:
    assert snapshot_fingerprint(()) == hashlib.sha256(b"").hexdigest()


async def test_persisted_fingerprint_matches_the_computed_one(
    conn: aiosqlite.Connection, repo: ResultRepository, make_watch: MakeWatch
) -> None:
    watch = await make_watch()
    ranked = (option("A"), option("B"))
    snapshot = await _save_snapshot(repo, conn, watch, ranked=ranked)
    cursor = await conn.execute(
        "SELECT fingerprint FROM result_snapshots WHERE id = ?", (str(snapshot.snapshot_id),)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["fingerprint"] == snapshot_fingerprint(ranked)


async def test_options_round_trip_through_the_payload_codec(
    conn: aiosqlite.Connection, repo: ResultRepository, make_watch: MakeWatch
) -> None:
    watch = await make_watch()
    ranked = (
        option("L17-L18", price_pence=1500),
        option("K1-K2", price_pence=None, performance_id="p2", raw_view_score=61.25),
    )
    await _save_snapshot(repo, conn, watch, ranked=ranked)
    latest = await repo.latest_snapshot(conn, watch.watch_id)
    assert latest is not None
    assert latest.options == ranked


async def test_options_are_stored_and_returned_in_supplied_rank_order(
    conn: aiosqlite.Connection, repo: ResultRepository, make_watch: MakeWatch
) -> None:
    watch = await make_watch()
    ranked = options(4)
    await _save_snapshot(repo, conn, watch, ranked=ranked)
    latest = await repo.latest_snapshot(conn, watch.watch_id)
    assert latest is not None
    assert [item.seat_label for item in latest.options] == [item.seat_label for item in ranked]


async def test_check_run_counts_and_outcome_are_recorded_on_completion(
    conn: aiosqlite.Connection, repo: ResultRepository, make_watch: MakeWatch
) -> None:
    watch = await make_watch()
    check_run_id = await repo.start_check(
        conn, watch_id=watch.watch_id, trigger=CheckTrigger.SCHEDULED, started_at=NOW
    )
    snapshot = await repo.complete_with_snapshot(
        conn,
        check_run_id=check_run_id,
        watch_id=watch.watch_id,
        options=options(3),
        checked_at=NOW + timedelta(seconds=2),
        outcome=CheckOutcome.NEW_OPTIONS,
        performance_count=7,
    )
    cursor = await conn.execute("SELECT * FROM check_runs WHERE id = ?", (str(check_run_id),))
    row = await cursor.fetchone()
    assert row is not None
    assert row["outcome"] == CheckOutcome.NEW_OPTIONS.value
    assert row["option_count"] == 3
    assert row["performance_count"] == 7
    assert row["completed_at"] is not None
    cursor = await conn.execute(
        "SELECT check_run_id FROM result_snapshots WHERE id = ?", (str(snapshot.snapshot_id),)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["check_run_id"] == str(check_run_id)


async def test_empty_snapshot_is_valid_and_becomes_latest(
    conn: aiosqlite.Connection, repo: ResultRepository, make_watch: MakeWatch
) -> None:
    watch = await make_watch()
    await _save_snapshot(repo, conn, watch, ranked=(option("A"),))
    empty = await _save_snapshot(repo, conn, watch, ranked=())
    latest = await repo.latest_snapshot(conn, watch.watch_id)
    assert latest is not None
    assert latest.snapshot_id == empty.snapshot_id
    assert latest.options == ()


async def test_latest_snapshot_is_scoped_to_its_watch(
    conn: aiosqlite.Connection, repo: ResultRepository, make_watch: MakeWatch
) -> None:
    first = await make_watch(1)
    second = await make_watch(2)
    await _save_snapshot(repo, conn, first, ranked=(option("A"),))
    await _save_snapshot(repo, conn, second, ranked=(option("B"),))
    latest_first = await repo.latest_snapshot(conn, first.watch_id)
    latest_second = await repo.latest_snapshot(conn, second.watch_id)
    assert latest_first is not None
    assert latest_second is not None
    assert [item.seat_label for item in latest_first.options] == ["A"]
    assert [item.seat_label for item in latest_second.options] == ["B"]


async def test_latest_snapshot_is_none_when_the_watch_has_never_completed_a_check(
    conn: aiosqlite.Connection, repo: ResultRepository, make_watch: MakeWatch
) -> None:
    watch = await make_watch()
    assert await repo.latest_snapshot(conn, watch.watch_id) is None


async def test_deleting_a_watch_cascades_to_snapshots_and_options(
    conn: aiosqlite.Connection, repo: ResultRepository, make_watch: MakeWatch
) -> None:
    watch = await make_watch()
    await _save_snapshot(repo, conn, watch, ranked=(option("A"),))
    await WatchRepository().delete(conn, watch.watch_id)
    for table in ("result_snapshots", "result_options", "check_runs"):
        cursor = await conn.execute(f"SELECT COUNT(*) AS total FROM {table}")
        row = await cursor.fetchone()
        assert row is not None
        assert row["total"] == 0, table


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------


async def test_snapshot_page_returns_the_requested_slice_and_totals(
    conn: aiosqlite.Connection, repo: ResultRepository, make_watch: MakeWatch
) -> None:
    watch = await make_watch()
    snapshot = await _save_snapshot(repo, conn, watch, ranked=options(23))
    page = await repo.snapshot_page(conn, snapshot.snapshot_id, page=3)
    assert page.page == 3
    assert page.total_pages == 3
    assert page.total_options == 23
    assert page.total_performances == 1
    assert [item.seat_label for item in page.options] == ["L20", "L21", "L22"]
    assert page.checked_at == NOW


async def test_snapshot_page_counts_distinct_performances(
    conn: aiosqlite.Connection, repo: ResultRepository, make_watch: MakeWatch
) -> None:
    watch = await make_watch()
    ranked = (
        option("A", performance_id="p1"),
        option("B", performance_id="p1"),
        option("C", performance_id="p2"),
    )
    snapshot = await _save_snapshot(repo, conn, watch, ranked=ranked)
    page = await repo.snapshot_page(conn, snapshot.snapshot_id, page=1)
    assert page.total_performances == 2


async def test_snapshot_page_honours_an_explicit_page_size(
    conn: aiosqlite.Connection, repo: ResultRepository, make_watch: MakeWatch
) -> None:
    watch = await make_watch()
    snapshot = await _save_snapshot(repo, conn, watch, ranked=options(5))
    page = await repo.snapshot_page(conn, snapshot.snapshot_id, page=2, page_size=2)
    assert page.total_pages == 3
    assert [item.seat_label for item in page.options] == ["L2", "L3"]


@pytest.mark.parametrize("requested", [0, -1, 4])
async def test_snapshot_page_rejects_pages_outside_the_available_range(
    conn: aiosqlite.Connection,
    repo: ResultRepository,
    make_watch: MakeWatch,
    requested: int,
) -> None:
    watch = await make_watch()
    snapshot = await _save_snapshot(repo, conn, watch, ranked=options(23))
    with pytest.raises(InputError, match="page"):
        await repo.snapshot_page(conn, snapshot.snapshot_id, page=requested)


async def test_snapshot_page_rejects_a_non_positive_page_size(
    conn: aiosqlite.Connection, repo: ResultRepository, make_watch: MakeWatch
) -> None:
    watch = await make_watch()
    snapshot = await _save_snapshot(repo, conn, watch, ranked=options(3))
    with pytest.raises(InputError, match="page size"):
        await repo.snapshot_page(conn, snapshot.snapshot_id, page=1, page_size=0)


async def test_empty_snapshot_still_has_one_addressable_page(
    conn: aiosqlite.Connection, repo: ResultRepository, make_watch: MakeWatch
) -> None:
    watch = await make_watch()
    snapshot = await _save_snapshot(repo, conn, watch, ranked=())
    page = await repo.snapshot_page(conn, snapshot.snapshot_id, page=1)
    assert page.total_pages == 1
    assert page.total_options == 0
    assert page.options == ()


async def test_snapshot_page_rejects_an_unknown_snapshot(
    conn: aiosqlite.Connection, repo: ResultRepository
) -> None:
    missing = UUID("00000000-0000-4000-8000-000000009999")
    with pytest.raises(InputError, match="snapshot"):
        await repo.snapshot_page(conn, missing, page=1)


# ---------------------------------------------------------------------------
# Option payload shape
# ---------------------------------------------------------------------------


async def test_stored_option_round_trips_label_ids_categories_and_title(
    conn: aiosqlite.Connection, repo: ResultRepository, make_watch: MakeWatch
) -> None:
    watch = await make_watch()
    stored = option(
        "L17-L18",
        seat_ids=("1FA0A9C8-1111-4000-8000-000000000017", "1FA0A9C8-2222-4000-8000-000000000018"),
        seat_categories=("Premium", "Standard"),
        title="Dog Stars",
    )

    snapshot = await _save_snapshot(repo, conn, watch, ranked=(stored,))
    snapshot_back = await repo.latest_snapshot(conn, watch.watch_id)
    assert snapshot_back is not None
    (loaded,) = snapshot_back.options

    assert loaded == stored
    assert loaded.seat_label == "L17-L18"
    assert loaded.seat_ids == stored.seat_ids
    assert loaded.seat_categories == ("Premium", "Standard")
    assert loaded.title == "Dog Stars"
    assert loaded.rank_vector.seat_key == stored.rank_vector.seat_key
    assert await option_keys_in(conn, snapshot.snapshot_id) == [stored.key]


async def test_option_key_column_records_performance_id_and_seat_ids(
    conn: aiosqlite.Connection, repo: ResultRepository, make_watch: MakeWatch
) -> None:
    watch = await make_watch()
    stored = option("L17-L18", performance_id="p9", seat_ids=("guid-a", "guid-b"))

    snapshot = await _save_snapshot(repo, conn, watch, ranked=(stored,))

    assert await option_keys_in(conn, snapshot.snapshot_id) == ["p9:guid-a|guid-b"]


async def test_snapshot_page_carries_the_persisted_watch_title(
    conn: aiosqlite.Connection, repo: ResultRepository, make_watch: MakeWatch
) -> None:
    watch = await make_watch()
    await WatchRepository().update(conn, replace(watch, title="Dog Stars"))
    snapshot = await _save_snapshot(repo, conn, watch, ranked=())

    page = await repo.snapshot_page(conn, snapshot.snapshot_id, page=1)

    assert page.watch_title == "Dog Stars"


async def test_snapshot_page_watch_title_is_none_when_the_watch_is_unnamed(
    conn: aiosqlite.Connection, repo: ResultRepository, make_watch: MakeWatch
) -> None:
    watch = await make_watch()
    snapshot = await _save_snapshot(repo, conn, watch, ranked=())

    page = await repo.snapshot_page(conn, snapshot.snapshot_id, page=1)

    assert page.watch_title is None
