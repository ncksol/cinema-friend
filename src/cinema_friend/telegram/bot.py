"""Outbound delivery and Telegram application wiring.

The delivery worker is the only place a queued notification becomes a message someone
reads, so it is also the only place allowed to record that they have been told. Marking
happens *after* a successful send and in one transaction with it
(:meth:`NotificationRepository.mark_delivered`): a failed send therefore leaves every
option unknown, and the next attempt says the same thing again rather than silently
dropping it. The reverse ordering -- mark, then send -- would lose results permanently
on any network blip.

Failures are split by permanence. A timeout or a network error is a bad minute and is
retried on the :data:`RETRY_DELAYS` ladder; a ``Forbidden`` (blocked bot) or a
``BadRequest`` (no such chat) will never succeed no matter how often it is retried, so
the delivery is failed and stops occupying the queue. Note that in python-telegram-bot
``BadRequest`` subclasses ``NetworkError``, so the permanent cases must be caught first.
"""

from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Protocol
from uuid import UUID

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import (
    BadRequest,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from cinema_friend.clock import Clock
from cinema_friend.config import Settings
from cinema_friend.domain.errors import AuthorizationError, InputError
from cinema_friend.domain.results import NotificationDelivery, RankVector
from cinema_friend.services.watch_service import WatchService
from cinema_friend.storage.database import Database
from cinema_friend.storage.draft_repository import DraftRepository
from cinema_friend.storage.notification_repository import NotificationRepository
from cinema_friend.storage.result_repository import ResultRepository
from cinema_friend.telegram.auth import DENIAL_TEXT, authorized_user_id
from cinema_friend.telegram.commands import (
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
from cinema_friend.telegram.rendering import RenderedMessage, render_result_page
from cinema_friend.telegram.wizard import CheckRunner, WizardDeps

_LOGGER = logging.getLogger(__name__)

#: How long to wait before each retry, by attempts already made. The last entry repeats
#: forever: a delivery that has failed four times is not going to be fixed by asking
#: faster, but it must not be abandoned either -- the user is still owed the message.
RETRY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
)

_RESULTS_KIND = "results"
_CONTRACT_ERROR_KIND = "contract_error"
_DEGRADATION_KIND = "degradation"
_RECOVERY_KIND = "recovery"

_MAX_TITLE_CHARS = 80

_RESULTS_GONE = (
    "Those results have expired before I could send them. Send /check for fresh "
    "availability."
)


class MessageSender(Protocol):
    """The one Bot capability this module needs, so tests can supply their own."""

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        parse_mode: str | None,
        reply_markup: InlineKeyboardMarkup | None,
    ) -> object: ...


class DeliveryAttemptOutcome(Enum):
    """The three things a sweep can do to one row.

    ``SENT`` means the message reached Telegram, which is the fact a waiting caller
    cares about; whether the bookkeeping that follows it succeeded is recorded
    separately on :attr:`DeliveryAttempt.recorded`.
    """

    SENT = "sent"
    RETRIED = "retried"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DeliveryAttempt:
    """What happened to one delivery in one sweep.

    Carries identity, not just a tally: a caller that flushed the queue to get *its own*
    message out has to be able to ask whether *its* row went, rather than whether
    anything at all went. Without this a busy queue would let one user's successful
    delivery silence another user's failed one.
    """

    delivery_id: UUID
    idempotency_key: str
    recipient_user_id: int
    watch_id: UUID | None
    outcome: DeliveryAttemptOutcome
    recorded: bool = True


