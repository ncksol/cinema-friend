"""Tests for cinema_friend.telegram.bot.

The delivery worker is the only component that turns a queued row into a message a
person actually sees, so every test here asserts on the *database* after the attempt:
whether an option counts as "known" is what decides if the owner is ever told about it
again, and a worker that sends nothing but marks everything known silently drops
results forever.

Failures are split by permanence on purpose. A timeout is a bad minute and must be
retried; a ``Forbidden`` is a blocked bot and retrying it forever would keep a dead
delivery in the queue for the lifetime of the process.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from telegram import Chat, Message, Update, User
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler

from cinema_friend.config import Settings
from cinema_friend.domain.bfi import Performance
from cinema_friend.domain.results import (
    CheckResult,
    DeliveryStatus,
    NotificationPayload,
    RankedOption,
    RankVector,
)
from cinema_friend.domain.state import CheckOutcome, CheckTrigger, WatchMode, WatchStatus
from cinema_friend.domain.watch import Watch, WatchCriteria
from cinema_friend.services.watch_service import WatchService
from cinema_friend.storage.database import Database
from cinema_friend.storage.draft_repository import DraftRepository
from cinema_friend.storage.notification_repository import NotificationRepository
from cinema_friend.storage.result_repository import ResultRepository
from cinema_friend.storage.watch_repository import WatchRepository
from cinema_friend.telegram.bot import (
    RETRY_DELAYS,
    BotDependencies,
    DeliveryWorker,
    build_telegram_application,
)
from cinema_friend.telegram.rendering import MAX_MESSAGE_CHARS
from tests.fakes import FakeClock

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
USER_ID = 42
WATCH_ID = UUID("00000000-0000-4000-8000-000000000001")


class _Bot:
    """Records sends; raises whatever is queued in ``errors`` first."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.errors: list[BaseException] = []
        self.delay = 0.0

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: object = None,
    ) -> object:
        await asyncio.sleep(self.delay)
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
            }
        )
        return object()


class _Checks:
    async def check(self, watch_id: UUID, trigger: CheckTrigger) -> CheckResult:
        raise AssertionError("no test here runs a check")


@dataclass
class Harness:
    database: Database
    clock: FakeClock
    bot: _Bot
    notifications: NotificationRepository
    results: ResultRepository
    watches: WatchService
    watch_repository: WatchRepository
    worker: DeliveryWorker


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
    bot = _Bot()
    worker = DeliveryWorker(
        bot=bot,
        database=database,
        notifications=notifications,
        results=results,
        watches=watches,
        clock=clock,
    )
    return Harness(
        database=database,
        clock=clock,
        bot=bot,
        notifications=notifications,
        results=results,
        watches=watches,
        watch_repository=watch_repository,
        worker=worker,
    )


def _criteria(**overrides: Any) -> WatchCriteria:
    defaults: dict[str, Any] = {
        "source_url": "https://whatson.bfi.org.uk/imax/Online/article/dog-stars",
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
    watch_id: UUID = WATCH_ID,
    status: WatchStatus = WatchStatus.ACTIVE,
    title: str | None = "Dog Stars",
) -> Watch:
    watch = Watch(
        watch_id=watch_id,
        user_id=USER_ID,
        criteria=_criteria(),
        status=status,
        created_at=NOW,
        updated_at=NOW,
        next_check_at=NOW,
        title=title,
    )
    async with harness.database.connection() as conn:
        await harness.watch_repository.create(conn, watch)
    return watch


def _option(index: int) -> RankedOption:
    start = datetime(2026, 8, 26, 18, 0, tzinfo=UTC) + timedelta(minutes=index)
    seat_ids = (f"seat-{index}a", f"seat-{index}b")
    performance = Performance(
        performance_id=f"perf-{index}",
        event_id="event-1",
        start_utc=start,
        sales_status_code="OPEN*",
        availability_code="A",
        availability_num=50,
        reserved_seating=True,
        seat_map_url="https://whatson.bfi.org.uk/imax/Online/mapSelect.asp?ID=1",
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
        title="Dog Stars",
    )


