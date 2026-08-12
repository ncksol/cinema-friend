"""Process lifecycle: read the configuration, wire everything up, and take it down again.

This is the only module that knows the whole application exists. Every other component
takes its collaborators as arguments and has no idea what a process is; here they are
constructed once, in the one order that works, and torn down in the reverse of it.

Startup order is not arbitrary:

1. Check the SQLite build first. Snapshot paging needs JSON functions that arrived in
   3.38, and finding that out an hour later, mid-check, is not a diagnosis anyone wants.
2. Take the single-instance lock before anything else touches the database or reaches
   outside the process. A second copy of the service on one database double-polls the
   bot, doubles the request rate at BFI, and delivers every notification twice; the
   only safe moment to refuse it is before it has done any of those things once.
3. Migrate before anything can read or write, so no component ever meets a half-built
   schema.
4. Build exactly one BFI session, shared by the one transport. The transport's spacing,
   concurrency cap, and circuit breaker are all per-instance, so a second session would
   silently double the request rate against a host that is already rate-limiting us.
5. Initialize Telegram, then converge stale wizard confirmations, and only then start
   polling. A confirmation left half-written by a crash has to be resolved before the
   user can send another update about it -- and telling the user about it needs a bot
   that is initialized but not yet racing incoming messages.

Shutdown is the same list backwards, with one addition: new work stops first, then
checks already in flight get a bounded grace period, and only then is anything closed.
Every close step runs even if an earlier one throws, because a failure to stop polling
is no reason to leak an HTTP session -- or to leave the lock held against the next
start.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import stat
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Final, Protocol, cast

from curl_cffi.requests import AsyncSession
from dotenv import dotenv_values

from cinema_friend.bfi.gateway import BfiGateway
from cinema_friend.bfi.transport import AsyncHttpSession, BfiTransport
from cinema_friend.clock import Clock, SystemClock
from cinema_friend.config import Settings
from cinema_friend.domain.errors import InputError
from cinema_friend.services.check_service import CheckService
from cinema_friend.services.scheduler import Scheduler, SchedulerDependencies
from cinema_friend.services.watch_service import WatchService
from cinema_friend.single_instance import SingleInstanceLock
from cinema_friend.storage.circuit_repository import SqliteCircuitStore
from cinema_friend.storage.database import Database, ensure_supported_sqlite
from cinema_friend.storage.draft_repository import DraftRepository
from cinema_friend.storage.notification_repository import NotificationRepository
from cinema_friend.storage.result_repository import ResultRepository
from cinema_friend.storage.retention import RetentionService
from cinema_friend.storage.watch_repository import WatchRepository
from cinema_friend.telegram.bot import BotDependencies, build_telegram_application
from cinema_friend.telegram.wizard import WizardDeps, recover_confirmations

logger = logging.getLogger(__name__)

SHUTDOWN_GRACE_SECONDS: Final = 60.0
"""How long shutdown waits for the scheduler to wind down before cancelling it.

It is not sized to cover a whole check, because no value here could:
``bfi.transport.TOTAL_TIMEOUT_SECONDS`` bounds one HTTP *attempt* at 45 seconds, and a
check is many attempts. Each ``get`` retries that attempt on a network error, a 5xx or a
403, and follows up to six redirect hops; one check fetches the film page, every
pagination page, and a seat map per candidate performance. A slow check runs for minutes.

What the grace buys is the ordinary case: a check with only its commit left to do, and a
fair chance for one in-flight request to return. Exceeding it costs nothing that needs
repairing. The transaction unwinds on cancellation like any other ``BaseException``, so
no half-written run is left behind, and the scheduler leaves the watch exactly as the
scan found it -- still due, in the past -- so the next process to start runs it again.
Cancelling costs a repeated check, not a lost one.