@dataclass(frozen=True, slots=True)
class DeliveryRun:
    """What one sweep of the queue achieved, delivery by delivery."""

    attempts: tuple[DeliveryAttempt, ...] = ()

    @property
    def attempted(self) -> int:
        return len(self.attempts)

    @property
    def sent(self) -> int:
        return len(self.sent_ids)

    @property
    def retried(self) -> int:
        return len(self.retried_ids)

    @property
    def failed(self) -> int:
        return len(self.failed_ids)

    @property
    def sent_ids(self) -> frozenset[UUID]:
        return self._ids(DeliveryAttemptOutcome.SENT)

    @property
    def retried_ids(self) -> frozenset[UUID]:
        return self._ids(DeliveryAttemptOutcome.RETRIED)

    @property
    def failed_ids(self) -> frozenset[UUID]:
        return self._ids(DeliveryAttemptOutcome.FAILED)

    @property
    def sent_keys(self) -> frozenset[str]:
        """The idempotency keys that were sent, for callers that know the key not the id."""
        return frozenset(
            attempt.idempotency_key
            for attempt in self.attempts
            if attempt.outcome is DeliveryAttemptOutcome.SENT
        )

    def sent_for(self, *, watch_id: UUID, recipient_user_id: int) -> bool:
        """Whether this run sent a message about *watch_id* to *recipient_user_id*.

        Both halves matter. A host-wide alert carries no watch id and must never be
        mistaken for a watch's results; another user's delivery must never be mistaken
        for this caller's.
        """
        return any(
            attempt.outcome is DeliveryAttemptOutcome.SENT
            and attempt.watch_id == watch_id
            and attempt.recipient_user_id == recipient_user_id
            for attempt in self.attempts
        )

    def _ids(self, outcome: DeliveryAttemptOutcome) -> frozenset[UUID]:
        return frozenset(
            attempt.delivery_id for attempt in self.attempts if attempt.outcome is outcome
        )


@dataclass(frozen=True, slots=True)
class _Renderable:
    """A message plus the facts that become true only once it is actually sent."""

    message: RenderedMessage
    option_keys: Sequence[str]
    best_rank: RankVector | None


def _plain(text: str) -> RenderedMessage:
    return RenderedMessage(text=text, parse_mode=ParseMode.HTML, reply_markup=None)


def _title(raw: str | None) -> str | None:
    return None if raw is None else html.escape(raw[:_MAX_TITLE_CHARS])


def _contract_error_text(title: str | None) -> str:
    subject = f"<b>{title}</b>" if title else "one of your watches"
    return (
        f"I've stopped watching {subject}. The BFI page no longer looks the way I "
        "expect, so I can't read seat availability from it — this usually means the "
        "site changed. The watch is paused. Send /check to try again once the site "
        "settles, or /watches to delete it."
    )


def _degradation_text(host: str | None) -> str:
    where = html.escape(host) if host else "the BFI site"
    return (
        f"I can't get reliable answers from <code>{where}</code>, so I've paused "
        "checks against it. I'll keep retrying and tell you when it's back."
    )


def _recovery_text(host: str | None, detail: str | None) -> str:
    where = html.escape(host) if host else "the BFI site"
    what = html.escape(detail) if detail else "back"
    return f"<code>{where}</code> is {what}. Your checks have resumed."


def _retry_after(error: RetryAfter) -> timedelta:
    """Read Telegram's requested wait, tolerating both PTB representations of it."""
    value = error.retry_after
    return value if isinstance(value, timedelta) else timedelta(seconds=value)