async def _seed_snapshot(harness: Harness, watch_id: UUID, count: int) -> tuple[UUID, list[str]]:
    options = [_option(index) for index in range(count)]
    async with harness.database.connection() as conn:
        check_run_id = await harness.results.start_check(
            conn, watch_id=watch_id, trigger=CheckTrigger.SCHEDULED, started_at=NOW
        )
        snapshot = await harness.results.complete_with_snapshot(
            conn,
            check_run_id=check_run_id,
            watch_id=watch_id,
            options=options,
            checked_at=NOW,
            outcome=CheckOutcome.SUCCESS,
            performance_count=count,
        )
    return snapshot.snapshot_id, [option.key for option in options]


async def _queue(
    harness: Harness,
    payload: NotificationPayload,
    *,
    key: str | None = None,
    when: datetime | None = None,
) -> UUID:
    async with harness.database.connection() as conn:
        delivery = await harness.notifications.create_delivery(
            conn, key or f"key-{uuid4()}", payload, when or NOW
        )
    return delivery.delivery_id


def _results_payload(snapshot_id: UUID, *, watch_id: UUID = WATCH_ID, new: int = 3) -> (
    NotificationPayload
):
    return NotificationPayload(
        kind="results",
        recipient_user_id=USER_ID,
        watch_id=watch_id,
        snapshot_id=snapshot_id,
        new_option_count=new,
        host=None,
        recovery_text=None,
    )


async def _delivery_row(harness: Harness, delivery_id: UUID) -> Any:
    async with harness.database.connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM notification_deliveries WHERE id = ?", (str(delivery_id),)
        )
        return await cursor.fetchone()


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------