Sixty is therefore a ceiling on waiting rather than a budget anything is spent against:
shutdown returns as soon as the scheduler is idle, so the wait is only ever paid by a
restart that lands mid-check, and it stops well short of the point where an operator
would conclude the service has hung.
"""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_settings(env_file: Path) -> Settings:
    """Read settings from a private env file, refusing to read it if it is not private.

    The file holds a bot token, so its permissions are part of its correctness. The
    checks run against the file's own metadata and the parse happens only afterwards:
    a file the whole machine can read must be rejected without its contents ever being
    loaded into this process.
    """
    try:
        info = os.stat(env_file)
    except OSError as error:
        raise InputError(f"env file does not exist or cannot be read: {env_file}") from error
    if not stat.S_ISREG(info.st_mode):
        raise InputError(f"env file is not a regular file: {env_file}")
    if info.st_uid != os.getuid():
        raise InputError(f"env file must be owned by the user running this process: {env_file}")
    if info.st_mode & 0o077:
        raise InputError(f"env file must not be readable by group or others (mode 0600): {env_file}")

    values = {key: value for key, value in dotenv_values(env_file).items() if value is not None}
    return Settings.from_mapping(values)


# ---------------------------------------------------------------------------
# The Telegram surface this module drives
# ---------------------------------------------------------------------------


class TelegramUpdater(Protocol):
    async def start_polling(self) -> Any: ...
    async def stop(self) -> None: ...


class TelegramApplication(Protocol):
    """Just the lifecycle python-telegram-bot exposes, named so it can be faked."""

    @property
    def bot(self) -> Any: ...
    @property
    def updater(self) -> TelegramUpdater | None: ...
    @property
    def bot_data(self) -> dict[str, Any]: ...
    async def initialize(self) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def shutdown(self) -> None: ...


class SchedulerRunner(Protocol):
    async def run(self, stop_event: asyncio.Event) -> None: ...


SessionFactory = Callable[[Settings], AsyncHttpSession]
ApplicationFactory = Callable[[Settings, BotDependencies], TelegramApplication]
SchedulerFactory = Callable[[SchedulerDependencies], SchedulerRunner]


def _default_session(settings: Settings) -> AsyncHttpSession:
    # `AsyncSession`'s keyword signature is broader than the two methods we call, so the
    # narrowing to our protocol is asserted here rather than inferred.
    return cast("AsyncHttpSession", AsyncSession(impersonate=settings.bfi_impersonate_profile))


def _default_application(settings: Settings, dependencies: BotDependencies) -> TelegramApplication:
    # PTB's `Application` satisfies this protocol at runtime; its six type parameters
    # do not line up structurally, so the narrowing is asserted once, here.
    return cast("TelegramApplication", build_telegram_application(settings, dependencies))


def _default_scheduler(dependencies: SchedulerDependencies) -> SchedulerRunner:
    return Scheduler(dependencies)


# ---------------------------------------------------------------------------
# The application
# ---------------------------------------------------------------------------


class CinemaFriendApp:
    """One running process: storage, the BFI transport, Telegram, and the scheduler."""

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Clock | None = None,
        session_factory: SessionFactory = _default_session,
        application_factory: ApplicationFactory = _default_application,
        scheduler_factory: SchedulerFactory = _default_scheduler,
        shutdown_grace_seconds: float = SHUTDOWN_GRACE_SECONDS,
    ) -> None:
        self._settings = settings
        self._clock = clock if clock is not None else SystemClock()
        self._session_factory = session_factory
        self._application_factory = application_factory
        self._scheduler_factory = scheduler_factory
        self._grace = shutdown_grace_seconds

        self._running = False
        self._lock = SingleInstanceLock(settings.database_path)
        self._stop_requested = asyncio.Event()
        self._scheduler_stop = asyncio.Event()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._session: AsyncHttpSession | None = None
        self._transport: BfiTransport | None = None
        self._telegram: TelegramApplication | None = None
        # Each flag means "this step was entered", not "this step succeeded": a step
        # that failed part-way still has resources to release.
        self._telegram_entered = False
        self._telegram_started = False
        self._polling_started = False

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    # -- startup -------------------------------------------------------

    async def start(self) -> None:
        """Bring the process up, cleaning up completely if any step fails."""
        if self._running:
            raise RuntimeError("application is already running")
        ensure_supported_sqlite()
        # Outside the try: a refused lock is a deployment mistake, not a failed
        # startup. There is nothing built yet to unwind and nothing about it worth a
        # traceback, so it propagates as the one sentence it is.
        self._lock.acquire()

        self._stop_requested = asyncio.Event()
        self._scheduler_stop = asyncio.Event()
        try:
            await self._start()
        except BaseException:
            logger.exception("startup failed; unwinding")
            await self._teardown()
            raise
        self._running = True
        logger.info("application started", extra={"database": str(self._settings.database_path)})

    async def _start(self) -> None:
        settings = self._settings
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        database = Database(settings.database_path)
        async with database.connection() as conn:
            await database.migrate(conn)

        watches = WatchRepository()
        results = ResultRepository(database)
        notifications = NotificationRepository(database)
        drafts = DraftRepository()

        # One session, one transport: spacing, the concurrency cap, and the circuit
        # breaker are all per-transport, so a second one would double the real request
        # rate against a host that is already pushing back. The transport owns the
        # session from here on and closing it is what closes the session.
        session = self._session_factory(settings)
        self._session = session
        circuits = SqliteCircuitStore(database)
        transport = BfiTransport(settings, circuits, self._clock, session=session)
        self._transport = transport
        gateway = BfiGateway(transport, self._clock, cache_seconds=settings.bfi_cache_seconds)

        watch_service = WatchService(database, watches, self._clock)
        check_service = CheckService(
            database, gateway, circuits, watches, results, notifications, self._clock
        )
        retention = RetentionService(database)

        self._telegram_entered = True
        telegram = self._application_factory(
            settings,
            BotDependencies(
                database=database,
                drafts=drafts,
                watches=watch_service,
                checks=check_service,
                results=results,
                notifications=notifications,
                clock=self._clock,
            ),
        )
        self._telegram = telegram
        await telegram.initialize()

        await self._recover_confirmations(
            telegram,
            WizardDeps(
                database=database,
                drafts=drafts,
                watches=watch_service,
                checks=check_service,
                clock=self._clock,
            ),
        )

        await telegram.start()
        self._telegram_started = True
        updater = telegram.updater
        if updater is None:  # pragma: no cover - the builder always provides one
            raise RuntimeError("telegram application has no updater to poll with")
        await updater.start_polling()
        self._polling_started = True

        scheduler = self._scheduler_factory(
            SchedulerDependencies(
                database=database,
                watches=watches,
                checks=check_service,
                # The worker is bound to this application's bot, so it has to come from
                # the built application rather than being constructed alongside it.
                deliveries=telegram.bot_data["delivery_worker"],
                retention=retention,
                clock=self._clock,
            )
        )
        self._scheduler_task = asyncio.create_task(
            scheduler.run(self._scheduler_stop), name="cinema-friend-scheduler"
        )

    async def _recover_confirmations(
        self, telegram: TelegramApplication, wizard: WizardDeps
    ) -> None:
        """Finish any wizard confirmation a crash left half-applied, and say so.

        Run before polling starts: until it has, a user's next message would be answered
        against a draft the process has not yet reconciled with what was actually saved.
        """
        recovered = await recover_confirmations(wizard)
        for confirmation in recovered:
            try:
                await telegram.bot.send_message(
                    chat_id=confirmation.user_id,
                    text=confirmation.message.text,
                    parse_mode=confirmation.message.parse_mode,
                    reply_markup=confirmation.message.reply_markup,
                )
            except Exception:
                # The recovery itself is already committed. A user we cannot reach --
                # blocked, deleted chat -- is not a reason to leave the process down.
                logger.warning(
                    "could not deliver a recovered confirmation",
                    exc_info=True,
                    extra={"user_id": confirmation.user_id},
                )
        if recovered:
            logger.info("recovered pending confirmations", extra={"count": len(recovered)})

    # -- waiting and stopping ------------------------------------------

    def request_stop(self) -> None:
        """Ask the process to shut down. Safe to call from a signal handler."""
        self._stop_requested.set()

    async def wait(self) -> None:
        """Block until a stop is requested or the scheduler stops on its own.

        The second case matters: a process whose worker loops have died is not running,
        and waiting for a signal that will never come would leave it up but idle.
        """
        waiters: list[asyncio.Task[Any]] = [asyncio.create_task(self._stop_requested.wait())]
        if self._scheduler_task is not None:
            waiters.append(self._scheduler_task)
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                if waiter is not self._scheduler_task and not waiter.done():
                    waiter.cancel()

    async def stop(self) -> None:
        """Stop new work, wait out the grace period, then close everything."""
        if not self._running and self._telegram is None and self._transport is None:
            return
        await self._teardown()
        self._running = False
        logger.info("application stopped")

    async def _teardown(self) -> None:
        """Release everything that was built, in reverse, whatever else fails.

        Each step is guarded on its own. A transport left open because the updater
        refused to stop would outlive the process's usefulness and keep a connection
        pool alive; the point of shutdown is that it finishes.
        """
        self._scheduler_stop.set()
        await self._stop_scheduler()

        if self._polling_started and self._telegram is not None:
            updater = self._telegram.updater
            if updater is not None:
                await _closing("stop telegram polling", updater.stop())
        self._polling_started = False

        if self._telegram_started and self._telegram is not None:
            await _closing("stop telegram application", self._telegram.stop())
        self._telegram_started = False

        if self._telegram_entered and self._telegram is not None:
            await _closing("shut down telegram application", self._telegram.shutdown())
        self._telegram_entered = False
        self._telegram = None

        if self._transport is not None:
            # Closes the shared session too; it is the transport's to close.
            await _closing("close bfi transport", self._transport.close())
        elif self._session is not None:
            # Startup died between building the session and handing it over, so nobody
            # owns it but this.
            await _closing("close bfi session", self._session.close())
        self._transport = None
        self._session = None

        # Last, and unconditional: the lock is what the next start needs back. Releasing
        # it only closes the descriptor this process opened, so an instance that was
        # refused the lock cannot free the holder's by unwinding.
        self._lock.release()

    async def _stop_scheduler(self) -> None:
        task = self._scheduler_task
        self._scheduler_task = None
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self._grace)
        except TimeoutError:
            logger.warning(
                "checks still running after the shutdown grace period; cancelling",
                extra={"grace_seconds": self._grace},
            )
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.info("scheduler cancelled after the grace period")
            except Exception:
                logger.exception("scheduler failed while being cancelled")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduler stopped with an error")


async def _closing(what: str, action: Awaitable[None]) -> None:
    """Await one teardown step, logging rather than propagating its failure."""
    try:
        await action
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("shutdown step failed", extra={"step": what})


# ---------------------------------------------------------------------------
# Process entry points
# ---------------------------------------------------------------------------


def install_signal_handlers(
    app: CinemaFriendApp,
    loop: Any = None,
    signals: Sequence[int] = (signal.SIGINT, signal.SIGTERM),
) -> None:
    """Ask *app* to stop when the process is interrupted or terminated.

    Registered on the loop rather than with :func:`signal.signal` so the handler runs as
    a normal callback between iterations instead of interrupting whatever coroutine
    happened to be executing. Platforms without loop signal support are logged and left
    alone; the process is still stoppable, just not gracefully.
    """
    target = loop if loop is not None else asyncio.get_running_loop()
    for number in signals:
        try:
            target.add_signal_handler(number, app.request_stop)
        except NotImplementedError:
            logger.warning(
                "this platform cannot register signal handlers on the event loop",
                extra={"signal": number},
            )
            return


async def run_app(settings: Settings, *, app: CinemaFriendApp | None = None) -> None:
    """Run until stopped, and always shut down cleanly."""
    instance = app if app is not None else CinemaFriendApp(settings)
    await instance.start()
    try:
        install_signal_handlers(instance)
        await instance.wait()
    finally:
        await instance.stop()
