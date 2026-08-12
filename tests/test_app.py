"""Tests for cinema_friend.app: configuration loading and process lifecycle.

Everything here is about the order things happen in and what survives when one of them
fails, because that is all a lifecycle is. The database, the repositories, and the
scheduler are real; the two things that would reach outside the process -- the BFI
session and the Telegram application -- are fakes that record what they were asked to
do, so a startup or shutdown sequence can be read back as a list.

The properties under test:

- Configuration: an env file that anyone else can read is rejected *before* it is
  parsed, so a bad deployment never gets as far as loading a token it should not have
  been holding in that file.
- Startup order: schema first, then exactly one shared BFI session, then Telegram
  initialised, then stale confirmations converged, and only then updates served.
- Shutdown: new work stops first, active checks get a bounded grace period, and every
  close step still runs when an earlier one throws.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sqlite3
import stat
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from cinema_friend.app import (
    SHUTDOWN_GRACE_SECONDS,
    CinemaFriendApp,
    install_signal_handlers,
    load_settings,
)
from cinema_friend.clock import SystemClock
from cinema_friend.config import Settings
from cinema_friend.domain.errors import AlreadyRunningError, InputError, PersistenceError
from cinema_friend.services.watch_service import WatchService
from cinema_friend.single_instance import lock_path_for
from cinema_friend.storage.database import Database
from cinema_friend.storage.draft_repository import DraftRepository
from cinema_friend.storage.notification_repository import NotificationRepository
from cinema_friend.storage.result_repository import ResultRepository
from cinema_friend.storage.watch_repository import WatchRepository
from cinema_friend.telegram.bot import BotDependencies
from cinema_friend.telegram.rendering import RenderedMessage
from cinema_friend.telegram.wizard import RecoveredConfirmation

TOKEN = "8012345678:AAF-secret-bot-token-value"

pytestmark = pytest.mark.usefixtures("no_leaked_tasks")

# One list, shared by every fake, so a whole startup or shutdown reads back in order.
ORDER: list[str] = []


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSession:
    """Stands in for the one shared ``curl_cffi`` session the transport owns."""

    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.closed = False

    async def get(self, url: str, **_kwargs: object) -> object:  # pragma: no cover - unused
        raise AssertionError("no HTTP request is expected during lifecycle tests")

    async def close(self) -> None:
        self._events.append("session.close")
        self.closed = True


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: object | None = None,
    ) -> None:
        self.sent.append((chat_id, text))


class FakeUpdater:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.running = False
        self.stop_error: BaseException | None = None

    async def start_polling(self) -> object:
        self._events.append("updater.start_polling")
        self.running = True
        return object()

    async def stop(self) -> None:
        self._events.append("updater.stop")
        self.running = False
        if self.stop_error is not None:
            raise self.stop_error


class FakeDeliveryWorker:
    def __init__(self) -> None:
        self.runs = 0

    async def run_once(self) -> object:
        self.runs += 1
        raise AssertionError("the fake scheduler never drives deliveries")


class FakeTelegramApplication:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.bot = FakeBot()
        self.updater = FakeUpdater(events)
        self.delivery_worker = FakeDeliveryWorker()
        self.bot_data: dict[str, Any] = {"delivery_worker": self.delivery_worker}
        self.initialize_error: BaseException | None = None
        self.initialized = False
        self.started = False

    async def initialize(self) -> None:
        self._events.append("telegram.initialize")
        if self.initialize_error is not None:
            raise self.initialize_error
        self.initialized = True

    async def start(self) -> None:
        self._events.append("telegram.start")
        self.started = True

    async def stop(self) -> None:
        self._events.append("telegram.stop")
        self.started = False

    async def shutdown(self) -> None:
        self._events.append("telegram.shutdown")
        self.initialized = False


class FakeScheduler:
    """Runs until the stop event fires, optionally ignoring it for a while."""

    def __init__(self, events: list[str], dependencies: Any) -> None:
        self._events = events
        self.dependencies = dependencies
        self.runs = 0
        self.stopped = False
        self.hang = False
        self.crash: BaseException | None = None

    async def run(self, stop_event: asyncio.Event) -> None:
        self._events.append("scheduler.run")
        self.runs += 1
        if self.crash is not None:
            raise self.crash
        await stop_event.wait()
        if self.hang:
            self._events.append("scheduler.hanging")
            await asyncio.Event().wait()
        self.stopped = True
        self._events.append("scheduler.stopped")


class Harness:
    """One app plus every fake it was built with, so tests can assert on both."""

    def __init__(
        self, settings: Settings, *, scheduler_crash: BaseException | None = None, **overrides: Any
    ) -> None:
        self.events = ORDER
        self.sessions: list[FakeSession] = []
        self.telegram = FakeTelegramApplication(self.events)
        self.scheduler: FakeScheduler | None = None
        self.scheduler_crash = scheduler_crash
        self.bot_dependencies: Any = None
        self.app = CinemaFriendApp(
            settings,
            session_factory=self._session,
            application_factory=self._application,
            scheduler_factory=self._scheduler,
            **overrides,
        )

    def _session(self, settings: Settings) -> FakeSession:
        self.events.append("session.create")
        session = FakeSession(self.events)
        self.sessions.append(session)
        return session

    def _application(self, settings: Settings, dependencies: Any) -> FakeTelegramApplication:
        self.events.append("telegram.build")
        self.bot_dependencies = dependencies
        return self.telegram

    def _scheduler(self, dependencies: Any) -> FakeScheduler:
        self.events.append("scheduler.build")
        scheduler = FakeScheduler(self.events, dependencies)
        scheduler.crash = self.scheduler_crash
        self.scheduler = scheduler
        return scheduler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "cinema-friend.db"


@pytest.fixture
def env_file(tmp_path: Path, database_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(
        f"TELEGRAM_BOT_TOKEN={TOKEN}\n"
        "TELEGRAM_ALLOWED_USER_IDS=11,12\n"
        f"DATABASE_PATH={database_path}\n"
        "LOG_LEVEL=debug\n"
    )
    path.chmod(0o600)
    return path


@pytest.fixture
def settings(database_path: Path) -> Settings:
    return Settings(
        telegram_bot_token=TOKEN,
        allowed_user_ids=frozenset({11}),
        database_path=database_path,
    )


@pytest.fixture
def recovered(monkeypatch: pytest.MonkeyPatch) -> list[RecoveredConfirmation]:
    """Replace the wizard's recovery pass with a recorder the tests control."""
    pending: list[RecoveredConfirmation] = []

    async def fake_recover(deps: object) -> tuple[RecoveredConfirmation, ...]:
        ORDER.append("recover")
        return tuple(pending)

    monkeypatch.setattr("cinema_friend.app.recover_confirmations", fake_recover)
    return pending