async def test_successful_delivery_marks_every_snapshot_option_known(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    snapshot_id, keys = await _seed_snapshot(harness, watch.watch_id, 12)
    delivery_id = await _queue(harness, _results_payload(snapshot_id))

    run = await harness.worker.run_once()

    assert run.sent == 1
    assert len(harness.bot.sent) == 1
    async with harness.database.connection() as conn:
        known = await harness.notifications.known_keys(conn, watch.watch_id)
        state = await harness.notifications.state(conn, watch.watch_id)
    assert known == frozenset(keys)
    assert state.last_best_rank is not None
    assert state.last_best_rank.sort_key() == _option(0).rank_vector.sort_key()
    row = await _delivery_row(harness, delivery_id)
    assert row["status"] == DeliveryStatus.SENT.value


async def test_successful_delivery_renders_the_referenced_snapshot(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    older, _ = await _seed_snapshot(harness, watch.watch_id, 1)
    await _seed_snapshot(harness, watch.watch_id, 5)
    await _queue(harness, _results_payload(older, new=1))

    await harness.worker.run_once()

    text = harness.bot.sent[0]["text"]
    assert "L0-M0" in text
    assert "L4-M4" not in text


async def test_delivery_goes_to_the_recipient(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    snapshot_id, _ = await _seed_snapshot(harness, watch.watch_id, 1)
    await _queue(harness, _results_payload(snapshot_id))

    await harness.worker.run_once()

    assert harness.bot.sent[0]["chat_id"] == USER_ID


async def test_deliveries_not_yet_due_are_left_alone(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    snapshot_id, _ = await _seed_snapshot(harness, watch.watch_id, 1)
    await _queue(harness, _results_payload(snapshot_id), when=NOW + timedelta(minutes=5))

    run = await harness.worker.run_once()

    assert run.attempted == 0
    assert harness.bot.sent == []


async def test_sent_deliveries_are_not_sent_twice(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    snapshot_id, _ = await _seed_snapshot(harness, watch.watch_id, 2)
    await _queue(harness, _results_payload(snapshot_id))

    await harness.worker.run_once()
    await harness.worker.run_once()

    assert len(harness.bot.sent) == 1


async def test_concurrent_runs_do_not_double_send(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    snapshot_id, _ = await _seed_snapshot(harness, watch.watch_id, 2)
    await _queue(harness, _results_payload(snapshot_id))
    harness.bot.delay = 0.01

    await asyncio.gather(harness.worker.run_once(), harness.worker.run_once())

    assert len(harness.bot.sent) == 1


# ---------------------------------------------------------------------------
# Transient failure
# ---------------------------------------------------------------------------


async def test_failed_delivery_reschedules_and_marks_nothing_known(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    snapshot_id, _ = await _seed_snapshot(harness, watch.watch_id, 3)
    delivery_id = await _queue(harness, _results_payload(snapshot_id))
    harness.bot.errors = [TimedOut()]

    run = await harness.worker.run_once()

    assert run.retried == 1
    assert run.sent == 0
    row = await _delivery_row(harness, delivery_id)
    assert row["status"] == DeliveryStatus.PENDING.value
    assert row["attempt_count"] == 1
    async with harness.database.connection() as conn:
        assert await harness.notifications.known_keys(conn, watch.watch_id) == frozenset()
        assert (await harness.notifications.state(conn, watch.watch_id)).last_best_rank is None


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [
        (0, timedelta(minutes=1)),
        (1, timedelta(minutes=5)),
        (2, timedelta(minutes=15)),
        (3, timedelta(hours=1)),
        (9, timedelta(hours=1)),
    ],
)
async def test_retry_delay_backs_off_then_caps(
    harness: Harness, attempts: int, expected: timedelta
) -> None:
    watch = await _seed_watch(harness)
    snapshot_id, _ = await _seed_snapshot(harness, watch.watch_id, 1)
    delivery_id = await _queue(harness, _results_payload(snapshot_id))
    async with harness.database.connection() as conn:
        await conn.execute(
            "UPDATE notification_deliveries SET attempt_count = ? WHERE id = ?",
            (attempts, str(delivery_id)),
        )
        await conn.commit()
    harness.bot.errors = [TimedOut()]

    await harness.worker.run_once()

    row = await _delivery_row(harness, delivery_id)
    assert row["next_attempt_at"].startswith((NOW + expected).isoformat()[:16])


def test_retry_delays_are_one_five_fifteen_minutes_then_an_hour() -> None:
    assert RETRY_DELAYS == (
        timedelta(minutes=1),
        timedelta(minutes=5),
        timedelta(minutes=15),
        timedelta(hours=1),
    )


async def test_retry_after_is_honoured_when_longer_than_the_step(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    snapshot_id, _ = await _seed_snapshot(harness, watch.watch_id, 1)
    delivery_id = await _queue(harness, _results_payload(snapshot_id))
    harness.bot.errors = [RetryAfter(600)]

    await harness.worker.run_once()

    row = await _delivery_row(harness, delivery_id)
    assert row["status"] == DeliveryStatus.PENDING.value
    assert row["next_attempt_at"].startswith((NOW + timedelta(seconds=600)).isoformat()[:16])


# ---------------------------------------------------------------------------
# Permanent failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [Forbidden("bot was blocked by the user"), BadRequest("chat not found")],
)
async def test_permanent_failures_mark_the_delivery_failed(
    harness: Harness, error: BaseException
) -> None:
    watch = await _seed_watch(harness)
    snapshot_id, _ = await _seed_snapshot(harness, watch.watch_id, 2)
    delivery_id = await _queue(harness, _results_payload(snapshot_id))
    harness.bot.errors = [error]

    run = await harness.worker.run_once()

    assert run.failed == 1
    row = await _delivery_row(harness, delivery_id)
    assert row["status"] == DeliveryStatus.FAILED.value
    async with harness.database.connection() as conn:
        assert await harness.notifications.known_keys(conn, watch.watch_id) == frozenset()


async def test_an_unknown_kind_is_failed_rather_than_retried_forever(harness: Harness) -> None:
    await _seed_watch(harness)
    payload = NotificationPayload(
        kind="mystery",
        recipient_user_id=USER_ID,
        watch_id=WATCH_ID,
        snapshot_id=None,
        new_option_count=0,
        host=None,
        recovery_text=None,
    )
    delivery_id = await _queue(harness, payload)

    run = await harness.worker.run_once()

    assert run.failed == 1
    assert harness.bot.sent == []
    row = await _delivery_row(harness, delivery_id)
    assert row["status"] == DeliveryStatus.FAILED.value


# ---------------------------------------------------------------------------
# Expired snapshots
# ---------------------------------------------------------------------------


async def test_a_pruned_snapshot_still_sends_a_truthful_message(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    snapshot_id, _ = await _seed_snapshot(harness, watch.watch_id, 2)
    delivery_id = await _queue(harness, _results_payload(snapshot_id))
    async with harness.database.connection() as conn:
        await conn.execute("DELETE FROM result_snapshots WHERE id = ?", (str(snapshot_id),))
        await conn.commit()

    run = await harness.worker.run_once()

    assert run.sent == 1
    assert "/check" in harness.bot.sent[0]["text"]
    row = await _delivery_row(harness, delivery_id)
    assert row["status"] == DeliveryStatus.SENT.value
    async with harness.database.connection() as conn:
        assert await harness.notifications.known_keys(conn, watch.watch_id) == frozenset()


# ---------------------------------------------------------------------------
# Contract errors, degradation, recovery
# ---------------------------------------------------------------------------


async def test_contract_error_explains_the_paused_watch_and_marks_no_options(
    harness: Harness,
) -> None:
    watch = await _seed_watch(harness, status=WatchStatus.PAUSED, title="Dog Stars")
    await _seed_snapshot(harness, watch.watch_id, 4)
    payload = NotificationPayload(
        kind="contract_error",
        recipient_user_id=USER_ID,
        watch_id=watch.watch_id,
        snapshot_id=None,
        new_option_count=0,
        host=None,
        recovery_text=None,
    )
    delivery_id = await _queue(harness, payload, key=f"contract_error:{watch.watch_id}")

    run = await harness.worker.run_once()

    assert run.sent == 1
    text = harness.bot.sent[0]["text"]
    assert "Dog Stars" in text
    assert "paused" in text.lower()
    assert "/check" in text
    row = await _delivery_row(harness, delivery_id)
    assert row["status"] == DeliveryStatus.SENT.value
    async with harness.database.connection() as conn:
        assert await harness.notifications.known_keys(conn, watch.watch_id) == frozenset()


async def test_contract_error_for_a_deleted_watch_still_sends(harness: Harness) -> None:
    payload = NotificationPayload(
        kind="contract_error",
        recipient_user_id=USER_ID,
        watch_id=None,
        snapshot_id=None,
        new_option_count=0,
        host=None,
        recovery_text=None,
    )
    await _queue(harness, payload)

    run = await harness.worker.run_once()

    assert run.sent == 1


async def test_contract_error_escapes_and_clips_a_hostile_title(harness: Harness) -> None:
    watch = await _seed_watch(
        harness,
        status=WatchStatus.PAUSED,
        title="<b>x</b>" + "y" * 500,
    )
    payload = NotificationPayload(
        kind="contract_error",
        recipient_user_id=USER_ID,
        watch_id=watch.watch_id,
        snapshot_id=None,
        new_option_count=0,
        host=None,
        recovery_text=None,
    )
    await _queue(harness, payload)

    await harness.worker.run_once()

    text = harness.bot.sent[0]["text"]
    assert "<b>x</b>y" not in text
    assert "&lt;b&gt;x&lt;/b&gt;" in text
    assert len(text) <= MAX_MESSAGE_CHARS


async def test_degradation_is_host_scoped_and_touches_no_watch_state(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    payload = NotificationPayload(
        kind="degradation",
        recipient_user_id=USER_ID,
        watch_id=None,
        snapshot_id=None,
        new_option_count=0,
        host="whatson.bfi.org.uk",
        recovery_text=None,
    )
    await _queue(harness, payload)

    run = await harness.worker.run_once()

    assert run.sent == 1
    assert "whatson.bfi.org.uk" in harness.bot.sent[0]["text"]
    async with harness.database.connection() as conn:
        state = await harness.notifications.state(conn, watch.watch_id)
    assert state.degradation_notified is False


async def test_recovery_reports_the_hosts_return(harness: Harness) -> None:
    payload = NotificationPayload(
        kind="recovery",
        recipient_user_id=USER_ID,
        watch_id=None,
        snapshot_id=None,
        new_option_count=0,
        host="whatson.bfi.org.uk",
        recovery_text="back online",
    )
    await _queue(harness, payload)

    run = await harness.worker.run_once()

    assert run.sent == 1
    assert "back online" in harness.bot.sent[0]["text"]


async def test_run_once_reports_every_outcome(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    snapshot_id, _ = await _seed_snapshot(harness, watch.watch_id, 1)
    await _queue(harness, _results_payload(snapshot_id), key="a")
    await _queue(harness, _results_payload(snapshot_id), key="b")
    await _queue(harness, _results_payload(snapshot_id), key="c")
    harness.bot.errors = [TimedOut(), Forbidden("blocked")]

    run = await harness.worker.run_once()

    assert (run.attempted, run.sent, run.retried, run.failed) == (3, 1, 1, 1)


# ---------------------------------------------------------------------------
# Application wiring
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="123456:AAHfakefakefakefakefakefakefakefake",
        allowed_user_ids=frozenset({USER_ID}),
        database_path=tmp_path / "cinema.db",
    )


def _dependencies(harness: Harness) -> BotDependencies:
    return BotDependencies(
        database=harness.database,
        drafts=DraftRepository(),
        watches=harness.watches,
        checks=_Checks(),
        results=harness.results,
        notifications=harness.notifications,
        clock=harness.clock,
    )


def _registered(application: Application[Any, Any, Any, Any, Any, Any]) -> list[Any]:
    return list(application.handlers[0])


async def test_every_command_is_registered(harness: Harness, tmp_path: Path) -> None:
    application = build_telegram_application(_settings(tmp_path), _dependencies(harness))
    commands = {
        name
        for handler in _registered(application)
        if isinstance(handler, CommandHandler)
        for name in handler.commands
    }
    assert commands == {
        "start",
        "help",
        "new",
        "cancel",
        "watches",
        "check",
        "pause",
        "resume",
        "delete",
    }


async def test_commands_are_matched_before_free_text(harness: Harness, tmp_path: Path) -> None:
    application = build_telegram_application(_settings(tmp_path), _dependencies(harness))
    handlers = _registered(application)
    last_command = max(
        index for index, handler in enumerate(handlers) if isinstance(handler, CommandHandler)
    )
    first_message = min(
        index for index, handler in enumerate(handlers) if isinstance(handler, MessageHandler)
    )
    assert last_command < first_message


async def test_wizard_callbacks_are_matched_before_watch_callbacks(
    harness: Harness, tmp_path: Path
) -> None:
    application = build_telegram_application(_settings(tmp_path), _dependencies(harness))
    patterns = [
        handler.pattern.pattern
        for handler in _registered(application)
        if isinstance(handler, CallbackQueryHandler) and handler.pattern is not None
    ]
    assert patterns.index("^wizard:") < patterns.index("^v1:")


async def test_the_delivery_worker_is_published_for_the_scheduler(
    harness: Harness, tmp_path: Path
) -> None:
    application = build_telegram_application(_settings(tmp_path), _dependencies(harness))
    worker = application.bot_data["delivery_worker"]
    assert isinstance(worker, DeliveryWorker)


def _handler_for(
    application: Application[Any, Any, Any, Any, Any, Any], command: str
) -> CommandHandler[Any, Any, Any]:
    for handler in _registered(application):
        if isinstance(handler, CommandHandler) and command in handler.commands:
            return handler
    raise AssertionError(f"no handler for /{command}")


def _command_update(text: str, *, user_id: int) -> Update:
    user = User(id=user_id, first_name="Test", is_bot=False)
    chat = Chat(id=user_id, type="private")
    message = Message(message_id=1, date=NOW, chat=chat, from_user=user, text=text)
    return Update(update_id=1, message=message)


async def test_a_routed_command_sends_its_reply_to_the_chat(
    harness: Harness, tmp_path: Path
) -> None:
    application = build_telegram_application(_settings(tmp_path), _dependencies(harness))
    handler = _handler_for(application, "watches")
    bot = _Bot()

    await handler.callback(_command_update("/watches", user_id=USER_ID), SimpleNamespace(bot=bot))

    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == USER_ID


async def test_a_routed_command_answers_an_unlisted_user_with_silence(
    harness: Harness, tmp_path: Path
) -> None:
    application = build_telegram_application(_settings(tmp_path), _dependencies(harness))
    handler = _handler_for(application, "watches")
    bot = _Bot()

    await handler.callback(_command_update("/watches", user_id=999), SimpleNamespace(bot=bot))

    assert bot.sent == []
