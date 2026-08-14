"""Tests for cinema_friend.telegram.wizard.

A wizard step is a state transition against a persisted draft, not a return value: the
draft is the only source of truth for "where a user is", so every test that matters
re-reads it from :class:`DraftRepository` (often via a freshly constructed
``WizardDeps``, to prove nothing is cached in the process) rather than trusting a
handler's return value alone.

Confirmation is a saga, and its tests are barrier tests: a :class:`_Crash` is raised at
each boundary (before the watch exists, after it exists, after the creation check, and
before the draft is deleted) precisely because it is a ``BaseException`` the saga's own
compensation never catches, so the database is left in exactly the durable state a
killed process would leave. Recovery then runs against that state and must converge on
one watch, one creation check, and no draft.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from telegram import CallbackQuery, Chat, Message, Update, User

from cinema_friend.domain.errors import BfiNetworkError, InputError, PersistenceError
from cinema_friend.domain.results import CheckResult
from cinema_friend.domain.state import (
    CheckOutcome,
    CheckTrigger,
    SeatPreferenceStrategy,
)
from cinema_friend.domain.watch import Watch, WatchCriteria
from cinema_friend.services.watch_service import WatchService
from cinema_friend.storage.database import Database
from cinema_friend.storage.draft_repository import DraftRepository
from cinema_friend.storage.watch_repository import WatchRepository
from cinema_friend.telegram.rendering import RenderedMessage
from cinema_friend.telegram.wizard import (
    CLAIM_LEASE,
    MAX_ROW_WIDTH,
    ConfirmPhase,
    WizardDeps,
    WizardState,
    cancel,
    handle_wizard_callback,
    handle_wizard_text,
    parse_date_range,
    parse_interval,
    parse_seat_selectors,
    parse_time_range,
    prepare_confirmation_watch_id,
    recover_confirmations,
    start_new,
)
from tests.fakes import FakeClock

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
USER_ID = 42
FILM_URL = "https://whatson.bfi.org.uk/imax/Online/article/dog-stars"
LONDON = ZoneInfo("Europe/London")


class _Crash(BaseException):
    """A simulated process death.

    Deliberately not an :class:`Exception`: the saga compensates for exceptions, and a
    crash is precisely the case where no compensation gets to run.
    """


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


class CrashAfterCheckRunner(FakeCheckRunner):
    """The check runs and is recorded, then the process dies before it is marked done."""

    async def check(self, watch_id: UUID, trigger: CheckTrigger) -> CheckResult:
        await super().check(watch_id, trigger)
        raise _Crash


class FailingCheckRunner(FakeCheckRunner):
    """A routine, recoverable check failure after the watch already exists."""

    async def check(self, watch_id: UUID, trigger: CheckTrigger) -> CheckResult:
        self.calls.append((watch_id, trigger))
        raise BfiNetworkError("host unreachable")


class CrashBeforeCreate(WatchService):
    async def create(
        self, owner_user_id: int, criteria: WatchCriteria, *, watch_id: UUID | None = None
    ) -> Watch:
        raise _Crash


class CrashAfterCreate(WatchService):
    async def create(
        self, owner_user_id: int, criteria: WatchCriteria, *, watch_id: UUID | None = None
    ) -> Watch:
        await super().create(owner_user_id, criteria, watch_id=watch_id)
        raise _Crash


class FailingCreate(WatchService):
    async def create(
        self, owner_user_id: int, criteria: WatchCriteria, *, watch_id: UUID | None = None
    ) -> Watch:
        raise PersistenceError("database unavailable")


class CrashOnDraftDelete(DraftRepository):
    """Dies at the last step, after the watch exists and the check has completed."""

    async def delete(self, conn: object, user_id: int) -> None:
        raise _Crash


async def _make_database(path: Path) -> Database:
    database = Database(path)
    async with database.connection() as conn:
        await database.migrate(conn)
    return database


def _deps(
    database: Database,
    clock: FakeClock,
    checks: FakeCheckRunner,
    *,
    watch_service: type[WatchService] = WatchService,
    drafts: DraftRepository | None = None,
) -> WizardDeps:
    return WizardDeps(
        database=database,
        drafts=drafts if drafts is not None else DraftRepository(),
        watches=watch_service(database, WatchRepository(), clock),
        checks=checks,
        clock=clock,
    )


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
    return _deps(database, fake_clock, fake_checks)


async def _draft(deps: WizardDeps):  # type: ignore[no-untyped-def]
    async with deps.database.connection() as conn:
        return await deps.drafts.get(conn, USER_ID)


async def _seed_state(deps: WizardDeps, state: WizardState, payload: dict[str, object]) -> None:
    async with deps.database.connection() as conn:
        await deps.drafts.upsert(conn, USER_ID, state.value, payload, deps.clock.now())



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


async def _choose_uniform_time(
    deps: WizardDeps,
    times: str = "18:00 to 23:00",
) -> None:
    reply = await handle_wizard_callback(
        _callback_update("wizard:time-mode:same"),
        deps,
    )
    assert reply is not None
    assert (await handle_wizard_text(_text_update(times), deps)) is not None


async def _drive_to_time_mode(deps: WizardDeps) -> RenderedMessage:
    await start_new(_text_update("/new"), deps)
    await handle_wizard_text(_text_update(FILM_URL), deps)
    reply = await handle_wizard_text(
        _text_update("2026-08-26 to 2026-08-30"),
        deps,
    )
    assert reply is not None
    return reply


async def _drive_to_review(deps: WizardDeps) -> None:
    """Push a fresh draft through every step up to (and including) ``REVIEW``."""
    await start_new(_text_update(""), deps)
    assert (await handle_wizard_text(_text_update(FILM_URL), deps)) is not None
    assert (await handle_wizard_text(_text_update("2026-08-26 to 2026-08-30"), deps)) is not None
    await _choose_uniform_time(deps)
    assert (await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)) is not None
    assert (
        await handle_wizard_callback(
            _callback_update("wizard:seat-mode:advanced"),
            deps,
        )
    ) is not None
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
    assert draft.payload == {"seat_flow_version": 2, "time_flow_version": 2}


async def test_start_new_replaces_an_abandoned_draft(deps: WizardDeps) -> None:
    await start_new(_text_update("/new"), deps)
    await handle_wizard_text(_text_update(FILM_URL), deps)

    await start_new(_text_update("/new"), deps)

    async with deps.database.connection() as conn:
        draft = await deps.drafts.get(conn, USER_ID)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_URL.value
    assert draft.payload == {"seat_flow_version": 2, "time_flow_version": 2}


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
    await _choose_uniform_time(deps)

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
    assert draft.payload == {"seat_flow_version": 2, "time_flow_version": 2}


async def test_invalid_quantity_leaves_draft_unchanged(deps: WizardDeps) -> None:
    await start_new(_text_update("/new"), deps)
    await handle_wizard_text(_text_update(FILM_URL), deps)
    await handle_wizard_text(_text_update("2026-08-26 to 2026-08-30"), deps)
    await _choose_uniform_time(deps)

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
    await _choose_uniform_time(deps)

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
    await _choose_uniform_time(deps)

    reply = await handle_wizard_callback(_callback_update("wizard:qty:8"), deps)

    assert reply is not None
    async with deps.database.connection() as conn:
        draft = await deps.drafts.get(conn, USER_ID)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_SEAT_MODE.value
    assert draft.payload["quantity"] == 8


# ---------------------------------------------------------------------------
# Simple vs Advanced seat setup
# ---------------------------------------------------------------------------


async def _drive_to_quantity(deps: WizardDeps) -> None:
    await start_new(_text_update("/new"), deps)
    await handle_wizard_text(_text_update(FILM_URL), deps)
    await handle_wizard_text(_text_update("2026-08-26 to 2026-08-30"), deps)
    await _choose_uniform_time(deps)


# ---------------------------------------------------------------------------
# Time-mode schedule choice
# ---------------------------------------------------------------------------


async def test_new_draft_asks_for_time_mode_after_dates(deps: WizardDeps) -> None:
    reply = await _drive_to_time_mode(deps)

    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_TIME_MODE.value
    assert _callback_data(reply) == {
        "wizard:time-mode:same",
        "wizard:time-mode:split",
    }


async def test_uniform_time_mode_uses_one_window(deps: WizardDeps) -> None:
    await _drive_to_time_mode(deps)

    await _choose_uniform_time(deps)

    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_QUANTITY.value
    assert draft.payload["time_from"] == "18:00:00"
    assert draft.payload["time_to"] == "23:00:00"
    assert "weekend_time_from" not in draft.payload
    assert "weekend_time_to" not in draft.payload


async def test_split_time_mode_collects_both_windows(deps: WizardDeps) -> None:
    await _drive_to_time_mode(deps)
    await handle_wizard_callback(
        _callback_update("wizard:time-mode:split"),
        deps,
    )
    await handle_wizard_text(_text_update("18:00 to 23:00"), deps)

    reply = await handle_wizard_text(_text_update("12:00 to 16:00"), deps)

    assert reply is not None
    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_QUANTITY.value
    assert draft.payload["time_from"] == "18:00:00"
    assert draft.payload["time_to"] == "23:00:00"
    assert draft.payload["weekend_time_from"] == "12:00:00"
    assert draft.payload["weekend_time_to"] == "16:00:00"


async def test_split_time_mode_prompts_weekday_then_weekend_windows(
    deps: WizardDeps,
) -> None:
    await _drive_to_time_mode(deps)

    reply = await handle_wizard_callback(_callback_update("wizard:time-mode:split"), deps)

    assert reply is not None
    assert "What Monday-Friday time window?" in reply.text

    followup = await handle_wizard_text(_text_update("18:00 to 23:00"), deps)

    assert followup is not None
    assert "What Saturday-Sunday time window?" in followup.text


async def test_invalid_time_mode_leaves_draft_unchanged(deps: WizardDeps) -> None:
    await _drive_to_time_mode(deps)

    reply = await handle_wizard_callback(
        _callback_update("wizard:time-mode:weekly"),
        deps,
    )

    assert reply is not None
    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_TIME_MODE.value
    assert "time_from" not in draft.payload


async def test_split_schedule_resumes_at_weekend_window_after_restart(
    db_path: Path,
    fake_clock: FakeClock,
    fake_checks: FakeCheckRunner,
) -> None:
    first_database = await _make_database(db_path)
    first = _deps(first_database, fake_clock, fake_checks)
    await _drive_to_time_mode(first)
    await handle_wizard_callback(
        _callback_update("wizard:time-mode:split"),
        first,
    )
    await handle_wizard_text(_text_update("18:00 to 23:00"), first)

    second = _deps(Database(db_path), fake_clock, fake_checks)
    draft = await _draft(second)

    assert draft is not None
    assert draft.state == WizardState.AWAIT_WEEKEND_TIME_RANGE.value
    assert draft.payload["time_from"] == "18:00:00"
    assert draft.payload["time_to"] == "23:00:00"


async def test_legacy_time_range_draft_keeps_uniform_flow(deps: WizardDeps) -> None:
    await _seed_state(
        deps,
        WizardState.AWAIT_TIME_RANGE,
        {
            "date_from": "2026-08-26",
            "date_to": "2026-08-30",
        },
    )

    await handle_wizard_text(_text_update("18:00 to 23:00"), deps)

    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_QUANTITY.value
    assert "time_flow_version" not in draft.payload


async def test_split_schedule_review_labels_both_windows(deps: WizardDeps) -> None:
    await _drive_to_time_mode(deps)
    await handle_wizard_callback(
        _callback_update("wizard:time-mode:split"),
        deps,
    )
    await handle_wizard_text(_text_update("18:00 to 23:00"), deps)
    await handle_wizard_text(_text_update("12:00 to 16:00"), deps)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)
    await handle_wizard_callback(
        _callback_update("wizard:seat-mode:simple"),
        deps,
    )
    await handle_wizard_callback(
        _callback_update("wizard:seat-preference:only_best"),
        deps,
    )
    await handle_wizard_text(_text_update("skip"), deps)

    reply = await handle_wizard_callback(
        _callback_update("wizard:mode:one_off"),
        deps,
    )

    assert reply is not None
    assert "Weekdays: 18:00 to 23:00" in reply.text
    assert "Weekends: 12:00 to 16:00" in reply.text


def _callback_data(message: RenderedMessage) -> set[str]:
    reply_markup = message.reply_markup
    assert reply_markup is not None
    return {
        button.callback_data
        for row in reply_markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    }


async def test_new_quantity_choice_prompts_for_simple_or_advanced(
    deps: WizardDeps,
) -> None:
    await _drive_to_quantity(deps)

    reply = await handle_wizard_callback(
        _callback_update("wizard:qty:2"),
        deps,
    )

    assert reply is not None
    assert reply.text == (
        "How would you like to choose acceptable seats?\n\n"
        "<b>Simple</b>: choose a preset for central seats between the aisles.\n"
        "<b>Advanced</b>: set preferred and excluded rows or exact seats yourself."
    )
    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_SEAT_MODE.value
    assert _callback_data(reply) == {
        "wizard:seat-mode:simple",
        "wizard:seat-mode:advanced",
    }


async def test_simple_mode_explains_both_presets_before_the_buttons(
    deps: WizardDeps,
) -> None:
    await _drive_to_quantity(deps)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)

    reply = await handle_wizard_callback(
        _callback_update("wizard:seat-mode:simple"),
        deps,
    )

    assert reply is not None
    assert "Only the best" in reply.text
    assert "between the aisles" in reply.text
    assert "row J" in reply.text
    assert "Best and good" in reply.text
    assert "row C" in reply.text
    assert _callback_data(reply) == {
        "wizard:seat-preference:only_best",
        "wizard:seat-preference:best_and_good",
    }


@pytest.mark.parametrize(
    ("callback", "strategy"),
    [
        (
            "wizard:seat-preference:only_best",
            SeatPreferenceStrategy.ONLY_BEST,
        ),
        (
            "wizard:seat-preference:best_and_good",
            SeatPreferenceStrategy.BEST_AND_GOOD,
        ),
    ],
)
async def test_simple_preset_skips_all_manual_seat_prompts(
    deps: WizardDeps,
    callback: str,
    strategy: SeatPreferenceStrategy,
) -> None:
    await _drive_to_quantity(deps)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)
    await handle_wizard_callback(
        _callback_update("wizard:seat-mode:simple"),
        deps,
    )

    await handle_wizard_callback(_callback_update(callback), deps)

    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_PREFERRED_INSTANT.value
    assert draft.payload["seat_preference_strategy"] == strategy.value
    assert {
        "preferred_rows",
        "preferred_seats",
        "excluded_rows",
        "excluded_seats",
    }.isdisjoint(draft.payload)


async def test_advanced_mode_keeps_the_existing_manual_flow(
    deps: WizardDeps,
) -> None:
    await _drive_to_quantity(deps)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)

    await handle_wizard_callback(
        _callback_update("wizard:seat-mode:advanced"),
        deps,
    )

    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_PREFERRED_ROWS.value
    assert draft.payload["seat_preference_strategy"] == "advanced"


@pytest.mark.parametrize(
    ("callback", "label"),
    [
        ("wizard:seat-preference:only_best", "Only the best"),
        ("wizard:seat-preference:best_and_good", "Best and good"),
    ],
)
async def test_simple_review_names_the_selected_preset(
    deps: WizardDeps,
    callback: str,
    label: str,
) -> None:
    await _drive_to_quantity(deps)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)
    await handle_wizard_callback(
        _callback_update("wizard:seat-mode:simple"),
        deps,
    )
    await handle_wizard_callback(
        _callback_update(callback),
        deps,
    )
    await handle_wizard_text(_text_update("skip"), deps)

    reply = await handle_wizard_callback(
        _callback_update("wizard:mode:one_off"),
        deps,
    )

    assert reply is not None
    assert f"Seat preference: {label}" in reply.text
    assert "Preferred rows:" not in reply.text
    assert "Excluded seats:" not in reply.text


async def test_advanced_review_keeps_manual_seat_details(
    deps: WizardDeps,
) -> None:
    await _drive_to_quantity(deps)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)
    await handle_wizard_callback(
        _callback_update("wizard:seat-mode:advanced"),
        deps,
    )
    await handle_wizard_text(_text_update("L"), deps)
    await handle_wizard_text(_text_update("L17"), deps)
    await handle_wizard_text(_text_update("A"), deps)
    await handle_wizard_text(_text_update("A1"), deps)
    await handle_wizard_text(_text_update("skip"), deps)

    reply = await handle_wizard_callback(
        _callback_update("wizard:mode:one_off"),
        deps,
    )

    assert reply is not None
    assert "Preferred rows: L" in reply.text
    assert "Preferred seats: L17" in reply.text
    assert "Excluded rows: A" in reply.text
    assert "Excluded seats: A1" in reply.text


async def test_invalid_simple_preset_callback_leaves_the_draft_unchanged(
    deps: WizardDeps,
) -> None:
    await _drive_to_quantity(deps)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)
    await handle_wizard_callback(
        _callback_update("wizard:seat-mode:simple"),
        deps,
    )

    reply = await handle_wizard_callback(
        _callback_update("wizard:seat-preference:advanced"),
        deps,
    )

    assert reply is not None
    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_SIMPLE_SEAT_PREFERENCE.value
    assert "seat_preference_strategy" not in draft.payload


async def test_stale_seat_callback_leaves_the_current_step_unchanged(
    deps: WizardDeps,
) -> None:
    await _drive_to_quantity(deps)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)
    await handle_wizard_callback(
        _callback_update("wizard:seat-mode:advanced"),
        deps,
    )

    reply = await handle_wizard_callback(
        _callback_update("wizard:seat-mode:simple"),
        deps,
    )

    assert reply is not None
    assert "no longer active" in reply.text
    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_PREFERRED_ROWS.value
    assert draft.payload["seat_preference_strategy"] == "advanced"


async def test_legacy_quantity_draft_continues_as_advanced(
    deps: WizardDeps,
) -> None:
    await _seed_state(deps, WizardState.AWAIT_QUANTITY, {})

    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)

    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_PREFERRED_ROWS.value
    assert draft.payload["seat_preference_strategy"] == "advanced"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (WizardState.AWAIT_PREFERRED_ROWS, WizardState.AWAIT_PREFERRED_SEATS),
        (WizardState.AWAIT_PREFERRED_SEATS, WizardState.AWAIT_EXCLUDED_ROWS),
        (WizardState.AWAIT_EXCLUDED_ROWS, WizardState.AWAIT_EXCLUDED_SEATS),
        (WizardState.AWAIT_EXCLUDED_SEATS, WizardState.AWAIT_PREFERRED_INSTANT),
        (WizardState.AWAIT_PREFERRED_INSTANT, WizardState.AWAIT_MODE),
    ],
)
async def test_legacy_manual_draft_resumes_without_a_flow_marker(
    deps: WizardDeps,
    state: WizardState,
    expected: WizardState,
) -> None:
    await _seed_state(deps, state, {})

    reply = await handle_wizard_text(_text_update("skip"), deps)

    assert reply is not None
    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == expected.value
    assert "seat_flow_version" not in draft.payload


async def test_legacy_review_draft_confirms_as_advanced(
    deps: WizardDeps,
) -> None:
    await _drive_to_review(deps)
    draft = await _draft(deps)
    assert draft is not None
    legacy_payload = dict(draft.payload)
    legacy_payload.pop("seat_flow_version")
    legacy_payload.pop("seat_preference_strategy")
    await _seed_state(deps, WizardState.REVIEW, legacy_payload)

    await handle_wizard_callback(_callback_update("wizard:confirm"), deps)

    (watch,) = await deps.watches.list_for_owner(USER_ID)
    assert (
        watch.criteria.seat_preference_strategy
        is SeatPreferenceStrategy.ADVANCED
    )


async def _drive_to_preferred_instant(
    deps: WizardDeps,
    *,
    dates: str = "2026-08-26 to 2026-08-30",
    times: str = "18:00 to 23:00",
) -> None:
    """Push a fresh draft to ``AWAIT_PREFERRED_INSTANT`` with the given window."""
    await start_new(_text_update("/new"), deps)
    await handle_wizard_text(_text_update(FILM_URL), deps)
    await handle_wizard_text(_text_update(dates), deps)
    await _choose_uniform_time(deps, times)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)
    assert (
        await handle_wizard_callback(
            _callback_update("wizard:seat-mode:advanced"),
            deps,
        )
    ) is not None
    for _ in range(4):  # preferred rows/seats, excluded rows/seats
        await handle_wizard_text(_text_update("skip"), deps)


async def test_invalid_preferred_instant_leaves_draft_unchanged(deps: WizardDeps) -> None:
    await _drive_to_preferred_instant(deps)

    # Outside the 18:00-23:00 window.
    reply = await handle_wizard_text(_text_update("2026-08-27 12:00"), deps)

    assert reply is not None
    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_PREFERRED_INSTANT.value


async def test_preferred_instant_is_read_in_london_and_stored_as_utc_during_bst(
    deps: WizardDeps,
) -> None:
    """August is BST: 20:00 at the cinema is 19:00Z, not 20:00Z."""
    await _drive_to_preferred_instant(deps)

    reply = await handle_wizard_text(_text_update("2026-08-27 20:00"), deps)

    assert reply is not None
    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_MODE.value
    assert draft.payload["preferred_utc_instant"] == "2026-08-27T19:00:00+00:00"


async def test_preferred_instant_is_read_in_london_and_stored_as_utc_during_gmt(
    deps: WizardDeps,
) -> None:
    """January is GMT: London and UTC agree, and the same input must survive unshifted."""
    await _drive_to_preferred_instant(deps, dates="2026-01-10 to 2026-01-12")

    reply = await handle_wizard_text(_text_update("2026-01-11 20:00"), deps)

    assert reply is not None
    draft = await _draft(deps)
    assert draft is not None
    assert draft.payload["preferred_utc_instant"] == "2026-01-11T20:00:00+00:00"


async def test_preferred_instant_at_the_edge_of_the_window_is_accepted_in_local_time(
    deps: WizardDeps,
) -> None:
    """23:00 London is 22:00Z: judged against the window as the user typed it."""
    await _drive_to_preferred_instant(deps)

    reply = await handle_wizard_text(_text_update("2026-08-27 23:00"), deps)

    assert reply is not None
    draft = await _draft(deps)
    assert draft is not None
    assert draft.payload["preferred_utc_instant"] == "2026-08-27T22:00:00+00:00"


async def test_preferred_instant_rejects_a_local_time_the_utc_reading_would_allow(
    deps: WizardDeps,
) -> None:
    """00:30 London on the 28th is 23:30Z on the 27th -- inside the window only if the
    input is (wrongly) read as UTC."""
    await _drive_to_preferred_instant(deps)

    reply = await handle_wizard_text(_text_update("2026-08-28 00:30"), deps)

    assert reply is not None
    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_PREFERRED_INSTANT.value


async def test_preferred_instant_is_accepted_inside_a_window_that_crosses_midnight(
    deps: WizardDeps,
) -> None:
    """A wrapping window must not make every preferred time unrepresentable."""
    await _drive_to_preferred_instant(deps, times="22:00 to 01:00")

    reply = await handle_wizard_text(_text_update("2026-08-27 00:30"), deps)

    assert reply is not None
    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_MODE.value
    assert draft.payload["preferred_utc_instant"] == "2026-08-26T23:30:00+00:00"


async def test_preferred_instant_rejects_the_daytime_gap_of_a_wrapping_window(
    deps: WizardDeps,
) -> None:
    await _drive_to_preferred_instant(deps, times="22:00 to 01:00")

    reply = await handle_wizard_text(_text_update("2026-08-27 14:00"), deps)

    assert reply is not None
    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_PREFERRED_INSTANT.value


async def test_preferred_instant_rejects_a_local_time_that_does_not_exist(
    deps: WizardDeps,
) -> None:
    """01:30 on 29 March 2026 never happens in London -- the clocks jump 01:00 to 02:00."""
    await _drive_to_preferred_instant(
        deps, dates="2026-03-28 to 2026-03-30", times="22:00 to 02:00"
    )

    reply = await handle_wizard_text(_text_update("2026-03-29 01:30"), deps)

    assert reply is not None
    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_PREFERRED_INSTANT.value


async def _drive_split_to_preferred_instant(deps: WizardDeps) -> None:
    await _drive_to_time_mode(deps)
    await handle_wizard_callback(
        _callback_update("wizard:time-mode:split"),
        deps,
    )
    await handle_wizard_text(_text_update("18:00 to 23:00"), deps)
    await handle_wizard_text(_text_update("12:00 to 16:00"), deps)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)
    await handle_wizard_callback(
        _callback_update("wizard:seat-mode:simple"),
        deps,
    )
    await handle_wizard_callback(
        _callback_update("wizard:seat-preference:only_best"),
        deps,
    )


async def test_split_preferred_time_uses_the_weekend_window(
    deps: WizardDeps,
) -> None:
    await _drive_split_to_preferred_instant(deps)

    accepted = await handle_wizard_text(
        _text_update("2026-08-29 13:00"),
        deps,
    )

    assert accepted is not None
    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_MODE.value
    assert draft.payload["preferred_utc_instant"] == "2026-08-29T12:00:00+00:00"

    await _drive_split_to_preferred_instant(deps)
    rejected = await handle_wizard_text(
        _text_update("2026-08-29 19:00"),
        deps,
    )

    assert rejected is not None
    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_PREFERRED_INSTANT.value
    assert "preferred_utc_instant" not in draft.payload


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
    await _choose_uniform_time(deps)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)
    assert (
        await handle_wizard_callback(
            _callback_update("wizard:seat-mode:advanced"),
            deps,
        )
    ) is not None
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
    deps_one = _deps(database_one, fake_clock, fake_checks)
    await start_new(_text_update("/new"), deps_one)
    await handle_wizard_text(_text_update(FILM_URL), deps_one)
    await handle_wizard_text(_text_update("2026-08-26 to 2026-08-30"), deps_one)

    # Simulate a process restart: a brand new Database/WizardDeps over the same file.
    database_two = Database(db_path)
    deps_two = _deps(database_two, fake_clock, fake_checks)
    async with database_two.connection() as conn:
        draft = await deps_two.drafts.get(conn, USER_ID)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_TIME_MODE.value
    assert draft.payload["slug"] == "dog-stars"
    assert draft.payload["date_from"] == "2026-08-26"

    assert (
        await handle_wizard_callback(_callback_update("wizard:time-mode:same"), deps_two)
    ) is not None
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
    assert "watch created" in reply.text.lower()
    watches = await deps.watches.list_for_owner(USER_ID)
    assert len(watches) == 1
    assert watches[0].criteria.slug == "dog-stars"
    assert fake_checks.calls == [(watches[0].watch_id, CheckTrigger.CREATION)]
    assert await _draft(deps) is None


async def test_confirmed_watch_carries_the_utc_instant_of_the_london_preference(
    deps: WizardDeps,
) -> None:
    """The instant that reaches ranking is the one the user actually named, in London."""
    await _drive_to_preferred_instant(deps)
    await handle_wizard_text(_text_update("2026-08-27 20:00"), deps)
    await handle_wizard_callback(_callback_update("wizard:mode:one_off"), deps)

    await handle_wizard_callback(_callback_update("wizard:confirm"), deps)

    (watch,) = await deps.watches.list_for_owner(USER_ID)
    assert watch.criteria.preferred_utc_instant == datetime(2026, 8, 27, 19, 0, tzinfo=UTC)
    assert watch.criteria.preferred_utc_instant.astimezone(LONDON).hour == 20


async def test_confirm_cancel_deletes_the_draft_without_creating_a_watch(
    deps: WizardDeps, fake_checks: FakeCheckRunner
) -> None:
    await _drive_to_review(deps)

    reply = await handle_wizard_callback(_callback_update("wizard:cancel"), deps)

    assert reply is not None
    assert not await deps.watches.list_for_owner(USER_ID)
    assert fake_checks.calls == []
    assert await _draft(deps) is None


async def test_a_second_confirm_after_success_is_a_no_op(
    deps: WizardDeps, fake_checks: FakeCheckRunner
) -> None:
    """A duplicate delivery arriving after the first completed has no draft to act on."""
    await _drive_to_review(deps)
    await handle_wizard_callback(_callback_update("wizard:confirm"), deps)

    reply = await handle_wizard_callback(_callback_update("wizard:confirm"), deps)

    assert reply is None
    assert len(await deps.watches.list_for_owner(USER_ID)) == 1
    assert len(fake_checks.calls) == 1


async def test_confirm_requires_a_reviewed_draft(deps: WizardDeps) -> None:
    await start_new(_text_update("/new"), deps)

    reply = await handle_wizard_callback(_callback_update("wizard:confirm"), deps)

    assert reply is None


async def test_confirm_revalidates_the_criteria_before_claiming_the_draft(
    deps: WizardDeps, fake_checks: FakeCheckRunner
) -> None:
    """A payload that can no longer build a WatchCriteria must be rejected while the
    draft is still ``REVIEW`` -- claiming first would strand it in ``CONFIRMING``."""
    await _drive_to_review(deps)
    draft = await _draft(deps)
    assert draft is not None
    await _seed_state(deps, WizardState.REVIEW, {**draft.payload, "quantity": 99})

    reply = await handle_wizard_callback(_callback_update("wizard:confirm"), deps)

    assert reply is not None
    assert "watch created" not in reply.text.lower()
    after = await _draft(deps)
    assert after is not None
    assert after.state == WizardState.REVIEW.value
    assert not await deps.watches.list_for_owner(USER_ID)
    assert fake_checks.calls == []


async def test_concurrent_duplicate_confirm_creates_only_one_watch(
    deps: WizardDeps, fake_checks: FakeCheckRunner
) -> None:
    """Two callbacks racing for the same reviewed draft must not double-create.

    ``BEGIN IMMEDIATE`` serializes the claim: whichever task wins proceeds, and the
    other observes a claim whose lease is still live and reports progress instead.
    """
    await _drive_to_review(deps)

    results = await asyncio.gather(
        handle_wizard_callback(_callback_update("wizard:confirm"), deps),
        handle_wizard_callback(_callback_update("wizard:confirm"), deps),
    )

    watches = await deps.watches.list_for_owner(USER_ID)
    assert len(watches) == 1
    assert len(fake_checks.calls) == 1
    created = [r for r in results if r is not None and "watch created" in r.text.lower()]
    assert len(created) == 1


async def test_confirm_while_an_attempt_holds_a_live_claim_reports_progress(
    deps: WizardDeps, fake_checks: FakeCheckRunner
) -> None:
    """A live claim is not a finished one: the reply must not imply a watch exists."""
    await _drive_to_review(deps)
    draft = await _draft(deps)
    assert draft is not None
    await _seed_state(deps, WizardState.CONFIRMING, dict(draft.payload))

    reply = await handle_wizard_callback(_callback_update("wizard:confirm"), deps)

    assert reply is not None
    assert "watch created" not in reply.text.lower()
    assert not await deps.watches.list_for_owner(USER_ID)
    assert fake_checks.calls == []


async def test_a_stale_claim_is_resumed_by_a_retried_confirm(
    deps: WizardDeps, fake_clock: FakeClock, fake_checks: FakeCheckRunner
) -> None:
    """Once the lease expires the claim is abandoned, and a retry finishes the job
    rather than telling the user to /cancel."""
    await _drive_to_review(deps)
    draft = await _draft(deps)
    assert draft is not None
    await _seed_state(deps, WizardState.CONFIRMING, dict(draft.payload))
    fake_clock.current += CLAIM_LEASE + timedelta(minutes=1)

    reply = await handle_wizard_callback(_callback_update("wizard:confirm"), deps)

    assert reply is not None
    assert "watch created" in reply.text.lower()
    assert len(await deps.watches.list_for_owner(USER_ID)) == 1
    assert len(fake_checks.calls) == 1
    assert await _draft(deps) is None


# ---------------------------------------------------------------------------
# Crash barriers: the saga must converge from every durable intermediate state
# ---------------------------------------------------------------------------


async def test_crash_before_the_watch_exists_recovers_into_exactly_one_watch(
    database: Database, fake_clock: FakeClock, fake_checks: FakeCheckRunner
) -> None:
    deps = _deps(database, fake_clock, fake_checks)
    await _drive_to_review(deps)
    crashing = _deps(database, fake_clock, fake_checks, watch_service=CrashBeforeCreate)

    with pytest.raises(_Crash):
        await handle_wizard_callback(_callback_update("wizard:confirm"), crashing)

    stuck = await _draft(deps)
    assert stuck is not None
    assert stuck.state == WizardState.CONFIRMING.value
    assert stuck.payload["confirm_phase"] == ConfirmPhase.CLAIMED.value
    assert not await deps.watches.list_for_owner(USER_ID)

    recovered = await recover_confirmations(deps)

    assert len(recovered) == 1
    assert recovered[0].user_id == USER_ID
    assert len(await deps.watches.list_for_owner(USER_ID)) == 1
    assert len(fake_checks.calls) == 1
    assert await _draft(deps) is None


async def test_crash_after_the_watch_exists_recovers_without_a_second_watch(
    database: Database, fake_clock: FakeClock, fake_checks: FakeCheckRunner
) -> None:
    """The watch is created but the process dies before that fact is recorded.

    Only a watch identity derived from the persisted setup id makes the retried create
    land on the same row instead of inserting a second one.
    """
    deps = _deps(database, fake_clock, fake_checks)
    await _drive_to_review(deps)
    crashing = _deps(database, fake_clock, fake_checks, watch_service=CrashAfterCreate)

    with pytest.raises(_Crash):
        await handle_wizard_callback(_callback_update("wizard:confirm"), crashing)

    stuck = await _draft(deps)
    assert stuck is not None
    assert stuck.payload["confirm_phase"] == ConfirmPhase.CLAIMED.value
    created_first = await deps.watches.list_for_owner(USER_ID)
    assert len(created_first) == 1

    recovered = await recover_confirmations(deps)

    assert len(recovered) == 1
    watches = await deps.watches.list_for_owner(USER_ID)
    assert [watch.watch_id for watch in watches] == [created_first[0].watch_id]
    assert len(fake_checks.calls) == 1
    assert await _draft(deps) is None


async def test_crash_after_the_creation_check_does_not_run_a_second_check(
    database: Database, fake_clock: FakeClock, fake_checks: FakeCheckRunner
) -> None:
    deps = _deps(database, fake_clock, fake_checks)
    await _drive_to_review(deps)
    crashing_checks = CrashAfterCheckRunner()
    crashing = _deps(database, fake_clock, crashing_checks)

    with pytest.raises(_Crash):
        await handle_wizard_callback(_callback_update("wizard:confirm"), crashing)

    stuck = await _draft(deps)
    assert stuck is not None
    assert stuck.payload["confirm_phase"] == ConfirmPhase.CHECK_STARTED.value
    assert len(crashing_checks.calls) == 1

    recovered = await recover_confirmations(deps)

    assert len(recovered) == 1
    assert len(await deps.watches.list_for_owner(USER_ID)) == 1
    assert fake_checks.calls == []
    assert await _draft(deps) is None


async def test_crash_before_the_draft_is_deleted_recovers_by_finishing_the_saga(
    database: Database, fake_clock: FakeClock, fake_checks: FakeCheckRunner
) -> None:
    deps = _deps(database, fake_clock, fake_checks)
    await _drive_to_review(deps)
    crashing = _deps(database, fake_clock, fake_checks, drafts=CrashOnDraftDelete())

    with pytest.raises(_Crash):
        await handle_wizard_callback(_callback_update("wizard:confirm"), crashing)

    stuck = await _draft(deps)
    assert stuck is not None
    assert stuck.payload["confirm_phase"] == ConfirmPhase.CHECK_DONE.value
    assert len(fake_checks.calls) == 1

    recovered = await recover_confirmations(deps)

    assert len(recovered) == 1
    assert "watch created" in recovered[0].message.text.lower()
    assert len(await deps.watches.list_for_owner(USER_ID)) == 1
    assert len(fake_checks.calls) == 1
    assert await _draft(deps) is None


async def test_recovery_after_a_restart_uses_a_freshly_constructed_deps(
    db_path: Path, fake_clock: FakeClock, fake_checks: FakeCheckRunner
) -> None:
    """A restart, not a retry: nothing in memory survives, and no user action is needed."""
    database_one = await _make_database(db_path)
    deps_one = _deps(database_one, fake_clock, fake_checks)
    await _drive_to_review(deps_one)
    crashing = _deps(database_one, fake_clock, fake_checks, watch_service=CrashBeforeCreate)
    with pytest.raises(_Crash):
        await handle_wizard_callback(_callback_update("wizard:confirm"), crashing)

    deps_two = _deps(Database(db_path), fake_clock, fake_checks)
    recovered = await recover_confirmations(deps_two)

    assert [entry.user_id for entry in recovered] == [USER_ID]
    assert len(await deps_two.watches.list_for_owner(USER_ID)) == 1
    assert len(fake_checks.calls) == 1
    assert await _draft(deps_two) is None


async def test_recovery_is_a_no_op_when_no_draft_is_mid_confirmation(
    deps: WizardDeps, fake_checks: FakeCheckRunner
) -> None:
    await _drive_to_review(deps)

    assert await recover_confirmations(deps) == ()
    assert not await deps.watches.list_for_owner(USER_ID)
    assert fake_checks.calls == []


# ---------------------------------------------------------------------------
# Compensation: a routine failure must leave a retryable, truthfully described draft
# ---------------------------------------------------------------------------


async def test_a_failure_before_the_watch_exists_says_nothing_was_saved_and_is_retryable(
    database: Database, fake_clock: FakeClock, fake_checks: FakeCheckRunner
) -> None:
    deps = _deps(database, fake_clock, fake_checks)
    await _drive_to_review(deps)
    failing = _deps(database, fake_clock, fake_checks, watch_service=FailingCreate)

    reply = await handle_wizard_callback(_callback_update("wizard:confirm"), failing)

    assert reply is not None
    assert "watch created" not in reply.text.lower()
    assert "nothing was saved" in reply.text.lower()
    compensated = await _draft(deps)
    assert compensated is not None
    assert compensated.state == WizardState.REVIEW.value
    assert not await deps.watches.list_for_owner(USER_ID)

    retry = await handle_wizard_callback(_callback_update("wizard:confirm"), deps)

    assert retry is not None
    assert "watch created" in retry.text.lower()
    assert len(await deps.watches.list_for_owner(USER_ID)) == 1
    assert len(fake_checks.calls) == 1
    assert await _draft(deps) is None


async def test_a_check_failure_after_the_watch_exists_does_not_deny_the_watch(
    database: Database, fake_clock: FakeClock, fake_checks: FakeCheckRunner
) -> None:
    """The watch really is saved; a reply claiming otherwise would be a lie the user
    acts on. The draft stays retryable and the retry does not re-run the check."""
    deps = _deps(database, fake_clock, fake_checks)
    await _drive_to_review(deps)
    failing_checks = FailingCheckRunner()
    failing = _deps(database, fake_clock, failing_checks)

    reply = await handle_wizard_callback(_callback_update("wizard:confirm"), failing)

    assert reply is not None
    assert "nothing was saved" not in reply.text.lower()
    assert "saved" in reply.text.lower()
    compensated = await _draft(deps)
    assert compensated is not None
    assert compensated.state == WizardState.REVIEW.value
    assert compensated.payload["confirm_phase"] == ConfirmPhase.CHECK_STARTED.value
    assert len(await deps.watches.list_for_owner(USER_ID)) == 1

    retry = await handle_wizard_callback(_callback_update("wizard:confirm"), deps)

    assert retry is not None
    assert "watch created" in retry.text.lower()
    assert len(await deps.watches.list_for_owner(USER_ID)) == 1
    assert len(failing_checks.calls) == 1
    assert fake_checks.calls == []
    assert await _draft(deps) is None


async def test_a_retried_attempt_reuses_the_persisted_setup_id(
    database: Database, fake_clock: FakeClock, fake_checks: FakeCheckRunner
) -> None:
    """The setup id is what makes a retry idempotent, so it must outlive the attempt."""
    deps = _deps(database, fake_clock, fake_checks)
    await _drive_to_review(deps)
    failing = _deps(database, fake_clock, fake_checks, watch_service=FailingCreate)
    await handle_wizard_callback(_callback_update("wizard:confirm"), failing)
    first = await _draft(deps)
    assert first is not None

    await handle_wizard_callback(_callback_update("wizard:confirm"), failing)
    second = await _draft(deps)

    assert second is not None
    assert second.payload["setup_id"] == first.payload["setup_id"]
    assert second.payload["confirm_attempts"] == 2


async def test_recovery_releases_a_claim_it_can_never_complete(
    database: Database, fake_clock: FakeClock, fake_checks: FakeCheckRunner
) -> None:
    """Skipping an unsatisfiable claim would strand it across every future restart.

    Recovery has to hand it back to the user in ``REVIEW``, where it can be fixed or
    cancelled, rather than leaving it claimed forever.
    """
    deps = _deps(database, fake_clock, fake_checks)
    await _drive_to_review(deps)
    draft = await _draft(deps)
    assert draft is not None
    await _seed_state(deps, WizardState.CONFIRMING, {**draft.payload, "quantity": 99})

    recovered = await recover_confirmations(deps)

    assert len(recovered) == 1
    assert recovered[0].user_id == USER_ID
    assert "watch created" not in recovered[0].message.text.lower()
    released = await _draft(deps)
    assert released is not None
    assert released.state == WizardState.REVIEW.value
    assert not await deps.watches.list_for_owner(USER_ID)
    assert fake_checks.calls == []


async def test_prepare_confirmation_adds_stable_identity_to_legacy_review_draft(
    deps: WizardDeps,
) -> None:
    await _drive_to_review(deps)
    before = await _draft(deps)
    assert before is not None
    assert "setup_id" not in before.payload

    first = await prepare_confirmation_watch_id(USER_ID, deps)
    second = await prepare_confirmation_watch_id(USER_ID, deps)
    prepared = await _draft(deps)

    assert first is not None
    assert second == first
    assert prepared is not None
    assert prepared.payload["setup_id"]


async def test_confirm_creates_the_prepared_watch_id(deps: WizardDeps) -> None:
    await _drive_to_review(deps)
    prepared = await prepare_confirmation_watch_id(USER_ID, deps)
    assert prepared is not None

    await handle_wizard_callback(_callback_update("wizard:confirm"), deps)

    (watch,) = await deps.watches.list_for_owner(USER_ID)
    assert watch.watch_id == prepared


async def test_prepare_confirmation_does_not_rewrite_an_already_prepared_draft(
    deps: WizardDeps,
    fake_clock: FakeClock,
) -> None:
    await _drive_to_review(deps)
    first = await prepare_confirmation_watch_id(USER_ID, deps)
    before = await _draft(deps)
    assert first is not None
    assert before is not None
    fake_clock.current += timedelta(minutes=5)

    second = await prepare_confirmation_watch_id(USER_ID, deps)
    after = await _draft(deps)

    assert second == first
    assert after == before


async def test_prepare_confirmation_returns_none_without_a_draft(deps: WizardDeps) -> None:
    assert await prepare_confirmation_watch_id(USER_ID, deps) is None


@pytest.mark.parametrize(
    "state",
    [WizardState.AWAIT_TIME_RANGE],
)
async def test_prepare_confirmation_returns_none_for_a_non_confirmable_draft(
    deps: WizardDeps, state: WizardState
) -> None:
    await _seed_state(deps, state, {})
    assert await prepare_confirmation_watch_id(USER_ID, deps) is None