@pytest.fixture(autouse=True)
def _record_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put schema migration into the same ordered list as everything else."""
    original = Database.migrate

    async def spy(self: Database, conn: Any) -> Any:
        ORDER.append("migrate")
        return await original(self, conn)

    monkeypatch.setattr(Database, "migrate", spy)


@pytest.fixture(autouse=True)
def _reset_order() -> Iterator[None]:
    ORDER.clear()
    yield
    ORDER.clear()


@pytest.fixture
def harness(settings: Settings, recovered: list[RecoveredConfirmation]) -> Harness:
    return Harness(settings)


def make_harness(settings: Settings, **overrides: Any) -> Harness:
    return Harness(settings, **overrides)


async def started(harness: Harness) -> Harness:
    await harness.app.start()
    return harness


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------


def test_load_settings_reads_a_private_env_file(env_file: Path, database_path: Path) -> None:
    loaded = load_settings(env_file)

    assert loaded.telegram_bot_token == TOKEN
    assert loaded.allowed_user_ids == frozenset({11, 12})
    assert loaded.database_path == database_path
    assert loaded.log_level == "DEBUG"


def test_load_settings_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="does not exist"):
        load_settings(tmp_path / "absent.env")


def test_load_settings_rejects_a_directory(tmp_path: Path) -> None:
    with pytest.raises(InputError):
        load_settings(tmp_path)


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o666, 0o700 | stat.S_IRGRP])
def test_load_settings_rejects_group_or_world_readable_files(env_file: Path, mode: int) -> None:
    env_file.chmod(mode)

    with pytest.raises(InputError, match="0600"):
        load_settings(env_file)


def test_load_settings_rejects_a_file_owned_by_someone_else(
    env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "getuid", lambda: os.stat(env_file).st_uid + 1)

    with pytest.raises(InputError, match="owned"):
        load_settings(env_file)


def test_load_settings_checks_the_file_before_parsing_any_secret(
    env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Permissions are a property of the file, so they are checked without reading it."""
    parsed: list[Path] = []

    def spy(path: Any) -> dict[str, str]:
        parsed.append(Path(path))
        return {}

    monkeypatch.setattr("cinema_friend.app.dotenv_values", spy)
    env_file.chmod(0o644)

    with pytest.raises(InputError):
        load_settings(env_file)
    assert parsed == []


