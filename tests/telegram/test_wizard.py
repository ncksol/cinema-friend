"""Tests for cinema_friend.telegram.wizard.

A wizard step is a state transition against a persisted draft, not a return value: the
draft is the only source of truth for "where a user is", so every test that matters
re-reads it from :class:`DraftRepository` (often via a freshly constructed
``WizardDeps``, to prove nothing is cached in the process) rather than trusting a
handler's return value alone.

Confirmation is the one step that must survive a duplicate delivery without creating a
second watch. Those tests drive :func:`handle_wizard_callback`'s confirm path directly,
including a hand-seeded ``CONFIRMING`` draft that simulates a crash between the
idempotency claim and the watch actually being created.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from telegram import CallbackQuery, Chat, Message, Update, User

from cinema_friend.domain.errors import InputError
from cinema_friend.domain.results import CheckResult
from cinema_friend.domain.state import CheckOutcome, CheckTrigger
from cinema_friend.services.watch_service import WatchService
from cinema_friend.storage.database import Database
from cinema_friend.storage.draft_repository import DraftRepository
from cinema_friend.storage.watch_repository import WatchRepository
from cinema_friend.telegram.wizard import (
    MAX_ROW_WIDTH,
    WizardDeps,
    WizardState,
    cancel,
    handle_wizard_callback,
    handle_wizard_text,
    parse_date_range,
    parse_interval,
    parse_seat_selectors,
    parse_time_range,
    start_new,
)
from tests.fakes import FakeClock

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
USER_ID = 42
FILM_URL = "https://whatson.bfi.org.uk/imax/Online/article/dog-stars"


class FakeCheckRunner:
    """Records every call instead of running a real check."""

    def __init__(self) -> None:
        self.calls: list[tuple[UUID, CheckTrigger]] = []

    async def check(self, watch_id: UUID, trigger: CheckTrigger) -> CheckResult:
        self.calls.append((watch_id, trigger))
        return CheckResult(
            check_run_id=uuid4(),
            watch_id=watch_id,
            trigger=trigger,
            outcome=CheckOutcome.NO_CHANGE,
            snapshot_id=None,
            performance_count=0,
            option_count=0,
            error_detail=None,
        )


async def _make_database(path: Path) -> Database:
    database = Database(path)
    async with database.connection() as conn:
        await database.migrate(conn)
    return database


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "cinema.db"


@pytest.fixture
async def database(db_path: Path) -> Database:
    return await _make_database(db_path)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def fake_checks() -> FakeCheckRunner:
    return FakeCheckRunner()


@pytest.fixture
def deps(database: Database, fake_clock: FakeClock, fake_checks: FakeCheckRunner) -> WizardDeps:
    return WizardDeps(
        database=database,
        drafts=DraftRepository(),
        watches=WatchService(database, WatchRepository(), fake_clock),
        checks=fake_checks,
        clock=fake_clock,
    )


def _text_update(text: str) -> Update:
    user = User(id=USER_ID, first_name="Test", is_bot=False)
    chat = Chat(id=USER_ID, type="private")
    message = Message(message_id=1, date=NOW, chat=chat, from_user=user, text=text)
    return Update(update_id=1, message=message)


def _callback_update(data: str) -> Update:
    user = User(id=USER_ID, first_name="Test", is_bot=False)
    callback_query = CallbackQuery(
        id="1", from_user=user, chat_instance="chat-instance", data=data
    )
    return Update(update_id=1, callback_query=callback_query)


async def _drive_to_review(deps: WizardDeps) -> None:
    """Push a fresh draft through every step up to (and including) ``REVIEW``."""
    await start_new(_text_update(""), deps)
    assert (await handle_wizard_text(_text_update(FILM_URL), deps)) is not None
    assert (await handle_wizard_text(_text_update("2026-08-26 to 2026-08-30"), deps)) is not None
    assert (await handle_wizard_text(_text_update("18:00 to 23:00"), deps)) is not None
    assert (await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)) is not None
    assert (await handle_wizard_text(_text_update("skip"), deps)) is not None  # preferred rows
    assert (await handle_wizard_text(_text_update("skip"), deps)) is not None  # preferred seats
    assert (await handle_wizard_text(_text_update("skip"), deps)) is not None  # excluded rows
    assert (await handle_wizard_text(_text_update("skip"), deps)) is not None  # excluded seats
    assert (await handle_wizard_text(_text_update("skip"), deps)) is not None  # preferred instant
    result = await handle_wizard_callback(_callback_update("wizard:mode:one_off"), deps)
    assert result is not None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def test_parse_date_range_accepts_an_ordered_pair() -> None:
    assert parse_date_range("2026-08-26 to 2026-08-30") == (
        date(2026, 8, 26),
        date(2026, 8, 30),
    )


def test_parse_date_range_rejects_reversed_dates() -> None:
    with pytest.raises(InputError):
        parse_date_range("2026-08-30 to 2026-08-26")


def test_parse_date_range_rejects_garbage() -> None:
    with pytest.raises(InputError):
        parse_date_range("not a date range")


def test_parse_time_range_accepts_a_normal_window() -> None:
    assert parse_time_range("18:00 to 23:00") == (time(18, 0), time(23, 0))


def test_parse_time_range_accepts_a_midnight_crossing_window() -> None:
    """A window like 22:00 to 01:00 is accepted, not rejected as reversed."""
    assert parse_time_range("22:00 to 01:00") == (time(22, 0), time(1, 0))


def test_parse_time_range_rejects_garbage() -> None:
    with pytest.raises(InputError):
        parse_time_range("evening")


def test_parse_seat_selectors_rows_normalizes_case() -> None:
    assert parse_seat_selectors("l, m", kind="row") == frozenset({"L", "M"})


def test_parse_seat_selectors_rejects_a_seat_number_in_row_mode() -> None:
    with pytest.raises(InputError):
        parse_seat_selectors("L16", kind="row")


def test_parse_seat_selectors_seats_normalizes_a_single_seat() -> None:
    assert parse_seat_selectors("l17", kind="seat") == frozenset({"L17"})


def test_parse_seat_selectors_seats_expands_a_same_row_range() -> None:
    assert parse_seat_selectors("L16-L22", kind="seat") == frozenset(
        {f"L{n}" for n in range(16, 23)}
    )


def test_parse_seat_selectors_seats_combines_ranges_and_singles() -> None:
    assert parse_seat_selectors("L16-L18,M5", kind="seat") == frozenset(
        {"L16", "L17", "L18", "M5"}
    )


def test_parse_seat_selectors_seats_rejects_cross_row_range() -> None:
    with pytest.raises(InputError):
        parse_seat_selectors("L16-M22", kind="seat")


def test_parse_seat_selectors_seats_rejects_a_range_wider_than_the_row() -> None:
    with pytest.raises(InputError):
        parse_seat_selectors(f"A1-A{MAX_ROW_WIDTH + 1}", kind="seat")


def test_parse_seat_selectors_seats_rejects_a_seat_number_beyond_row_width() -> None:
    with pytest.raises(InputError):
        parse_seat_selectors(f"A{MAX_ROW_WIDTH + 1}", kind="seat")


def test_parse_interval_accepts_the_minimum() -> None:
    assert parse_interval("15") == timedelta(minutes=15)


def test_parse_interval_rejects_below_the_minimum() -> None:
    with pytest.raises(InputError):
        parse_interval("14")


def test_parse_interval_rejects_non_numeric() -> None:
    with pytest.raises(InputError):
        parse_interval("half an hour")


# ---------------------------------------------------------------------------
# start_new / cancel
# ---------------------------------------------------------------------------


async def test_start_new_creates_a_draft_awaiting_the_url(deps: WizardDeps) -> None:
    await start_new(_text_update("/new"), deps)

    async with deps.database.connection() as conn:
        draft = await deps.drafts.get(conn, USER_ID)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_URL.value
    assert draft.payload == {}


async def test_start_new_replaces_an_abandoned_draft(deps: WizardDeps) -> None:
    await start_new(_text_update("/new"), deps)
    await handle_wizard_text(_text_update(FILM_URL), deps)

    await start_new(_text_update("/new"), deps)

    async with deps.database.connection() as conn:
        draft = await deps.drafts.get(conn, USER_ID)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_URL.value
    assert draft.payload == {}


async def test_cancel_deletes_the_draft(deps: WizardDeps) -> None:
    await start_new(_text_update("/new"), deps)

    await cancel(_text_update("/cancel"), deps)

    async with deps.database.connection() as conn:
        assert await deps.drafts.get(conn, USER_ID) is None


async def test_cancel_on_a_missing_draft_is_a_no_op(deps: WizardDeps) -> None:
    result = await cancel(_text_update("/cancel"), deps)

    assert "cancelled" in result.text.lower()


# ---------------------------------------------------------------------------
# Handlers ignore updates that are not theirs
# ---------------------------------------------------------------------------


async def test_handle_wizard_text_returns_none_without_a_draft(deps: WizardDeps) -> None:
    assert await handle_wizard_text(_text_update("hello"), deps) is None


async def test_handle_wizard_callback_returns_none_without_a_draft(deps: WizardDeps) -> None:
    assert await handle_wizard_callback(_callback_update("wizard:qty:1"), deps) is None


async def test_handle_wizard_text_returns_none_when_draft_awaits_a_callback(
    deps: WizardDeps,
) -> None:
    await start_new(_text_update("/new"), deps)
    await handle_wizard_text(_text_update(FILM_URL), deps)
    await handle_wizard_text(_text_update("2026-08-26 to 2026-08-30"), deps)
    await handle_wizard_text(_text_update("18:00 to 23:00"), deps)

    assert await handle_wizard_text(_text_update("2"), deps) is None


async def test_handle_wizard_callback_returns_none_when_draft_awaits_text(
    deps: WizardDeps,
) -> None:
    await start_new(_text_update("/new"), deps)

    assert await handle_wizard_callback(_callback_update("wizard:qty:2"), deps) is None


# ---------------------------------------------------------------------------
# Invalid input leaves the draft unchanged
# ---------------------------------------------------------------------------


async def test_invalid_url_leaves_draft_unchanged(deps: WizardDeps) -> None:
    await start_new(_text_update("/new"), deps)

    reply = await handle_wizard_text(_text_update("not a url"), deps)

    assert reply is not None
    async with deps.database.connection() as conn:
        draft = await deps.drafts.get(conn, USER_ID)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_URL.value
    assert draft.payload == {}


async def test_invalid_quantity_leaves_draft_unchanged(deps: WizardDeps) -> None:
    await start_new(_text_update("/new"), deps)
    await handle_wizard_text(_text_update(FILM_URL), deps)
    await handle_wizard_text(_text_update("2026-08-26 to 2026-08-30"), deps)
    await handle_wizard_text(_text_update("18:00 to 23:00"), deps)

    reply = await handle_wizard_callback(_callback_update("wizard:qty:9"), deps)

    assert reply is not None
    async with deps.database.connection() as conn:
        draft = await deps.drafts.get(conn, USER_ID)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_QUANTITY.value
    assert "quantity" not in draft.payload


async def test_quantity_zero_is_rejected(deps: WizardDeps) -> None:
    await start_new(_text_update("/new"), deps)
    await handle_wizard_text(_text_update(FILM_URL), deps)
    await handle_wizard_text(_text_update("2026-08-26 to 2026-08-30"), deps)
    await handle_wizard_text(_text_update("18:00 to 23:00"), deps)

    reply = await handle_wizard_callback(_callback_update("wizard:qty:0"), deps)

    assert reply is not None
    async with deps.database.connection() as conn:
        draft = await deps.drafts.get(conn, USER_ID)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_QUANTITY.value


async def test_quantity_eight_is_accepted(deps: WizardDeps) -> None:
    await start_new(_text_update("/new"), deps)
    await handle_wizard_text(_text_update(FILM_URL), deps)
    await handle_wizard_text(_text_update("2026-08-26 to 2026-08-30"), deps)
    await handle_wizard_text(_text_update("18:00 to 23:00"), deps)

    reply = await handle_wizard_callback(_callback_update("wizard:qty:8"), deps)

    assert reply is not None
    async with deps.database.connection() as conn:
        draft = await deps.drafts.get(conn, USER_ID)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_PREFERRED_ROWS.value
    assert draft.payload["quantity"] == 8


async def test_invalid_preferred_instant_leaves_draft_unchanged(deps: WizardDeps) -> None:
    await start_new(_text_update("/new"), deps)
    await handle_wizard_text(_text_update(FILM_URL), deps)
    await handle_wizard_text(_text_update("2026-08-26 to 2026-08-30"), deps)
    await handle_wizard_text(_text_update("18:00 to 23:00"), deps)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)
    await handle_wizard_text(_text_update("skip"), deps)
    await handle_wizard_text(_text_update("skip"), deps)
    await handle_wizard_text(_text_update("skip"), deps)
    await handle_wizard_text(_text_update("skip"), deps)

    # Outside the 18:00-23:00 window.
    reply = await handle_wizard_text(_text_update("2026-08-27 12:00"), deps)

    assert reply is not None
    async with deps.database.connection() as conn:
        draft = await deps.drafts.get(conn, USER_ID)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_PREFERRED_INSTANT.value


async def test_preferred_instant_within_the_window_is_accepted(deps: WizardDeps) -> None:
    await start_new(_text_update("/new"), deps)
    await handle_wizard_text(_text_update(FILM_URL), deps)
    await handle_wizard_text(_text_update("2026-08-26 to 2026-08-30"), deps)
    await handle_wizard_text(_text_update("18:00 to 23:00"), deps)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)
    await handle_wizard_text(_text_update("skip"), deps)
    await handle_wizard_text(_text_update("skip"), deps)
    await handle_wizard_text(_text_update("skip"), deps)
    await handle_wizard_text(_text_update("skip"), deps)

    reply = await handle_wizard_text(_text_update("2026-08-27 20:00"), deps)

    assert reply is not None
    async with deps.database.connection() as conn:
        draft = await deps.drafts.get(conn, USER_ID)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_MODE.value
    assert draft.payload["preferred_utc_instant"] == "2026-08-27T20:00:00+00:00"


async def test_recurring_mode_asks_for_an_interval_before_review(deps: WizardDeps) -> None:
    await _drive_recurring_to_interval(deps)

    async with deps.database.connection() as conn:
        draft = await deps.drafts.get(conn, USER_ID)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_INTERVAL.value


async def _drive_recurring_to_interval(deps: WizardDeps) -> None:
    await start_new(_text_update("/new"), deps)
    await handle_wizard_text(_text_update(FILM_URL), deps)
    await handle_wizard_text(_text_update("2026-08-26 to 2026-08-30"), deps)
    await handle_wizard_text(_text_update("18:00 to 23:00"), deps)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)
    await handle_wizard_text(_text_update("skip"), deps)
    await handle_wizard_text(_text_update("skip"), deps)
    await handle_wizard_text(_text_update("skip"), deps)
    await handle_wizard_text(_text_update("skip"), deps)
    await handle_wizard_text(_text_update("skip"), deps)
    await handle_wizard_callback(_callback_update("wizard:mode:recurring"), deps)


async def test_short_interval_leaves_draft_at_await_interval(deps: WizardDeps) -> None:
    await _drive_recurring_to_interval(deps)

    reply = await handle_wizard_text(_text_update("5"), deps)

    assert reply is not None
    async with deps.database.connection() as conn:
        draft = await deps.drafts.get(conn, USER_ID)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_INTERVAL.value


async def test_recurring_interval_reaches_review(deps: WizardDeps) -> None:
    await _drive_recurring_to_interval(deps)

    reply = await handle_wizard_text(_text_update("30"), deps)

    assert reply is not None
    async with deps.database.connection() as conn:
        draft = await deps.drafts.get(conn, USER_ID)
    assert draft is not None
    assert draft.state == WizardState.REVIEW.value
    assert draft.payload["interval_minutes"] == 30


# ---------------------------------------------------------------------------
# Persisted resume across a restart
# ---------------------------------------------------------------------------


async def test_wizard_resumes_from_a_freshly_constructed_deps_pointed_at_the_same_file(
    db_path: Path, fake_clock: FakeClock, fake_checks: FakeCheckRunner
) -> None:
    """Nothing about the wizard's progress may live only in process memory.

    A brand-new ``Database``/``WizardDeps`` pair, opened after the first one is done
    being used, must see exactly the same draft the first pair left behind -- this is
    what "restart between messages loses nothing" actually means for a SQLite-backed
    draft.
    """
    database_one = await _make_database(db_path)
    deps_one = WizardDeps(
        database=database_one,
        drafts=DraftRepository(),
        watches=WatchService(database_one, WatchRepository(), fake_clock),
        checks=fake_checks,
        clock=fake_clock,
    )
    await start_new(_text_update("/new"), deps_one)
    await handle_wizard_text(_text_update(FILM_URL), deps_one)
    await handle_wizard_text(_text_update("2026-08-26 to 2026-08-30"), deps_one)

    # Simulate a process restart: a brand new Database/WizardDeps over the same file.
    database_two = Database(db_path)
    deps_two = WizardDeps(
        database=database_two,
        drafts=DraftRepository(),
        watches=WatchService(database_two, WatchRepository(), fake_clock),
        checks=fake_checks,
        clock=fake_clock,
    )
    async with database_two.connection() as conn:
        draft = await deps_two.drafts.get(conn, USER_ID)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_TIME_RANGE.value
    assert draft.payload["slug"] == "dog-stars"
    assert draft.payload["date_from"] == "2026-08-26"

    reply = await handle_wizard_text(_text_update("18:00 to 23:00"), deps_two)
    assert reply is not None
    async with database_two.connection() as conn:
        draft = await deps_two.drafts.get(conn, USER_ID)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_QUANTITY.value


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


async def test_confirm_creates_exactly_one_watch_and_runs_one_creation_check(
    deps: WizardDeps, fake_checks: FakeCheckRunner
) -> None:
    await _drive_to_review(deps)

    reply = await handle_wizard_callback(_callback_update("wizard:confirm"), deps)

    assert reply is not None
    assert "created" in reply.text.lower()
    watches = await deps.watches.list_for_owner(USER_ID)
    assert len(watches) == 1
    assert watches[0].criteria.slug == "dog-stars"
    assert fake_checks.calls == [(watches[0].watch_id, CheckTrigger.CREATION)]
    async with deps.database.connection() as conn:
        assert await deps.drafts.get(conn, USER_ID) is None


async def test_confirm_cancel_deletes_the_draft_without_creating_a_watch(
    deps: WizardDeps, fake_checks: FakeCheckRunner
) -> None:
    await _drive_to_review(deps)

    reply = await handle_wizard_callback(_callback_update("wizard:cancel"), deps)

    assert reply is not None
    assert not await deps.watches.list_for_owner(USER_ID)
    assert fake_checks.calls == []
    async with deps.database.connection() as conn:
        assert await deps.drafts.get(conn, USER_ID) is None


async def test_a_second_confirm_after_success_is_a_no_op(
    deps: WizardDeps, fake_checks: FakeCheckRunner
) -> None:
    """A duplicate Telegram delivery of the confirm button, arriving after the first
    one already completed, must not create a second watch -- there is no draft left
    for it to act on.
    """
    await _drive_to_review(deps)
    await handle_wizard_callback(_callback_update("wizard:confirm"), deps)

    reply = await handle_wizard_callback(_callback_update("wizard:confirm"), deps)

    assert reply is None
    assert len(await deps.watches.list_for_owner(USER_ID)) == 1
    assert len(fake_checks.calls) == 1


async def test_concurrent_duplicate_confirm_creates_only_one_watch(
    deps: WizardDeps, fake_checks: FakeCheckRunner
) -> None:
    """Two callbacks racing for the same reviewed draft must not double-create.

    SQLite's ``BEGIN IMMEDIATE`` claim serializes the two attempts: whichever task wins
    the claim proceeds, and the other observes the draft is no longer ``REVIEW`` once
    it gets its turn and safely no-ops.
    """
    await _drive_to_review(deps)

    results = await asyncio.gather(
        handle_wizard_callback(_callback_update("wizard:confirm"), deps),
        handle_wizard_callback(_callback_update("wizard:confirm"), deps),
    )

    watches = await deps.watches.list_for_owner(USER_ID)
    assert len(watches) == 1
    assert len(fake_checks.calls) == 1
    # Exactly one of the two callers should have actually created the watch.
    created_replies = [r for r in results if r is not None and "watch created" in r.text.lower()]
    assert len(created_replies) == 1


async def test_confirm_after_a_crash_between_claim_and_completion_does_not_duplicate(
    deps: WizardDeps, fake_checks: FakeCheckRunner
) -> None:
    """Simulates a process crash strictly between the idempotency claim committing and
    the watch actually being created: the draft is stuck at ``CONFIRMING`` (the claim
    succeeded) but no watch exists yet. A retried confirm must not create a watch out
    from under that stuck state -- it is a documented limitation that the user must
    ``/cancel`` and start over, not that a retry silently double-creates.
    """
    await _drive_to_review(deps)
    async with deps.database.connection() as conn:
        draft = await deps.drafts.get(conn, USER_ID)
    assert draft is not None
    async with deps.database.connection() as conn:
        await deps.drafts.upsert(
            conn, USER_ID, WizardState.CONFIRMING.value, draft.payload, deps.clock.now()
        )

    reply = await handle_wizard_callback(_callback_update("wizard:confirm"), deps)

    assert reply is not None
    assert "already" in reply.text.lower()
    assert not await deps.watches.list_for_owner(USER_ID)
    assert fake_checks.calls == []


async def test_confirm_requires_the_draft_to_be_in_review(deps: WizardDeps) -> None:
    await start_new(_text_update("/new"), deps)

    reply = await handle_wizard_callback(_callback_update("wizard:confirm"), deps)

    assert reply is None
