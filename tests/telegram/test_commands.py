"""Tests for cinema_friend.telegram.commands.

Commands are the only place a Telegram update turns into a state change, so every test
here drives a real database through the real services: asserting on a returned
:class:`RenderedMessage` alone would not show whether a watch was actually paused, and
would not catch an authorization hole that happens to reply politely.

The allow-list tests are the load-bearing ones. Each asserts both halves of the
contract -- the call raises, *and* the database is untouched -- because a handler that
denies after mutating is exactly as broken as one that does not deny at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.error import TimedOut

from cinema_friend.domain.bfi import Performance, PerformanceListing
from cinema_friend.domain.errors import (
    BfiContractError,
    BfiNetworkError,
    ConflictError,
    InputError,
)
from cinema_friend.domain.results import (
    CheckResult,
    DeliveryStatus,
    RankedOption,
    RankVector,
)
from cinema_friend.domain.state import CheckOutcome, CheckTrigger, WatchMode, WatchStatus
from cinema_friend.domain.watch import Watch, WatchCriteria
from cinema_friend.services.check_service import CheckService
from cinema_friend.services.watch_service import WatchService
from cinema_friend.storage.circuit_repository import SqliteCircuitStore
from cinema_friend.storage.database import Database
from cinema_friend.storage.draft_repository import DraftRepository
from cinema_friend.storage.notification_repository import NotificationRepository
from cinema_friend.storage.result_repository import ResultRepository
from cinema_friend.storage.watch_repository import WatchRepository
from cinema_friend.telegram.bot import (
    DeliveryAttempt,
    DeliveryAttemptOutcome,
    DeliveryRun,
    DeliveryWorker,
)
from cinema_friend.telegram.callbacks import encode_callback
from cinema_friend.telegram.commands import (
    RESULTS_GONE,
    STALE_ACTION,
    CommandDeps,
    handle_callback,
    handle_cancel,
    handle_check,
    handle_delete,
    handle_help,
    handle_new,
    handle_pause,
    handle_resume,
    handle_text,
    handle_watches,
    handle_wizard_button,
)
from cinema_friend.telegram.wizard import WizardDeps, WizardState
from tests.fakes import FakeClock

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
USER_ID = 42
OTHER_USER_ID = 43
FILM_URL = "https://whatson.bfi.org.uk/imax/Online/article/dog-stars"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Bot:
    """Records every outbound message; optionally raises a queued failure."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.error: BaseException | None = None

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: object = None,
    ) -> object:
        if self.error is not None:
            raise self.error
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
            }
        )
        return object()


class _Gateway:
    """A BFI stand-in that lists nothing, or fails on demand."""

    def __init__(self) -> None:
        self.error: BaseException | None = None
        self.listing = PerformanceListing(title=None, performances=())
        self.calls: list[str] = []

    async def list_performances(self, slug: str) -> PerformanceListing:
        self.calls.append(slug)
        if self.error is not None:
            raise self.error
        return self.listing

    async def load_seat_map(self, performance: Performance) -> object:  # pragma: no cover
        raise AssertionError("no performance should need a seat map in these tests")


class _RaisingChecks:
    """A check runner that always raises, to exercise the command's error mapping."""

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.calls: list[tuple[UUID, CheckTrigger]] = []

    async def check(self, watch_id: UUID, trigger: CheckTrigger) -> CheckResult:
        self.calls.append((watch_id, trigger))
        raise self._error


class _RecordingDispatcher:
    def __init__(self, run: DeliveryRun | None = None) -> None:
        self.runs = 0
        self._run = run if run is not None else DeliveryRun()

    async def run_once(self) -> DeliveryRun:
        self.runs += 1
        return self._run