def test_load_settings_reports_a_missing_required_value(tmp_path: Path) -> None:
    path = tmp_path / "incomplete.env"
    path.write_text("TELEGRAM_ALLOWED_USER_IDS=11\n")
    path.chmod(0o600)

    with pytest.raises(InputError):
        load_settings(path)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


async def test_start_migrates_the_database_before_anything_reaches_outside(
    harness: Harness, database_path: Path
) -> None:
    await harness.app.start()
    try:
        with sqlite3.connect(database_path) as raw:
            tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master")}
        assert "watches" in tables
        order = harness.events
        assert order.index("migrate") < order.index("session.create")
        assert order.index("session.create") < order.index("telegram.build")
    finally:
        await harness.app.stop()


async def test_start_creates_the_database_directory(harness: Harness, database_path: Path) -> None:
    assert not database_path.parent.exists()

    await harness.app.start()
    try:
        assert database_path.exists()
    finally:
        await harness.app.stop()


async def test_start_builds_exactly_one_shared_bfi_session(harness: Harness) -> None:
    await harness.app.start()
    try:
        assert len(harness.sessions) == 1
        assert harness.events.count("session.create") == 1
    finally:
        await harness.app.stop()


async def test_start_converges_stale_confirmations_before_serving_updates(
    harness: Harness,
) -> None:
    await harness.app.start()
    try:
        order = harness.events
        assert order.index("telegram.initialize") < order.index("recover")
        assert order.index("recover") < order.index("telegram.start")
        assert order.index("telegram.start") < order.index("updater.start_polling")
        assert order.count("recover") == 1
    finally:
        await harness.app.stop()


async def test_start_tells_each_recovered_owner_what_happened(
    settings: Settings, recovered: list[RecoveredConfirmation]
) -> None:
    recovered.append(
        RecoveredConfirmation(
            user_id=11,
            message=RenderedMessage(text="Watch created.", parse_mode="HTML", reply_markup=None),
        )
    )
    harness = make_harness(settings)

    await harness.app.start()
    try:
        assert harness.telegram.bot.sent == [(11, "Watch created.")]
    finally:
        await harness.app.stop()


async def test_a_failed_recovery_message_does_not_stop_startup(
    settings: Settings, recovered: list[RecoveredConfirmation], caplog: pytest.LogCaptureFixture
) -> None:
    """A user we cannot reach is not a reason to leave every other watch unserved."""
    recovered.append(
        RecoveredConfirmation(
            user_id=11,
            message=RenderedMessage(text="Watch created.", parse_mode="HTML", reply_markup=None),
        )
    )
    harness = make_harness(settings)

    async def explode(**_kwargs: object) -> None:
        raise RuntimeError("blocked by user")

    harness.telegram.bot.send_message = explode  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING, logger="cinema_friend.app"):
        await harness.app.start()
    try:
        assert "updater.start_polling" in harness.events
        assert caplog.records
    finally:
        await harness.app.stop()


async def test_start_gives_the_scheduler_the_bot_bound_delivery_worker(harness: Harness) -> None:
    await harness.app.start()
    try:
        assert harness.scheduler is not None
        assert harness.scheduler.dependencies.deliveries is harness.telegram.delivery_worker
    finally:
        await harness.app.stop()


async def test_start_refuses_to_run_twice(harness: Harness) -> None:
    await harness.app.start()
    try:
        with pytest.raises(RuntimeError, match="already"):
            await harness.app.start()
    finally:
        await harness.app.stop()


