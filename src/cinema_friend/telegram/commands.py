"""Telegram command and callback handlers.

Every entry point in this module authorizes *first*: nothing is read and nothing is
mutated until :func:`~cinema_friend.telegram.auth.authorized_user_id` has accepted the
update. A denial is the only error a handler lets escape -- every other failure is
turned into a reply, because a user who typed something wrong deserves an answer, while
a user who is not allowed to be here deserves no signal at all.

Watches are addressed by the number shown in ``/watches``, resolved against the
caller's own list. That makes ownership structural rather than checked: an index can
only ever name a watch the caller already owns, so there is no id to guess and no
cross-owner reference to reject.

Handlers return a :class:`~cinema_friend.telegram.rendering.RenderedMessage` (or
``None`` when the update is not theirs and nothing should be said); actually sending it
belongs to :mod:`cinema_friend.telegram.bot`. Keeping I/O out of here is what lets these
tests drive real storage without a live bot.
"""

from __future__ import annotations

import html
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode

from cinema_friend.clock import Clock
from cinema_friend.domain.errors import ConflictError, InputError
from cinema_friend.domain.results import CheckResult
from cinema_friend.domain.state import CheckOutcome, CheckTrigger, WatchMode, WatchStatus
from cinema_friend.domain.watch import Watch
from cinema_friend.services.watch_service import WatchService
from cinema_friend.storage.database import Database
from cinema_friend.storage.result_repository import ResultRepository
from cinema_friend.telegram import wizard
from cinema_friend.telegram.auth import authorized_user_id
from cinema_friend.telegram.callbacks import (
    ResultPageAction,
    WatchAction,
    decode_callback,
    encode_callback,
)
from cinema_friend.telegram.rendering import (
    RenderedMessage,
    render_result_page,
    render_watch_list,
)
from cinema_friend.telegram.wizard import CheckRunner, WizardDeps

_MAX_TITLE_CHARS = 80

HELP_TEXT = (
    "<b>Cinema Friend</b>\n"
    "I watch BFI IMAX seating and tell you when good seats appear.\n"
    "\n"
    "/new — set up a new watch\n"
    "/cancel — abandon a setup in progress\n"
    "/watches — list your watches, with buttons to pause, resume or delete\n"
    "/check &lt;number&gt; — check one watch right now\n"
    "/pause &lt;number&gt; — stop scheduled checks for a watch\n"
    "/resume &lt;number&gt; — start them again\n"
    "/delete &lt;number&gt; — delete a watch (I'll ask you to confirm)\n"
    "/help — this message"
)

_NO_WATCHES = "You have no watches yet. Send /new to create one."
#: The one answer to everything that no longer describes reality: a duplicate tap, a
#: watch deleted mid-command, a forged callback. It names nothing, so it cannot confirm
#: that any particular watch ever existed.
STALE_ACTION = "That no longer applies — send /watches for the current list."
#: The same idea for a result page: pruned, out of range, and not-yours are one answer.
RESULTS_GONE = "Those results are no longer available. Send /check for fresh availability."
_UNKNOWN_TEXT = "I didn't understand that. Send /help to see what I can do."
_SUPERSEDED = (
    "That watch changed while I was checking it, so I stopped. "
    "Send /watches to see where it stands."
)
_RESUME_ONE_OFF = (
    "That's a one-off watch, so there is no schedule to resume. Send /check to run it "
    "again now, or delete it."
)
_CHECK_PAUSED = "That watch is paused. Send /resume to start its schedule again, then /check."

_OUTCOME_REPLY: dict[CheckOutcome, str] = {
    CheckOutcome.SUCCESS: "Checked — I'll send the results in a moment.",
    CheckOutcome.NO_CHANGE: "Checked — I'll send the results in a moment.",
    CheckOutcome.NEW_OPTIONS: "Checked — I'll send the results in a moment.",
    CheckOutcome.NETWORK_ERROR: (
        "I couldn't reach the BFI site just now. Nothing has changed; I'll try again."
    ),
    CheckOutcome.CHALLENGE: (
        "The BFI site asked for a browser check, so I couldn't read availability. "
        "I'll try again shortly."
    ),
    CheckOutcome.CONTRACT_ERROR: (
        "The BFI page no longer looks the way I expect, so I've stopped that watch. "
        "Send /check to try again once the site settles, or delete it."
    ),
    CheckOutcome.CIRCUIT_OPEN: (
        "I've paused requests to the BFI site after repeated failures. I'll retry "
        "automatically and tell you when it's back."
    ),
    CheckOutcome.PERSISTENCE_ERROR: (
        "Something went wrong saving that check, so nothing was recorded. Try again."
    ),
    CheckOutcome.RUNNING: "That check is still running.",
}


class DeliveryOutcome(Protocol):
    """What one delivery sweep achieved, addressable per delivery.

    A bare count of sends is not enough to decide whether a caller's own message went
    out: the queue is shared, so another user's successful delivery would otherwise be
    read as this caller's answer.
    """

    def sent_for(self, *, watch_id: UUID, recipient_user_id: int) -> bool: ...