def _sent_run(*, watch_id: UUID | None, user_id: int = USER_ID) -> DeliveryRun:
    """A run in which exactly one delivery was sent, for the given watch and user."""
    return DeliveryRun(
        attempts=(
            DeliveryAttempt(
                delivery_id=uuid4(),
                idempotency_key=f"results:{watch_id}",
                recipient_user_id=user_id,
                watch_id=watch_id,
                outcome=DeliveryAttemptOutcome.SENT,
            ),
        )
    )


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class Harness:
    database: Database
    clock: FakeClock
    watches: WatchService
    watch_repository: WatchRepository
    results: ResultRepository
    notifications: NotificationRepository
    drafts: DraftRepository
    gateway: _Gateway
    bot: _Bot
    deps: CommandDeps

    def with_checks(self, checks: Any) -> CommandDeps:
        """Return deps whose check runner is replaced, leaving storage shared."""
        wizard = WizardDeps(
            database=self.database,
            drafts=self.drafts,
            watches=self.watches,
            checks=checks,
            clock=self.clock,
        )
        return CommandDeps(
            wizard=wizard,
            results=self.results,
            deliveries=self.deps.deliveries,
            allowed_user_ids=self.deps.allowed_user_ids,
        )


@pytest.fixture
async def harness(tmp_path: Path) -> Harness:
    database = Database(tmp_path / "cinema.db")
    async with database.connection() as conn:
        await database.migrate(conn)
    clock = FakeClock(NOW)
    watch_repository = WatchRepository()
    watches = WatchService(database, watch_repository, clock)
    results = ResultRepository(database)
    notifications = NotificationRepository(database)
    drafts = DraftRepository()
    gateway = _Gateway()
    checks = CheckService(
        database,
        gateway,
        SqliteCircuitStore(database),
        watch_repository,
        results,
        notifications,
        clock,
        jitter_source=lambda: 0.0,
    )
    bot = _Bot()
    worker = DeliveryWorker(
        bot=bot,
        database=database,
        notifications=notifications,
        results=results,
        watches=watches,
        clock=clock,
    )
    wizard = WizardDeps(
        database=database, drafts=drafts, watches=watches, checks=checks, clock=clock
    )
    deps = CommandDeps(
        wizard=wizard,
        results=results,
        deliveries=worker,
        allowed_user_ids=frozenset({USER_ID}),
    )
    return Harness(
        database=database,
        clock=clock,
        watches=watches,
        watch_repository=watch_repository,
        results=results,
        notifications=notifications,
        drafts=drafts,
        gateway=gateway,
        bot=bot,
        deps=deps,
    )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _criteria(**overrides: Any) -> WatchCriteria:
    defaults: dict[str, Any] = {
        "source_url": FILM_URL,
        "slug": "dog-stars",
        "date_from": date(2026, 8, 26),
        "date_to": date(2026, 8, 30),
        "time_from": time(18, 0),
        "time_to": time(23, 0),
        "quantity": 2,
        "mode": WatchMode.RECURRING,
        "interval": timedelta(minutes=30),
    }
    defaults.update(overrides)
    return WatchCriteria(**defaults)


async def _seed_watch(
    harness: Harness,
    *,
    suffix: int = 1,
    owner: int = USER_ID,
    status: WatchStatus = WatchStatus.ACTIVE,
    title: str | None = "Dog Stars",
    next_check_at: datetime | None = NOW + timedelta(minutes=30),
    **criteria_overrides: Any,
) -> Watch:
    watch = Watch(
        watch_id=UUID(f"00000000-0000-4000-8000-{suffix:012d}"),
        user_id=owner,
        criteria=_criteria(**criteria_overrides),
        status=status,
        created_at=NOW - timedelta(hours=suffix),
        updated_at=NOW,
        next_check_at=next_check_at,
        title=title,
    )
    async with harness.database.connection() as conn:
        await harness.watch_repository.create(conn, watch)
    return watch