async def test_start_rejects_a_sqlite_too_old_for_snapshot_paging(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.37.2")

    with pytest.raises(PersistenceError, match="3.38"):
        await harness.app.start()

    assert harness.events == []
    await harness.app.stop()


async def test_start_closes_the_session_when_telegram_cannot_even_be_built(
    settings: Settings, recovered: list[RecoveredConfirmation]
) -> None:
    """The session exists before anything owns it, so that window has to be covered."""
    harness = Harness(settings)

    def explode(_settings: Settings, _dependencies: Any) -> FakeTelegramApplication:
        raise RuntimeError("no token")

    harness.app._application_factory = explode  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="no token"):
        await harness.app.start()

    assert harness.sessions[0].closed


async def test_start_cleans_up_when_a_later_step_fails(harness: Harness) -> None:
    """A half-built process must not leak the session, the task, or a polling updater."""
    harness.telegram.initialize_error = RuntimeError("bad token")

    with pytest.raises(RuntimeError, match="bad token"):
        await harness.app.start()

    assert harness.sessions[0].closed
    assert not harness.telegram.updater.running
    assert "updater.start_polling" not in harness.events
    assert "telegram.shutdown" in harness.events
    assert harness.scheduler is None


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


async def test_stop_unwinds_startup_in_reverse(harness: Harness) -> None:
    await harness.app.start()

    await harness.app.stop()

    order = harness.events
    assert harness.scheduler is not None
    assert harness.scheduler.stopped
    assert order.index("scheduler.stopped") < order.index("updater.stop")
    assert order.index("updater.stop") < order.index("telegram.stop")
    assert order.index("telegram.stop") < order.index("telegram.shutdown")
    assert order.index("telegram.shutdown") < order.index("session.close")


async def test_stop_waits_for_active_checks_only_up_to_the_grace_period(
    settings: Settings, recovered: list[RecoveredConfirmation], caplog: pytest.LogCaptureFixture
) -> None:
    harness = make_harness(settings, shutdown_grace_seconds=0.05)
    await harness.app.start()
    assert harness.scheduler is not None
    harness.scheduler.hang = True

    with caplog.at_level(logging.WARNING, logger="cinema_friend.app"):
        await harness.app.stop()

    assert "scheduler.hanging" in harness.events
    assert not harness.scheduler.stopped
    assert "session.close" in harness.events
    assert any("grace" in record.getMessage().lower() for record in caplog.records)


def test_the_specified_grace_period_is_thirty_seconds() -> None:
    assert SHUTDOWN_GRACE_SECONDS == 30.0