class DeliveryWorker:
    """Sends queued notifications, retrying transient failures with backoff.

    One sweep is serialized by a lock so a manual ``/check`` flush and a scheduled tick
    can never pick up the same due row concurrently and send it twice.
    """

    def __init__(
        self,
        *,
        bot: MessageSender,
        database: Database,
        notifications: NotificationRepository,
        results: ResultRepository,
        watches: WatchService,
        clock: Clock,
    ) -> None:
        self._bot = bot
        self._database = database
        self._notifications = notifications
        self._results = results
        self._watches = watches
        self._clock = clock
        self._lock = asyncio.Lock()

    async def run_once(self) -> DeliveryRun:
        """Attempt every delivery that is due now and report what happened to each.

        Every row is isolated. A delivery that raises something with no rule attached to
        it -- an unrecognised Telegram error, a database that went away mid-render -- is
        logged, left retryable, and stepped over, because the queue is ordered by due
        time: an exception that escaped this loop would abort the sweep at the oldest
        row and would abort every future sweep at exactly the same place, permanently
        blocking every message queued behind it.

        ``BaseException`` is deliberately not caught. Cancellation and interpreter exit
        are not delivery problems and must keep unwinding.
        """
        async with self._lock:
            now = self._clock.now()
            async with self._database.connection() as conn:
                due = await self._notifications.due_deliveries(conn, now)
            attempts: list[DeliveryAttempt] = []
            for delivery in due:
                try:
                    attempts.append(await self._attempt(delivery, now))
                except Exception:
                    _LOGGER.exception(
                        "unhandled failure for delivery %s; leaving it retryable",
                        delivery.delivery_id,
                    )
                    await self._back_off(delivery, now)
                    attempts.append(self._record(delivery, DeliveryAttemptOutcome.RETRIED))
            return DeliveryRun(attempts=tuple(attempts))

    async def _attempt(self, delivery: NotificationDelivery, now: datetime) -> DeliveryAttempt:
        """Render, send, and record one delivery, classifying every failure it knows."""
        renderable = await self._render(delivery)
        if renderable is None:
            _LOGGER.error(
                "unknown notification kind %r for delivery %s; failing it",
                delivery.payload.kind,
                delivery.delivery_id,
            )
            await self._fail(delivery, now)
            return self._record(delivery, DeliveryAttemptOutcome.FAILED)
        try:
            await self._send(renderable, delivery)
        except (Forbidden, BadRequest, InvalidToken) as error:
            _LOGGER.error("permanent delivery failure for %s: %s", delivery.delivery_id, error)
            await self._fail(delivery, now)
            return self._record(delivery, DeliveryAttemptOutcome.FAILED)
        except RetryAfter as error:
            await self._retry(delivery, now, floor=_retry_after(error))
            return self._record(delivery, DeliveryAttemptOutcome.RETRIED)
        except NetworkError as error:
            _LOGGER.warning("transient delivery failure for %s: %s", delivery.delivery_id, error)
            await self._retry(delivery, now)
            return self._record(delivery, DeliveryAttemptOutcome.RETRIED)
        except TelegramError as error:
            # A Telegram failure this code has no rule for. Assume it is transient:
            # retrying costs a duplicate at worst, while failing it would throw away a
            # message the user is owed on the strength of an error nobody has read yet.
            _LOGGER.error(
                "unclassified Telegram failure for %s (%s); retrying: %s",
                delivery.delivery_id,
                type(error).__name__,
                error,
            )
            await self._retry(delivery, now)
            return self._record(delivery, DeliveryAttemptOutcome.RETRIED)
        try:
            await self._mark_sent(delivery, renderable)
        except Exception:
            # The message went out; only the bookkeeping failed. The row stays PENDING
            # so at-least-once delivery repeats it, and it is backed off so a database
            # that is failing consistently cannot turn the queue into a resend loop.
            _LOGGER.exception(
                "delivery %s was sent but could not be recorded; leaving it pending",
                delivery.delivery_id,
            )
            await self._back_off(delivery, now)
            return self._record(delivery, DeliveryAttemptOutcome.SENT, recorded=False)
        return self._record(delivery, DeliveryAttemptOutcome.SENT)

    @staticmethod
    def _record(
        delivery: NotificationDelivery,
        outcome: DeliveryAttemptOutcome,
        *,
        recorded: bool = True,
    ) -> DeliveryAttempt:
        return DeliveryAttempt(
            delivery_id=delivery.delivery_id,
            idempotency_key=delivery.idempotency_key,
            recipient_user_id=delivery.payload.recipient_user_id,
            watch_id=delivery.payload.watch_id,
            outcome=outcome,
            recorded=recorded,
        )

    async def _send(self, renderable: _Renderable, delivery: NotificationDelivery) -> None:
        message = renderable.message
        await self._bot.send_message(
            chat_id=delivery.payload.recipient_user_id,
            text=message.text,
            parse_mode=message.parse_mode,
            reply_markup=message.reply_markup,
        )

    async def _mark_sent(
        self, delivery: NotificationDelivery, renderable: _Renderable
    ) -> None:
        """Record a sent message's consequences in one transaction.

        Sending first is deliberate: a crash between the send and the mark leaves the
        delivery pending and may repeat a message, which is a nuisance. The other order
        would mark options known for a message nobody received, which is silence.
        """
        async with self._database.connection() as conn:
            await self._notifications.mark_delivered(
                conn,
                delivery.delivery_id,
                renderable.option_keys,
                renderable.best_rank,
                self._clock.now(),
            )

    async def _back_off(self, delivery: NotificationDelivery, now: datetime) -> None:
        """Push a row down the retry ladder, tolerating a database that is still broken.

        Used only on paths that have already failed unexpectedly, where the alternative
        -- leaving ``next_attempt_at`` untouched -- would make the row due again
        immediately and let the very next sweep repeat whatever just went wrong.
        """
        try:
            await self._retry(delivery, now)
        except Exception:
            _LOGGER.exception(
                "could not back off delivery %s; it stays due", delivery.delivery_id
            )

    async def _retry(
        self,
        delivery: NotificationDelivery,
        now: datetime,
        *,
        floor: timedelta | None = None,
    ) -> None:
        step = RETRY_DELAYS[min(delivery.attempt_count, len(RETRY_DELAYS) - 1)]
        delay = max(step, floor) if floor is not None else step
        async with self._database.connection() as conn:
            await self._notifications.reschedule(conn, delivery.delivery_id, now + delay)

    async def _fail(self, delivery: NotificationDelivery, now: datetime) -> None:
        async with self._database.connection() as conn:
            await self._notifications.mark_failed(conn, delivery.delivery_id, now)

    async def _render(self, delivery: NotificationDelivery) -> _Renderable | None:
        payload = delivery.payload
        if payload.kind == _RESULTS_KIND:
            return await self._render_results(payload.snapshot_id)
        if payload.kind == _CONTRACT_ERROR_KIND:
            return _Renderable(
                message=_plain(
                    _contract_error_text(
                        await self._watch_title(payload.recipient_user_id, payload.watch_id)
                    )
                ),
                option_keys=(),
                best_rank=None,
            )
        if payload.kind == _DEGRADATION_KIND:
            return _Renderable(_plain(_degradation_text(payload.host)), (), None)
        if payload.kind == _RECOVERY_KIND:
            return _Renderable(
                _plain(_recovery_text(payload.host, payload.recovery_text)), (), None
            )
        return None

    async def _watch_title(self, recipient_user_id: int, watch_id: UUID | None) -> str | None:
        if watch_id is None:
            return None
        try:
            watch = await self._watches.get_owned(recipient_user_id, watch_id)
        except InputError:
            # The watch was deleted before the alert went out. The alert is still true
            # and still owed; it just cannot name the film any more.
            return None
        return _title(watch.title)

    async def _render_results(self, snapshot_id: UUID | None) -> _Renderable:
        """Render the *referenced* snapshot, never the watch's latest.

        A page that silently upgraded itself to newer results would contradict the
        message the user is replying to. A snapshot that retention has already pruned
        gets a truthful "expired" message with no option keys, so nothing is recorded
        as announced that never was.
        """
        if snapshot_id is None:
            return _Renderable(_plain(_RESULTS_GONE), (), None)
        try:
            async with self._database.connection() as conn:
                page = await self._results.snapshot_page(conn, snapshot_id, 1)
                if page.total_options > len(page.options):
                    page_size = page.total_options
                    full = await self._results.snapshot_page(
                        conn, snapshot_id, 1, page_size=page_size
                    )
                else:
                    full = page
        except InputError:
            return _Renderable(_plain(_RESULTS_GONE), (), None)
        keys = [option.key for option in full.options]
        best = min(
            (option.rank_vector for option in full.options),
            key=lambda vector: vector.sort_key(),
            default=None,
        )
        return _Renderable(render_result_page(page), keys, best)