def _option(index: int, *, title: str = "Dog Stars") -> RankedOption:
    start = datetime(2026, 8, 26, 18, 0, tzinfo=UTC) + timedelta(minutes=index)
    seat_ids = (f"seat-{index}a", f"seat-{index}b")
    performance = Performance(
        performance_id=f"perf-{index}",
        start_utc=start,
        sales_status_code="OPEN*",
        availability_status_code="E",
        availability_num=50,
        seat_map_url="https://whatson.bfi.org.uk/imax/Online/mapSelect.asp?ID=1",
        options=("1", "2"),
    )
    return RankedOption(
        performance=performance,
        seat_label=f"L{index}-M{index}",
        seat_ids=seat_ids,
        rank_vector=RankVector(
            preferred_seat_overlap=1,
            preferred_row_match=1,
            view_score_band=int((99.0 - index) // 5),
            preferred_time_distance_minutes=index,
            raw_view_score=99.0 - index,
            performance_start=start,
            seat_key="|".join(seat_ids),
        ),
        price_pence=1500,
        seat_categories=("Premium",),
        title=title,
    )


async def _seed_snapshot(harness: Harness, watch: Watch, count: int) -> UUID:
    async with harness.database.connection() as conn:
        check_run_id = await harness.results.start_check(
            conn, watch_id=watch.watch_id, trigger=CheckTrigger.MANUAL, started_at=NOW
        )
        snapshot = await harness.results.complete_with_snapshot(
            conn,
            check_run_id=check_run_id,
            watch_id=watch.watch_id,
            options=tuple(_option(index) for index in range(count)),
            checked_at=NOW,
            outcome=CheckOutcome.SUCCESS,
            performance_count=count,
        )
    return snapshot.snapshot_id


def _text_update(text: str, *, user_id: int = USER_ID) -> Update:
    user = User(id=user_id, first_name="Test", is_bot=False)
    chat = Chat(id=user_id, type="private")
    message = Message(message_id=1, date=NOW, chat=chat, from_user=user, text=text)
    return Update(update_id=1, message=message)


def _callback_update(data: str, *, user_id: int = USER_ID) -> Update:
    user = User(id=user_id, first_name="Test", is_bot=False)
    chat = Chat(id=user_id, type="private")
    message = Message(message_id=1, date=NOW, chat=chat, from_user=user, text="x")
    query = CallbackQuery(
        id="1", from_user=user, chat_instance="chat-instance", data=data, message=message
    )
    return Update(update_id=1, callback_query=query)


async def _status_of(harness: Harness, watch_id: UUID) -> WatchStatus | None:
    async with harness.database.connection() as conn:
        row = await harness.watch_repository.get(conn, watch_id)
    return None if row is None else row.status


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


async def test_every_command_denies_a_user_outside_the_allow_list(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    routes = [
        (handle_help, _text_update("/help", user_id=OTHER_USER_ID)),
        (handle_new, _text_update("/new", user_id=OTHER_USER_ID)),
        (handle_cancel, _text_update("/cancel", user_id=OTHER_USER_ID)),
        (handle_text, _text_update(FILM_URL, user_id=OTHER_USER_ID)),
        (handle_watches, _text_update("/watches", user_id=OTHER_USER_ID)),
        (handle_pause, _text_update("/pause 1", user_id=OTHER_USER_ID)),
        (handle_resume, _text_update("/resume 1", user_id=OTHER_USER_ID)),
        (handle_delete, _text_update("/delete 1", user_id=OTHER_USER_ID)),
        (handle_check, _text_update("/check 1", user_id=OTHER_USER_ID)),
        (handle_wizard_button, _callback_update("wizard:qty:2", user_id=OTHER_USER_ID)),
        (
            handle_callback,
            _callback_update(
                encode_callback("pause", watch.watch_id), user_id=OTHER_USER_ID
            ),
        ),
    ]
    for handler, update in routes:
        with pytest.raises(InputError):
            await handler(update, harness.deps)
    assert await _status_of(harness, watch.watch_id) is WatchStatus.ACTIVE
    async with harness.database.connection() as conn:
        assert await harness.drafts.get(conn, OTHER_USER_ID) is None


async def test_denied_new_creates_no_draft(harness: Harness) -> None:
    with pytest.raises(InputError):
        await handle_new(_text_update("/new", user_id=OTHER_USER_ID), harness.deps)
    async with harness.database.connection() as conn:
        assert await harness.drafts.get(conn, OTHER_USER_ID) is None


async def test_denied_check_never_runs_a_check(harness: Harness) -> None:
    await _seed_watch(harness)
    with pytest.raises(InputError):
        await handle_check(_text_update("/check 1", user_id=OTHER_USER_ID), harness.deps)
    assert harness.gateway.calls == []


# ---------------------------------------------------------------------------
# /help and /watches
# ---------------------------------------------------------------------------


async def test_help_lists_every_command(harness: Harness) -> None:
    message = await handle_help(_text_update("/help"), harness.deps)
    for command in ("/new", "/watches", "/check", "/pause", "/resume", "/delete", "/cancel"):
        assert command in message.text


async def test_watches_lists_only_the_callers_watches(harness: Harness) -> None:
    await _seed_watch(harness, suffix=1, title="Mine")
    await _seed_watch(harness, suffix=2, owner=OTHER_USER_ID, title="Theirs")
    message = await handle_watches(_text_update("/watches"), harness.deps)
    assert "Mine" in message.text
    assert "Theirs" not in message.text


async def test_watches_reports_an_empty_list(harness: Harness) -> None:
    message = await handle_watches(_text_update("/watches"), harness.deps)
    assert "/new" in message.text


# ---------------------------------------------------------------------------
# /pause, /resume, /delete
# ---------------------------------------------------------------------------


async def test_pause_by_number_pauses_that_watch(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    await handle_pause(_text_update("/pause 1"), harness.deps)
    assert await _status_of(harness, watch.watch_id) is WatchStatus.PAUSED


async def test_pause_without_a_number_explains_the_usage(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    message = await handle_pause(_text_update("/pause"), harness.deps)
    assert "/pause" in message.text
    assert await _status_of(harness, watch.watch_id) is WatchStatus.ACTIVE


async def test_pause_out_of_range_changes_nothing(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    message = await handle_pause(_text_update("/pause 7"), harness.deps)
    assert "7" in message.text
    assert await _status_of(harness, watch.watch_id) is WatchStatus.ACTIVE


async def test_numbering_only_covers_the_callers_own_watches(harness: Harness) -> None:
    theirs = await _seed_watch(harness, suffix=1, owner=OTHER_USER_ID)
    mine = await _seed_watch(harness, suffix=2, owner=USER_ID)
    await handle_pause(_text_update("/pause 1"), harness.deps)
    assert await _status_of(harness, theirs.watch_id) is WatchStatus.ACTIVE
    assert await _status_of(harness, mine.watch_id) is WatchStatus.PAUSED


async def test_resume_reactivates_a_paused_recurring_watch(harness: Harness) -> None:
    watch = await _seed_watch(harness, status=WatchStatus.PAUSED)
    await handle_resume(_text_update("/resume 1"), harness.deps)
    assert await _status_of(harness, watch.watch_id) is WatchStatus.ACTIVE


async def test_resume_refuses_a_paused_one_off_watch(harness: Harness) -> None:
    """A one-off paused by a contract error is recovered by /check, never /resume."""
    watch = await _seed_watch(
        harness, status=WatchStatus.PAUSED, mode=WatchMode.ONE_OFF, interval=None
    )
    message = await handle_resume(_text_update("/resume 1"), harness.deps)
    assert "/check" in message.text
    assert await _status_of(harness, watch.watch_id) is WatchStatus.PAUSED


async def test_delete_asks_for_confirmation_first(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    message = await handle_delete(_text_update("/delete 1"), harness.deps)
    assert message.reply_markup is not None
    buttons = [
        button
        for row in message.reply_markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]
    assert encode_callback("delete_confirm", watch.watch_id) in {
        button.callback_data for button in buttons
    }
    assert await _status_of(harness, watch.watch_id) is WatchStatus.ACTIVE


async def test_delete_confirmation_callback_deletes_the_watch(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    await handle_callback(
        _callback_update(encode_callback("delete_confirm", watch.watch_id)), harness.deps
    )
    assert await _status_of(harness, watch.watch_id) is None


async def test_keeping_a_watch_leaves_it_in_place(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    message = await handle_callback(
        _callback_update(encode_callback("keep", watch.watch_id)), harness.deps
    )
    assert message is not None
    assert await _status_of(harness, watch.watch_id) is WatchStatus.ACTIVE


async def test_delete_confirmation_refuses_another_users_watch(harness: Harness) -> None:
    watch = await _seed_watch(harness, owner=OTHER_USER_ID)
    message = await handle_callback(
        _callback_update(encode_callback("delete_confirm", watch.watch_id)), harness.deps
    )
    assert message is not None
    assert await _status_of(harness, watch.watch_id) is WatchStatus.ACTIVE


# ---------------------------------------------------------------------------
# Action callbacks
# ---------------------------------------------------------------------------


async def test_pause_callback_pauses_and_returns_the_refreshed_list(harness: Harness) -> None:
    watch = await _seed_watch(harness, title="Dog Stars")
    message = await handle_callback(
        _callback_update(encode_callback("pause", watch.watch_id)), harness.deps
    )
    assert message is not None
    assert "Dog Stars" in message.text
    assert await _status_of(harness, watch.watch_id) is WatchStatus.PAUSED


async def test_duplicate_pause_callback_reports_a_stale_action(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    update = _callback_update(encode_callback("pause", watch.watch_id))
    await handle_callback(update, harness.deps)
    message = await handle_callback(update, harness.deps)
    assert message is not None
    assert "/watches" in message.text
    assert await _status_of(harness, watch.watch_id) is WatchStatus.PAUSED


async def test_action_callback_refuses_another_users_watch(harness: Harness) -> None:
    watch = await _seed_watch(harness, owner=OTHER_USER_ID)
    message = await handle_callback(
        _callback_update(encode_callback("pause", watch.watch_id)), harness.deps
    )
    assert message is not None
    assert await _status_of(harness, watch.watch_id) is WatchStatus.ACTIVE


async def test_malformed_callback_data_is_reported_benignly(harness: Harness) -> None:
    message = await handle_callback(_callback_update("not-a-callback"), harness.deps)
    assert message is not None
    assert "/watches" in message.text


async def test_callback_without_data_is_ignored(harness: Harness) -> None:
    assert await handle_callback(_callback_update(""), harness.deps) is None


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


async def test_page_callback_renders_the_requested_page(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    snapshot_id = await _seed_snapshot(harness, watch, 12)
    message = await handle_callback(
        _callback_update(encode_callback("page", snapshot_id, 2)), harness.deps
    )
    assert message is not None
    assert "L11-M11" in message.text
    assert "L0-M0" not in message.text


async def test_page_callback_reads_the_referenced_snapshot_not_the_latest(
    harness: Harness,
) -> None:
    watch = await _seed_watch(harness)
    first = await _seed_snapshot(harness, watch, 1)
    await _seed_snapshot(harness, watch, 3)
    message = await handle_callback(
        _callback_update(encode_callback("page", first, 1)), harness.deps
    )
    assert message is not None
    assert "1 of 1" in message.text or "1 option" in message.text


async def test_page_callback_for_an_unknown_snapshot_reports_expiry(harness: Harness) -> None:
    message = await handle_callback(
        _callback_update(encode_callback("page", uuid4(), 1)), harness.deps
    )
    assert message is not None
    assert "/check" in message.text


async def test_page_callback_refuses_another_users_snapshot(harness: Harness) -> None:
    watch = await _seed_watch(harness, owner=OTHER_USER_ID)
    snapshot_id = await _seed_snapshot(harness, watch, 2)
    message = await handle_callback(
        _callback_update(encode_callback("page", snapshot_id, 1)), harness.deps
    )
    assert message is not None
    assert "/check" in message.text
    assert "L0-M0" not in message.text


async def test_page_callback_beyond_the_last_page_is_reported_benignly(
    harness: Harness,
) -> None:
    watch = await _seed_watch(harness)
    snapshot_id = await _seed_snapshot(harness, watch, 2)
    message = await handle_callback(
        _callback_update(encode_callback("page", snapshot_id, 9)), harness.deps
    )
    assert message is not None
    assert "/check" in message.text


# ---------------------------------------------------------------------------
# /check
# ---------------------------------------------------------------------------


async def test_manual_check_preserves_a_recurring_watch_schedule(harness: Harness) -> None:
    scheduled = NOW + timedelta(minutes=30)
    watch = await _seed_watch(harness, next_check_at=scheduled)
    await handle_check(_text_update("/check 1"), harness.deps)
    async with harness.database.connection() as conn:
        stored = await harness.watch_repository.get(conn, watch.watch_id)
    assert stored is not None
    assert stored.next_check_at == scheduled
    assert stored.status is WatchStatus.ACTIVE


async def test_manual_check_delivers_its_results_immediately(harness: Harness) -> None:
    await _seed_watch(harness)
    reply = await handle_check(_text_update("/check 1"), harness.deps)
    assert reply is None  # the delivered results are the answer; do not say it twice
    assert len(harness.bot.sent) == 1
    async with harness.database.connection() as conn:
        cursor = await conn.execute("SELECT status FROM notification_deliveries")
        rows = await cursor.fetchall()
    assert [row["status"] for row in rows] == [DeliveryStatus.SENT.value]


async def test_manual_check_still_answers_when_delivery_fails(harness: Harness) -> None:
    """A check that succeeded but could not be delivered must not look like silence."""
    await _seed_watch(harness)
    harness.bot.error = TimedOut()
    reply = await handle_check(_text_update("/check 1"), harness.deps)
    assert reply is not None
    async with harness.database.connection() as conn:
        cursor = await conn.execute("SELECT status FROM notification_deliveries")
        rows = await cursor.fetchall()
    assert [row["status"] for row in rows] == [DeliveryStatus.PENDING.value]


async def test_manual_check_on_a_broken_page_pauses_the_watch_and_explains(
    harness: Harness,
) -> None:
    watch = await _seed_watch(harness)
    harness.gateway.error = BfiContractError("listing markup changed")
    reply = await handle_check(_text_update("/check 1"), harness.deps)
    assert reply is None
    text = harness.bot.sent[0]["text"]
    assert "Dog Stars" in text
    assert "/check" in text
    assert await _status_of(harness, watch.watch_id) is WatchStatus.PAUSED
    async with harness.database.connection() as conn:
        assert await harness.notifications.known_keys(conn, watch.watch_id) == frozenset()


async def test_manual_check_on_a_paused_one_off_watch_runs_and_completes_it(
    harness: Harness,
) -> None:
    watch = await _seed_watch(
        harness,
        status=WatchStatus.PAUSED,
        mode=WatchMode.ONE_OFF,
        interval=None,
        next_check_at=None,
    )
    await handle_check(_text_update("/check 1"), harness.deps)
    assert harness.gateway.calls == ["dog-stars"]
    assert await _status_of(harness, watch.watch_id) is WatchStatus.COMPLETED


async def test_manual_check_refuses_a_paused_recurring_watch(harness: Harness) -> None:
    watch = await _seed_watch(harness, status=WatchStatus.PAUSED)
    message = await handle_check(_text_update("/check 1"), harness.deps)
    assert message is not None
    assert "/resume" in message.text
    assert harness.gateway.calls == []
    assert await _status_of(harness, watch.watch_id) is WatchStatus.PAUSED


async def test_manual_check_reports_a_superseded_watch(harness: Harness) -> None:
    await _seed_watch(harness)
    deps = harness.with_checks(_RaisingChecks(ConflictError("watch changed mid-check")))
    message = await handle_check(_text_update("/check 1"), deps)
    assert message is not None
    lowered = message.text.lower()
    assert "changed" in lowered or "superseded" in lowered
    assert "/watches" in message.text


async def test_manual_check_reports_a_network_failure(harness: Harness) -> None:
    await _seed_watch(harness)
    harness.gateway.error = BfiNetworkError("host unreachable")
    message = await handle_check(_text_update("/check 1"), harness.deps)
    assert message is not None
    assert "reach" in message.text.lower() or "network" in message.text.lower()


async def test_check_out_of_range_changes_nothing(harness: Harness) -> None:
    message = await handle_check(_text_update("/check 3"), harness.deps)
    assert message is not None
    assert harness.gateway.calls == []


async def test_check_flushes_deliveries_once(harness: Harness) -> None:
    await _seed_watch(harness)
    dispatcher = _RecordingDispatcher()
    deps = CommandDeps(
        wizard=harness.deps.wizard,
        results=harness.results,
        deliveries=dispatcher,
        allowed_user_ids=harness.deps.allowed_user_ids,
    )
    await handle_check(_text_update("/check 1"), deps)
    assert dispatcher.runs == 1


def _with_dispatcher(harness: Harness, dispatcher: Any) -> CommandDeps:
    return CommandDeps(
        wizard=harness.deps.wizard,
        results=harness.results,
        deliveries=dispatcher,
        allowed_user_ids=harness.deps.allowed_user_ids,
    )


async def test_check_stays_quiet_when_its_own_delivery_was_the_one_sent(
    harness: Harness,
) -> None:
    watch = await _seed_watch(harness)
    deps = _with_dispatcher(harness, _RecordingDispatcher(_sent_run(watch_id=watch.watch_id)))

    assert await handle_check(_text_update("/check 1"), deps) is None


async def test_check_still_answers_when_someone_elses_delivery_was_sent(
    harness: Harness,
) -> None:
    await _seed_watch(harness)
    other = await _seed_watch(harness, suffix=2, owner=OTHER_USER_ID, title="Theirs")
    deps = _with_dispatcher(
        harness,
        _RecordingDispatcher(_sent_run(watch_id=other.watch_id, user_id=OTHER_USER_ID)),
    )

    message = await handle_check(_text_update("/check 1"), deps)

    assert message is not None
    assert "Theirs" not in message.text


async def test_check_still_answers_when_a_host_alert_was_the_only_thing_sent(
    harness: Harness,
) -> None:
    await _seed_watch(harness)
    deps = _with_dispatcher(harness, _RecordingDispatcher(_sent_run(watch_id=None)))

    assert await handle_check(_text_update("/check 1"), deps) is not None


async def test_check_still_answers_when_its_own_delivery_was_only_retried(
    harness: Harness,
) -> None:
    watch = await _seed_watch(harness)
    run = DeliveryRun(
        attempts=(
            DeliveryAttempt(
                delivery_id=uuid4(),
                idempotency_key="results:1",
                recipient_user_id=USER_ID,
                watch_id=watch.watch_id,
                outcome=DeliveryAttemptOutcome.RETRIED,
            ),
        )
    )
    deps = _with_dispatcher(harness, _RecordingDispatcher(run))

    assert await handle_check(_text_update("/check 1"), deps) is not None


async def test_check_on_a_watch_deleted_mid_flight_answers_safely(harness: Harness) -> None:
    await _seed_watch(harness)
    deps = harness.with_checks(_RaisingChecks(InputError("unknown watch")))

    message = await handle_check(_text_update("/check 1"), deps)

    assert message is not None
    assert message.text == STALE_ACTION


async def test_pause_on_a_watch_deleted_mid_flight_answers_safely(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    original = harness.watches.pause

    async def racing_pause(user_id: int, watch_id: UUID) -> Any:
        await harness.watches.delete(USER_ID, watch.watch_id)
        return await original(user_id, watch_id)

    harness.watches.pause = racing_pause  # type: ignore[method-assign]

    message = await handle_pause(_text_update("/pause 1"), harness.deps)

    assert message.text == STALE_ACTION


async def test_a_page_callback_survives_a_repository_input_error(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    snapshot_id = await _seed_snapshot(harness, watch, 3)

    async def failing_lookup(conn: Any, requested: UUID) -> UUID | None:
        raise InputError(f"snapshot {requested} is malformed")

    harness.results.snapshot_watch_id = failing_lookup  # type: ignore[method-assign]

    message = await handle_callback(
        _callback_update(encode_callback("page", snapshot_id, 2)), harness.deps
    )

    assert message is not None
    assert message.text == RESULTS_GONE
    assert str(snapshot_id) not in message.text


# ---------------------------------------------------------------------------
# Wizard routes
# ---------------------------------------------------------------------------


async def test_new_starts_the_wizard(harness: Harness) -> None:
    await handle_new(_text_update("/new"), harness.deps)
    async with harness.database.connection() as conn:
        draft = await harness.drafts.get(conn, USER_ID)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_URL.value


async def test_cancel_discards_a_draft(harness: Harness) -> None:
    await handle_new(_text_update("/new"), harness.deps)
    await handle_cancel(_text_update("/cancel"), harness.deps)
    async with harness.database.connection() as conn:
        assert await harness.drafts.get(conn, USER_ID) is None


async def test_cancel_refuses_to_discard_a_live_confirmation(harness: Harness) -> None:
    async with harness.database.connection() as conn:
        await harness.drafts.upsert(
            conn, USER_ID, WizardState.CONFIRMING.value, {"setup_id": "abc"}, NOW
        )
    message = await handle_cancel(_text_update("/cancel"), harness.deps)
    assert "still" in message.text.lower()
    async with harness.database.connection() as conn:
        draft = await harness.drafts.get(conn, USER_ID)
    assert draft is not None
    assert draft.state == WizardState.CONFIRMING.value


async def test_cancel_discards_an_abandoned_confirmation(harness: Harness) -> None:
    async with harness.database.connection() as conn:
        await harness.drafts.upsert(
            conn,
            USER_ID,
            WizardState.CONFIRMING.value,
            {"setup_id": "abc"},
            NOW - timedelta(hours=1),
        )
    message = await handle_cancel(_text_update("/cancel"), harness.deps)
    assert "/watches" in message.text
    async with harness.database.connection() as conn:
        assert await harness.drafts.get(conn, USER_ID) is None


async def test_text_without_a_draft_offers_help(harness: Harness) -> None:
    message = await handle_text(_text_update("hello"), harness.deps)
    assert message is not None
    assert "/help" in message.text


async def test_text_advances_an_active_draft(harness: Harness) -> None:
    await handle_new(_text_update("/new"), harness.deps)
    message = await handle_text(_text_update(FILM_URL), harness.deps)
    assert message is not None
    async with harness.database.connection() as conn:
        draft = await harness.drafts.get(conn, USER_ID)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_DATE_RANGE.value


async def test_wizard_button_advances_a_draft(harness: Harness) -> None:
    await handle_new(_text_update("/new"), harness.deps)
    await handle_text(_text_update(FILM_URL), harness.deps)
    await handle_text(_text_update("2026-08-26 to 2026-08-30"), harness.deps)
    await handle_wizard_button(_callback_update("wizard:time-mode:same"), harness.deps)
    await handle_text(_text_update("18:00 to 23:00"), harness.deps)
    message = await handle_wizard_button(_callback_update("wizard:qty:2"), harness.deps)
    assert message is not None
    async with harness.database.connection() as conn:
        draft = await harness.drafts.get(conn, USER_ID)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_SEAT_MODE.value