class DeliveryDispatcher(Protocol):
    """Flushes and temporarily defers queued notifications."""

    async def run_once(self) -> DeliveryOutcome: ...

    def defer_initial_recurring_empty(
        self, recipient_user_id: int, watch_id: UUID
    ) -> AbstractAsyncContextManager[None]: ...


@dataclass(frozen=True, slots=True)
class CommandDeps:
    """Everything a command handler needs, bundled so every handler has one parameter.

    The wizard's own dependencies are nested rather than flattened so that wizard
    routes keep taking exactly the object they were written against.
    """

    wizard: WizardDeps
    results: ResultRepository
    deliveries: DeliveryDispatcher
    allowed_user_ids: frozenset[int]

    @property
    def database(self) -> Database:
        return self.wizard.database

    @property
    def watches(self) -> WatchService:
        return self.wizard.watches

    @property
    def checks(self) -> CheckRunner:
        return self.wizard.checks

    @property
    def clock(self) -> Clock:
        return self.wizard.clock


def _plain(text: str) -> RenderedMessage:
    return RenderedMessage(text=text, parse_mode=ParseMode.HTML, reply_markup=None)


def _authorize(update: Update, deps: CommandDeps) -> int:
    return authorized_user_id(update, deps.allowed_user_ids)


def _arguments(update: Update) -> list[str]:
    text = update.message.text if update.message is not None else None
    return [] if text is None else text.split()[1:]


def _usage(command: str) -> RenderedMessage:
    return _plain(
        f"Send {command} with the number shown in /watches, for example "
        f"<code>{command} 1</code>."
    )


async def _resolve_watch(
    update: Update, deps: CommandDeps, user_id: int, command: str
) -> Watch | RenderedMessage:
    """Turn ``/pause 2`` into the caller's second watch, or into a reply explaining why not."""
    arguments = _arguments(update)
    if not arguments:
        return _usage(command)
    try:
        index = int(arguments[0])
    except ValueError:
        return _usage(command)
    watches = await deps.watches.list_for_owner(user_id)
    if not watches:
        return _plain(_NO_WATCHES)
    if not 1 <= index <= len(watches):
        return _plain(
            f"You have no watch {index}. Send /watches to see the {len(watches)} you do have."
        )
    return watches[index - 1]


async def _watch_list(deps: CommandDeps, user_id: int) -> RenderedMessage:
    watches = await deps.watches.list_for_owner(user_id)
    if not watches:
        return _plain(_NO_WATCHES)
    return render_watch_list(watches)