# ---------------------------------------------------------------------------
# Application wiring
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BotDependencies:
    """The collaborators the Telegram layer needs, built by the caller that owns them."""

    database: Database
    drafts: DraftRepository
    watches: WatchService
    checks: CheckRunner
    results: ResultRepository
    notifications: NotificationRepository
    clock: Clock


_Handler = Callable[[Update, CommandDeps], Awaitable[RenderedMessage | None]]
_Context = ContextTypes.DEFAULT_TYPE


def _chat_id(update: Update) -> int | None:
    if update.effective_chat is not None:
        return update.effective_chat.id
    return None if update.effective_user is None else update.effective_user.id


def _route(
    handler: _Handler, deps: CommandDeps, *, answers_callback: bool = False
) -> Callable[[Update, _Context], Coroutine[Any, Any, None]]:
    """Adapt a pure handler into a PTB callback that sends whatever it returns.

    Authorization happens here, first, before any bot call and before the handler is
    entered. Answering a callback query is itself a request made on the caller's behalf
    against a resource they named, so doing it ahead of the allow-list check would let
    an unlisted user provoke a bot action -- and would confirm, by the spinner stopping,
    that the button was live. An unauthorized caller therefore gets exactly
    :data:`~cinema_friend.telegram.auth.DENIAL_TEXT` and nothing else: no callback
    answer, no resource-specific wording, nothing that distinguishes a real watch id
    from an invented one.

    An :class:`InputError` that is *not* an authorization failure means an allowed user
    hit something stale or invalid that the handler did not convert into a reply.
    Silence there would be a bug the user cannot see, so it becomes the same benign
    guidance a stale button gets, with the underlying message left in the log.
    """

    async def callback(update: Update, context: _Context) -> None:
        try:
            authorized_user_id(update, deps.allowed_user_ids)
        except AuthorizationError:
            _LOGGER.warning("denied update %s", update.update_id)
            await _reply(update, context, DENIAL_TEXT)
            return
        if answers_callback and update.callback_query is not None:
            # Stop the client's spinner before doing any work, so a slow handler does
            # not look like a broken button. Only ever for a caller already allowed in.
            await context.bot.answer_callback_query(update.callback_query.id)
        try:
            message = await handler(update, deps)
        except AuthorizationError:  # pragma: no cover - the guard above already ran
            _LOGGER.warning("denied update %s", update.update_id)
            await _reply(update, context, DENIAL_TEXT)
            return
        except InputError as error:
            _LOGGER.warning("unhandled input error on update %s: %s", update.update_id, error)
            await _reply(update, context, STALE_ACTION)
            return
        if message is None:
            return
        await _send(update, context, message)

    return callback