async def test_stop_closes_everything_even_when_one_step_fails(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    await harness.app.start()
    harness.telegram.updater.stop_error = RuntimeError("updater already gone")

    with caplog.at_level(logging.ERROR, logger="cinema_friend.app"):
        await harness.app.stop()

    assert "telegram.stop" in harness.events
    assert "telegram.shutdown" in harness.events
    assert harness.sessions[0].closed
    assert caplog.records


async def test_stop_survives_a_scheduler_that_crashed(
    settings: Settings, recovered: list[RecoveredConfirmation]
) -> None:
    harness = make_harness(settings, scheduler_crash=RuntimeError("loop died"))
    await harness.app.start()

    await harness.app.stop()

    assert harness.sessions[0].closed


async def test_stop_is_safe_before_start_and_repeatable(harness: Harness) -> None:
    await harness.app.stop()
    await harness.app.start()
    await harness.app.stop()
    await harness.app.stop()

    assert harness.events.count("telegram.shutdown") == 1
    assert harness.events.count("session.close") == 1


async def test_the_app_can_be_started_again_after_stopping(
    settings: Settings, recovered: list[RecoveredConfirmation]
) -> None:
    harness = make_harness(settings)
    await harness.app.start()
    await harness.app.stop()

    await harness.app.start()
    try:
        assert harness.events.count("updater.start_polling") == 2
        assert len(harness.sessions) == 2
    finally:
        await harness.app.stop()


# ---------------------------------------------------------------------------
# Waiting and signals
# ---------------------------------------------------------------------------


async def test_wait_returns_once_a_stop_is_requested(harness: Harness) -> None:
    await harness.app.start()
    try:
        waiter = asyncio.create_task(harness.app.wait())
        await asyncio.sleep(0)
        assert not waiter.done()

        harness.app.request_stop()
        await asyncio.wait_for(waiter, timeout=2)
    finally:
        await harness.app.stop()


async def test_wait_returns_when_the_scheduler_dies(
    settings: Settings, recovered: list[RecoveredConfirmation]
) -> None:
    """A process whose workers are gone is not running; it must not sit there forever."""
    harness = make_harness(settings, scheduler_crash=RuntimeError("loop died"))

    await harness.app.start()
    try:
        await asyncio.wait_for(harness.app.wait(), timeout=2)
    finally:
        await harness.app.stop()


class FakeLoop:
    def __init__(self, *, supported: bool = True) -> None:
        self.handlers: dict[int, Callable[[], None]] = {}
        self.supported = supported

    def add_signal_handler(self, signal_number: int, handler: Callable[[], None]) -> None:
        if not self.supported:
            raise NotImplementedError
        self.handlers[signal_number] = handler


def test_install_signal_handlers_registers_interrupt_and_terminate(harness: Harness) -> None:
    import signal

    loop = FakeLoop()

    install_signal_handlers(harness.app, loop=loop)

    assert set(loop.handlers) == {signal.SIGINT, signal.SIGTERM}
    loop.handlers[signal.SIGTERM]()
    assert harness.app.stop_requested


def test_install_signal_handlers_tolerates_a_platform_without_them(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    loop = FakeLoop(supported=False)

    with caplog.at_level(logging.WARNING, logger="cinema_friend.app"):
        install_signal_handlers(harness.app, loop=loop)

    assert loop.handlers == {}
    assert caplog.records


# ---------------------------------------------------------------------------
# The real Telegram application
# ---------------------------------------------------------------------------


def test_the_built_telegram_application_has_the_lifecycle_this_module_drives(
    settings: Settings, tmp_path: Path
) -> None:
    """The fakes above are only honest if the real thing has the same surface.

    Nothing here talks to Telegram -- building an application is local -- but a PTB
    upgrade that renamed a lifecycle method or moved the delivery worker would otherwise
    only show up when the daemon failed to start.
    """
    from cinema_friend.app import _default_application

    database = Database(tmp_path / "cinema-friend.db")
    application = _default_application(
        settings,
        BotDependencies(
            database=database,
            drafts=DraftRepository(),
            watches=WatchService(database, WatchRepository(), SystemClock()),
            checks=cast(Any, object()),
            results=ResultRepository(database),
            notifications=NotificationRepository(database),
            clock=SystemClock(),
        ),
    )

    for method in ("initialize", "start", "stop", "shutdown"):
        assert inspect.iscoroutinefunction(getattr(application, method))
    assert application.updater is not None
    assert inspect.iscoroutinefunction(application.updater.start_polling)
    assert inspect.iscoroutinefunction(application.updater.stop)
    assert inspect.iscoroutinefunction(application.bot.send_message)
    assert hasattr(application.bot_data["delivery_worker"], "run_once")


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_root_logging() -> Iterator[None]:
    """The CLI configures the root logger for real, so put it back afterwards."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if handler not in handlers:
                root.removeHandler(handler)
        for handler in handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(level)


def test_main_runs_the_app_with_the_loaded_settings(
    env_file: Path, isolated_root_logging: None
) -> None:
    from cinema_friend.__main__ import main

    ran: list[Settings] = []

    exit_code = main(["--env-file", str(env_file)], runner=ran.append)

    assert exit_code == 0
    assert ran[0].telegram_bot_token == TOKEN


def test_main_reports_an_unusable_env_file(
    env_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cinema_friend.__main__ import main

    env_file.chmod(0o644)
    ran: list[Settings] = []

    exit_code = main(["--env-file", str(env_file)], runner=ran.append)

    assert exit_code == 2
    assert ran == []
    assert TOKEN not in capsys.readouterr().err


def test_main_requires_an_env_file() -> None:
    from cinema_friend.__main__ import main

    with pytest.raises(SystemExit):
        main([], runner=lambda _settings: None)


def test_main_never_prints_a_raw_traceback_when_the_app_fails(
    env_file: Path, isolated_root_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A crash inside a Telegram or HTTP library can carry the token in its message.

    Letting it reach the default excepthook writes that token to the console and the
    journal in the clear, which is precisely what the redacting handler exists to stop,
    so the CLI reports failures through logging and returns a code instead.
    """
    from cinema_friend.__main__ import main

    def explode(_settings: Settings) -> None:
        raise RuntimeError(f"The token `{TOKEN}` was rejected by the server.")

    exit_code = main(["--env-file", str(env_file)], runner=explode)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert TOKEN not in captured.err + captured.out


def test_main_rejects_an_unusable_log_level(
    tmp_path: Path, database_path: Path, isolated_root_logging: None
) -> None:
    path = tmp_path / "loud.env"
    path.write_text(
        f"TELEGRAM_BOT_TOKEN={TOKEN}\n"
        "TELEGRAM_ALLOWED_USER_IDS=11\n"
        f"DATABASE_PATH={database_path}\n"
        "LOG_LEVEL=chatty\n"
    )
    path.chmod(0o600)
    from cinema_friend.__main__ import main

    ran: list[Settings] = []
    assert main(["--env-file", str(path)], runner=ran.append) == 2
    assert ran == []


# ---------------------------------------------------------------------------
# Single instance
# ---------------------------------------------------------------------------
#
# Two processes on one database means two long-polls against one bot token, two
# schedulers doubling the request rate at BFI, and every notification delivered twice.
# The second one has to lose, and it has to lose before it has polled Telegram or
# touched BFI -- which means before the schema is even opened.


async def test_a_second_app_on_the_same_database_is_refused(
    settings: Settings, recovered: list[RecoveredConfirmation], database_path: Path
) -> None:
    first = make_harness(settings)
    await first.app.start()
    second = make_harness(settings)
    try:
        with pytest.raises(AlreadyRunningError) as caught:
            await second.app.start()

        assert str(lock_path_for(database_path)) in str(caught.value)
    finally:
        await second.app.stop()
        await first.app.stop()


async def test_a_refused_second_app_never_reaches_telegram_or_bfi(
    settings: Settings, recovered: list[RecoveredConfirmation]
) -> None:
    first = make_harness(settings)
    await first.app.start()
    ORDER.clear()
    second = make_harness(settings)
    try:
        with pytest.raises(AlreadyRunningError):
            await second.app.start()

        assert second.sessions == [], "no BFI session may be built"
        assert "telegram.build" not in ORDER
        assert "updater.start_polling" not in ORDER
        assert "migrate" not in ORDER, "the lock is taken before the schema is touched"
    finally:
        await second.app.stop()
        await first.app.stop()


async def test_a_running_app_holds_a_private_lock_beside_its_database(
    harness: Harness, database_path: Path
) -> None:
    await harness.app.start()
    try:
        lock = lock_path_for(database_path)
        assert lock.exists()
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    finally:
        await harness.app.stop()


async def test_a_second_app_on_a_different_database_may_run(
    settings: Settings, recovered: list[RecoveredConfirmation], tmp_path: Path
) -> None:
    first = make_harness(settings)
    await first.app.start()
    elsewhere = Harness(
        Settings(
            telegram_bot_token=TOKEN,
            allowed_user_ids=frozenset({11}),
            database_path=tmp_path / "other" / "cinema-friend.db",
        )
    )
    try:
        await elsewhere.app.start()
    finally:
        await elsewhere.app.stop()
        await first.app.stop()


async def test_stopping_releases_the_lock(
    settings: Settings, recovered: list[RecoveredConfirmation]
) -> None:
    first = make_harness(settings)
    await first.app.start()
    await first.app.stop()

    second = make_harness(settings)
    await second.app.start()
    try:
        assert "updater.start_polling" in second.events
    finally:
        await second.app.stop()


async def test_a_startup_that_fails_half_way_releases_the_lock(
    settings: Settings, recovered: list[RecoveredConfirmation]
) -> None:
    """A refused restart after a crashed startup would need a human with a shell."""
    failed = make_harness(settings)
    failed.telegram.initialize_error = RuntimeError("bad token")
    with pytest.raises(RuntimeError, match="bad token"):
        await failed.app.start()

    healthy = make_harness(settings)
    await healthy.app.start()
    try:
        assert "updater.start_polling" in healthy.events
    finally:
        await healthy.app.stop()


def test_main_reports_a_second_instance_clearly(
    env_file: Path, isolated_root_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator gets one sentence naming the cause, not a traceback."""
    from cinema_friend.__main__ import main

    def explode(_settings: Settings) -> None:
        raise AlreadyRunningError(
            "another cinema-friend process is already using this database "
            "(lock: /tmp/cinema-friend.db.lock)"
        )

    exit_code = main(["--env-file", str(env_file)], runner=explode)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "already" in (captured.err + captured.out).lower()
    assert "Traceback" not in captured.err + captured.out