def _delete_prompt(watch: Watch) -> RenderedMessage:
    title = html.escape((watch.title or "that watch")[:_MAX_TITLE_CHARS])
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="Delete",
                    callback_data=encode_callback("delete_confirm", watch.watch_id),
                ),
                InlineKeyboardButton(
                    text="Keep", callback_data=encode_callback("keep", watch.watch_id)
                ),
            ]
        ]
    )
    return RenderedMessage(
        text=f"Delete <b>{title}</b>? This cannot be undone.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def handle_help(update: Update, deps: CommandDeps) -> RenderedMessage:
    """Describe every command. Also serves ``/start``."""
    _authorize(update, deps)
    return _plain(HELP_TEXT)


async def handle_new(update: Update, deps: CommandDeps) -> RenderedMessage:
    _authorize(update, deps)
    return await wizard.start_new(update, deps.wizard)


async def handle_cancel(update: Update, deps: CommandDeps) -> RenderedMessage:
    _authorize(update, deps)
    return await wizard.cancel(update, deps.wizard)


async def handle_text(update: Update, deps: CommandDeps) -> RenderedMessage | None:
    """Feed free text to the wizard, or say plainly that nothing was expecting it."""
    _authorize(update, deps)
    reply = await wizard.handle_wizard_text(update, deps.wizard)
    return reply if reply is not None else _plain(_UNKNOWN_TEXT)


async def handle_wizard_button(update: Update, deps: CommandDeps) -> RenderedMessage | None:
    _authorize(update, deps)
    return await wizard.handle_wizard_callback(update, deps.wizard)


async def handle_watches(update: Update, deps: CommandDeps) -> RenderedMessage:
    user_id = _authorize(update, deps)
    return await _watch_list(deps, user_id)


async def handle_pause(update: Update, deps: CommandDeps) -> RenderedMessage:
    user_id = _authorize(update, deps)
    resolved = await _resolve_watch(update, deps, user_id, "/pause")
    if isinstance(resolved, RenderedMessage):
        return resolved
    try:
        await deps.watches.pause(user_id, resolved.watch_id)
    except InputError:
        return _plain(STALE_ACTION)
    return await _watch_list(deps, user_id)


async def handle_resume(update: Update, deps: CommandDeps) -> RenderedMessage:
    user_id = _authorize(update, deps)
    resolved = await _resolve_watch(update, deps, user_id, "/resume")
    if isinstance(resolved, RenderedMessage):
        return resolved
    if resolved.criteria.mode is WatchMode.ONE_OFF:
        # A one-off paused by a contract error has no schedule to restore; only an
        # explicit /check (or a delete) can move it on, so say so rather than failing.
        return _plain(_RESUME_ONE_OFF)
    try:
        await deps.watches.resume(user_id, resolved.watch_id)
    except InputError:
        return _plain(STALE_ACTION)
    return await _watch_list(deps, user_id)


async def handle_delete(update: Update, deps: CommandDeps) -> RenderedMessage:
    """Offer to delete. Nothing is removed until the confirmation callback arrives."""
    user_id = _authorize(update, deps)
    resolved = await _resolve_watch(update, deps, user_id, "/delete")
    if isinstance(resolved, RenderedMessage):
        return resolved
    return _delete_prompt(resolved)


async def handle_check(update: Update, deps: CommandDeps) -> RenderedMessage | None:
    """Run one check now and flush whatever it queued, so the answer arrives at once.

    A ``PAUSED`` recurring watch is refused: running it would leave a paused watch with
    a live schedule. A ``PAUSED`` one-off is *not* refused -- that is how a watch
    stopped by a parser change gets another go.

    The reply is suppressed only when the flush sent *this* watch's message to *this*
    caller. The queue is shared, so a global "something was sent" test would let another
    user's delivery swallow this caller's acknowledgement -- and would hide a retried or
    failed delivery behind a stranger's success.
    """
    user_id = _authorize(update, deps)
    resolved = await _resolve_watch(update, deps, user_id, "/check")
    if isinstance(resolved, RenderedMessage):
        return resolved
    if resolved.status is WatchStatus.PAUSED and resolved.criteria.mode is WatchMode.RECURRING:
        return _plain(_CHECK_PAUSED)
    try:
        result: CheckResult = await deps.checks.check(resolved.watch_id, CheckTrigger.MANUAL)
    except ConflictError:
        return _plain(_SUPERSEDED)
    except InputError:
        # The watch was deleted between listing it and checking it. The caller is
        # allowed to be here, so they get the same benign note a stale button gets
        # rather than the silence an authorization failure earns.
        return _plain(STALE_ACTION)
    run = await deps.deliveries.run_once()
    if run.sent_for(watch_id=resolved.watch_id, recipient_user_id=user_id):
        return None
    return _plain(_OUTCOME_REPLY.get(result.outcome, _OUTCOME_REPLY[CheckOutcome.SUCCESS]))


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


async def _handle_watch_action(
    action: WatchAction, deps: CommandDeps, user_id: int
) -> RenderedMessage:
    try:
        if action.action == "pause":
            await deps.watches.pause(user_id, action.watch_id)
        elif action.action == "resume":
            watch = await deps.watches.get_owned(user_id, action.watch_id)
            if watch.criteria.mode is WatchMode.ONE_OFF:
                return _plain(_RESUME_ONE_OFF)
            await deps.watches.resume(user_id, action.watch_id)
        elif action.action == "delete":
            return _delete_prompt(await deps.watches.get_owned(user_id, action.watch_id))
        elif action.action == "delete_confirm":
            await deps.watches.get_owned(user_id, action.watch_id)
            await deps.watches.delete(user_id, action.watch_id)
    except InputError:
        # A stale keyboard, a second tap, or someone else's watch id: all of them mean
        # "this button no longer describes reality", and none of them may reveal which.
        return _plain(STALE_ACTION)
    return await _watch_list(deps, user_id)


async def _handle_result_page(
    action: ResultPageAction, deps: CommandDeps, user_id: int
) -> RenderedMessage:
    try:
        async with deps.database.connection() as conn:
            watch_id: UUID | None = await deps.results.snapshot_watch_id(conn, action.snapshot_id)
        if watch_id is None:
            return _plain(RESULTS_GONE)
        await deps.watches.get_owned(user_id, watch_id)
        async with deps.database.connection() as conn:
            page = await deps.results.snapshot_page(conn, action.snapshot_id, action.page)
    except InputError:
        # Pruned, unreadable, out of range, or not the caller's snapshot -- one answer
        # for all of them, so a harvested callback payload cannot tell them apart.
        return _plain(RESULTS_GONE)
    return render_result_page(page)


async def handle_callback(update: Update, deps: CommandDeps) -> RenderedMessage | None:
    """Route one ``v1:`` callback to the action it encodes.

    Returns ``None`` only when the update carries no data at all. Anything malformed,
    stale, or unowned is answered with the same benign note: callback data is replayable
    by anyone with a Telegram client, so a decode failure is a normal event, not a bug.
    """
    user_id = _authorize(update, deps)
    query = update.callback_query
    data = query.data if query is not None else None
    if not data:
        return None
    try:
        action = decode_callback(data)
    except InputError:
        return _plain(STALE_ACTION)
    if isinstance(action, ResultPageAction):
        return await _handle_result_page(action, deps, user_id)
    if action.action == "keep":
        return await _watch_list(deps, user_id)
    return await _handle_watch_action(action, deps, user_id)