async def _reply(update: Update, context: _Context, text: str) -> None:
    await _send(update, context, _plain(text))


async def _send(update: Update, context: _Context, message: RenderedMessage) -> None:
    chat_id = _chat_id(update)
    if chat_id is None:  # pragma: no cover - Telegram always supplies one of the two
        return
    await context.bot.send_message(
        chat_id=chat_id,
        text=message.text,
        parse_mode=message.parse_mode,
        reply_markup=message.reply_markup,
    )


def build_telegram_application(
    settings: Settings, dependencies: BotDependencies
) -> Application[Any, Any, Any, Any, Any, Any]:
    """Build the Telegram application with every handler registered, in order.

    Ordering is the contract: commands are matched before free text (so ``/check`` is
    never fed to the wizard as an answer), and the wizard's own ``wizard:`` callbacks
    are matched before the versioned ``v1:`` action callbacks. The two patterns are
    disjoint, so the order is belt-and-braces rather than load-bearing.

    The delivery worker is constructed here because it needs the application's bot, and
    is published on ``bot_data`` so the scheduler can flush the queue on its own tick.
    """
    application = ApplicationBuilder().token(settings.telegram_bot_token).build()
    worker = DeliveryWorker(
        bot=application.bot,
        database=dependencies.database,
        notifications=dependencies.notifications,
        results=dependencies.results,
        watches=dependencies.watches,
        clock=dependencies.clock,
    )
    deps = CommandDeps(
        wizard=WizardDeps(
            database=dependencies.database,
            drafts=dependencies.drafts,
            watches=dependencies.watches,
            checks=dependencies.checks,
            clock=dependencies.clock,
        ),
        results=dependencies.results,
        deliveries=worker,
        allowed_user_ids=frozenset(settings.allowed_user_ids),
    )

    application.bot_data["delivery_worker"] = worker
    application.bot_data["command_deps"] = deps

    for command, handler in (
        ("start", handle_help),
        ("help", handle_help),
        ("new", handle_new),
        ("cancel", handle_cancel),
        ("watches", handle_watches),
        ("check", handle_check),
        ("pause", handle_pause),
        ("resume", handle_resume),
        ("delete", handle_delete),
    ):
        application.add_handler(CommandHandler(command, _route(handler, deps)))

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _route(handle_text, deps))
    )
    application.add_handler(
        CallbackQueryHandler(
            _route(handle_wizard_button, deps, answers_callback=True), pattern="^wizard:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(_route(handle_callback, deps, answers_callback=True), pattern="^v1:")
    )
    return application
