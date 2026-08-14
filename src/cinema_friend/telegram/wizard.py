"""Persistent, resumable Telegram wizard for creating a watch.

The wizard is a linear state machine driven entirely by :class:`ConversationDraft` rows:
every handler reads the current draft, validates exactly one field, and writes the new
state/payload back before replying. Nothing about "where the user is" lives in process
memory, so a restart between two messages loses nothing -- the next message just
resumes from whatever was last persisted.

Two state families exist. Free-text states (``AWAIT_URL`` through ``AWAIT_INTERVAL``)
are advanced by :func:`handle_wizard_text`; button-driven states (``AWAIT_TIME_MODE``,
``AWAIT_QUANTITY``, ``AWAIT_SEAT_MODE``, ``AWAIT_SIMPLE_SEAT_PREFERENCE``, ``AWAIT_MODE``,
and the confirm/cancel actions on ``REVIEW``) are advanced by :func:`handle_wizard_callback`.
Invalid input in either family leaves the persisted draft completely unchanged and
replies with the same message the parser raised, which already names the accepted
shape.

Confirmation is the one step with effects outside the draft table -- a watch row and a
creation check -- so it is written as a saga rather than a single transition. A watch
and its draft live behind separate connections and cannot be committed together, which
leaves durable intermediate states no matter how the code is arranged; the saga's job
is to make every one of them converge on the same outcome.

Confirmation first *claims* the draft, flipping ``REVIEW`` to the internal
``CONFIRMING`` marker inside one ``BEGIN IMMEDIATE`` transaction that also persists a
``setup_id``. SQLite serialises that transaction, so a concurrent duplicate always
loses the claim. The setup id then yields a deterministic ``watch_id`` (a UUID5 of it),
which is what makes retrying safe: re-running :meth:`WatchService.create` after a crash
lands on the same row instead of inserting a second watch. Each completed step records
its :class:`ConfirmPhase` on the draft, so a resumed attempt skips work already done --
notably it never re-runs a creation check that already ran.

Three things end a claim. Success deletes the draft. A routine failure (network,
persistence) compensates: the draft goes back to ``REVIEW``, keeping its phase markers,
and the reply describes what actually happened -- a failure after the watch exists must
not tell the user nothing was saved. A crash leaves the claim in place, and the claim
carries a :data:`CLAIM_LEASE`: once it expires a retried Confirm resumes the saga, and
:func:`recover_confirmations` does the same at startup without waiting for the user.
"""

from __future__ import annotations

import html
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import Enum
from typing import Any, Protocol
from uuid import UUID, uuid4, uuid5

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode

from cinema_friend.bfi.urls import parse_article_url
from cinema_friend.clock import Clock
from cinema_friend.domain.errors import (
    BfiChallengeError,
    BfiContractError,
    BfiNetworkError,
    CircuitOpenError,
    ConflictError,
    DeliveryError,
    InputError,
    PersistenceError,
)
from cinema_friend.domain.results import CheckResult
from cinema_friend.domain.state import CheckTrigger, SeatPreferenceStrategy, WatchMode
from cinema_friend.domain.time_window import (
    LONDON,
    DailyTimeWindow,
    window_for_local_date,
    within_daily_window,
)
from cinema_friend.domain.watch import WatchCriteria
from cinema_friend.services.watch_service import WatchService
from cinema_friend.storage.database import Database
from cinema_friend.storage.draft_repository import ConversationDraft, DraftRepository
from cinema_friend.telegram.rendering import RenderedMessage

#: The known BFI row width; a same-row seat range or a bare seat number beyond this is
#: rejected rather than silently accepted, so an absurd input cannot build an unbounded
#: preferred/excluded seat set.
MAX_ROW_WIDTH = 40

#: Matches :class:`WatchCriteria`'s own floor for a recurring watch's interval.
MIN_INTERVAL_MINUTES = 15

#: How long a confirmation claim is assumed to still be in flight. Past this, the
#: claiming process is presumed dead and the saga may be resumed by anyone. It has to
#: exceed the worst realistic time for one creation check, or a slow check would have
#: its own work stolen; each completed phase refreshes the draft, renewing the lease.
CLAIM_LEASE = timedelta(minutes=5)

#: Namespace for deriving a watch id from a draft's ``setup_id``. Fixed forever: change
#: it and in-flight confirmations would resume into a *second* watch.
_WATCH_NAMESPACE = UUID("6f3f7a0e-2c56-4d5b-9b1a-0d5f9f5a1c77")

_SKIP = "skip"

#: Marks a draft created by the current (branching) wizard. Persisted at the very
#: start of a new draft so every later transition can tell a fresh draft from a
#: legacy one -- created before this branch existed -- without guessing from which
#: keys happen to be present.
_SEAT_FLOW_VERSION = 2
_SEAT_FLOW_VERSION_KEY = "seat_flow_version"
_SEAT_PREFERENCE_KEY = "seat_preference_strategy"

_TIME_FLOW_VERSION = 2
_TIME_FLOW_VERSION_KEY = "time_flow_version"


class TimeSetupMode(str, Enum):
    """Which of the two time-window experiences the user asked for."""

    SAME_EVERY_DAY = "same"
    WEEKDAY_WEEKEND = "split"


class SeatSetupMode(str, Enum):
    """Which of the two seat-selection experiences the user asked for."""

    SIMPLE = "simple"
    ADVANCED = "advanced"


class ConfirmPhase(str, Enum):
    """How far a claimed confirmation got before it stopped.

    Persisted on the draft after each step completes, so a resumed attempt can tell
    "not done yet" from "already done" for every effect outside the draft table.
    """

    CLAIMED = "claimed"
    WATCH_CREATED = "watch_created"
    CHECK_STARTED = "check_started"
    CHECK_DONE = "check_done"


class WizardState(str, Enum):
    """One step of the guided watch-creation conversation.

    ``CONFIRMING`` is never prompted to the user; it is the claimed, in-flight marker
    held for the duration of the confirmation saga, so a duplicate confirmation can
    recognise an attempt already under way and a resumed one can find work left behind.
    """

    AWAIT_URL = "await_url"
    AWAIT_DATE_RANGE = "await_date_range"
    AWAIT_TIME_MODE = "await_time_mode"
    AWAIT_TIME_RANGE = "await_time_range"
    AWAIT_WEEKDAY_TIME_RANGE = "await_weekday_time_range"
    AWAIT_WEEKEND_TIME_RANGE = "await_weekend_time_range"
    AWAIT_QUANTITY = "await_quantity"
    AWAIT_SEAT_MODE = "await_seat_mode"
    AWAIT_SIMPLE_SEAT_PREFERENCE = "await_simple_seat_preference"
    AWAIT_PREFERRED_ROWS = "await_preferred_rows"
    AWAIT_PREFERRED_SEATS = "await_preferred_seats"
    AWAIT_EXCLUDED_ROWS = "await_excluded_rows"
    AWAIT_EXCLUDED_SEATS = "await_excluded_seats"
    AWAIT_PREFERRED_INSTANT = "await_preferred_instant"
    AWAIT_MODE = "await_mode"
    AWAIT_INTERVAL = "await_interval"
    REVIEW = "review"
    CONFIRMING = "confirming"


class CheckRunner(Protocol):
    """The slice of :class:`~cinema_friend.services.check_service.CheckService` the
    wizard needs: run one check and report what happened. Kept narrow so a test can
    substitute a recording fake instead of standing up the full BFI gateway.
    """

    async def check(self, watch_id: UUID, trigger: CheckTrigger) -> CheckResult: ...


@dataclass(frozen=True, slots=True)
class WizardDeps:
    """Everything a wizard handler needs, bundled so every handler has one parameter."""

    database: Database
    drafts: DraftRepository
    watches: WatchService
    checks: CheckRunner
    clock: Clock


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

_DATE_TOKEN = r"\d{4}-\d{2}-\d{2}"
_DATE_RANGE_RE = re.compile(rf"({_DATE_TOKEN})\s+to\s+({_DATE_TOKEN})", re.IGNORECASE)
_DATE_RANGE_EXAMPLE = "send a date range like 2026-08-26 to 2026-08-30"


def parse_date_range(text: str) -> tuple[date, date]:
    """Parse ``YYYY-MM-DD to YYYY-MM-DD`` into an ordered ``(date_from, date_to)`` pair."""
    match = _DATE_RANGE_RE.fullmatch(text.strip())
    if match is None:
        raise InputError(_DATE_RANGE_EXAMPLE)
    try:
        date_from = date.fromisoformat(match.group(1))
        date_to = date.fromisoformat(match.group(2))
    except ValueError as exc:
        raise InputError(_DATE_RANGE_EXAMPLE) from exc
    if date_from > date_to:
        raise InputError(f"the first date must not be after the second; {_DATE_RANGE_EXAMPLE}")
    return date_from, date_to


_TIME_TOKEN = r"(\d{1,2}):(\d{2})"
_TIME_RANGE_RE = re.compile(rf"{_TIME_TOKEN}\s+to\s+{_TIME_TOKEN}", re.IGNORECASE)
_TIME_RANGE_EXAMPLE = (
    "send a time window like 18:00 to 23:00 (it may cross midnight, e.g. 22:00 to 01:00)"
)


def _clock_time(hour_text: str, minute_text: str) -> time:
    try:
        return time(int(hour_text), int(minute_text))
    except ValueError as exc:
        raise InputError(_TIME_RANGE_EXAMPLE) from exc


def parse_time_range(text: str) -> tuple[time, time]:
    """Parse ``HH:MM to HH:MM`` into a ``(time_from, time_to)`` pair.

    A window where ``time_from > time_to`` (e.g. ``22:00 to 01:00``) is accepted as-is:
    it means the window crosses midnight, matching how
    :func:`~cinema_friend.watches.criteria.performance_matches` already interprets it.
    """
    match = _TIME_RANGE_RE.fullmatch(text.strip())
    if match is None:
        raise InputError(_TIME_RANGE_EXAMPLE)
    time_from = _clock_time(match.group(1), match.group(2))
    time_to = _clock_time(match.group(3), match.group(4))
    return time_from, time_to


_ROW_ONLY_RE = re.compile(r"[A-Za-z]+")
_SEAT_RE = re.compile(r"([A-Za-z]+)(\d{1,2})")
_ROW_EXAMPLE = "send comma-separated row letters like L,M"
_SEAT_EXAMPLE = f"send comma-separated seats or same-row ranges like L16-L22,M17 (rows are at most {MAX_ROW_WIDTH} seats wide)"


def _parse_row_token(token: str, example: str) -> str:
    if not _ROW_ONLY_RE.fullmatch(token):
        raise InputError(example)
    return token.upper()


def _seat_label(row: str, number: int, example: str) -> str:
    if not (1 <= number <= MAX_ROW_WIDTH):
        raise InputError(example)
    return f"{row.upper()}{number}"


def _parse_seat_token(token: str, example: str) -> frozenset[str]:
    if "-" in token:
        left, _, right = token.partition("-")
        left_match = _SEAT_RE.fullmatch(left)
        right_match = _SEAT_RE.fullmatch(right)
        if left_match is None or right_match is None:
            raise InputError(example)
        left_row, left_num = left_match.group(1).upper(), int(left_match.group(2))
        right_row, right_num = right_match.group(1).upper(), int(right_match.group(2))
        if left_row != right_row or left_num > right_num:
            raise InputError(example)
        if right_num - left_num + 1 > MAX_ROW_WIDTH:
            raise InputError(example)
        return frozenset(
            _seat_label(left_row, number, example) for number in range(left_num, right_num + 1)
        )
    match = _SEAT_RE.fullmatch(token)
    if match is None:
        raise InputError(example)
    return frozenset({_seat_label(match.group(1), int(match.group(2)), example)})


def parse_seat_selectors(text: str, *, kind: str) -> frozenset[str]:
    """Parse a comma-separated list of rows (``kind="row"``) or seats/ranges
    (``kind="seat"``) into normalized uppercase labels matching ranking's own (e.g.
    ``L17``, ``L16-L22`` expanding to ``L16``..``L22``).

    A same-row range is expanded inclusively and capped at :data:`MAX_ROW_WIDTH` seats
    -- the known BFI row width -- so an absurd range (or a seat number beyond it)
    cannot be used to build an unbounded set.
    """
    example = _ROW_EXAMPLE if kind == "row" else _SEAT_EXAMPLE
    tokens = [token.strip() for token in text.split(",")]
    if not tokens or any(not token for token in tokens):
        raise InputError(example)
    labels: set[str] = set()
    for token in tokens:
        if kind == "row":
            labels.add(_parse_row_token(token, example))
        else:
            labels.update(_parse_seat_token(token, example))
    return frozenset(labels)


_INTERVAL_EXAMPLE = (
    f"send the recheck interval in whole minutes, at least {MIN_INTERVAL_MINUTES}, e.g. 30"
)


def parse_interval(text: str) -> timedelta:
    """Parse a plain integer number of minutes, rejecting anything below the floor
    :class:`WatchCriteria` itself enforces for a recurring watch.
    """
    stripped = text.strip()
    if not stripped.isdigit():
        raise InputError(_INTERVAL_EXAMPLE)
    minutes = int(stripped)
    if minutes < MIN_INTERVAL_MINUTES:
        raise InputError(_INTERVAL_EXAMPLE)
    return timedelta(minutes=minutes)


_INSTANT_EXAMPLE = (
    "send a preferred London date and time like 2026-08-27 20:00, within your date "
    "and time range, or send 'skip'"
)


def _is_skip(text: str) -> bool:
    return text.strip().lower() == _SKIP


def _weekend_window(payload: Mapping[str, Any]) -> DailyTimeWindow | None:
    """Extract the optional weekend window from a draft payload.

    Returns ``None`` when neither bound is present (uniform schedule). Raises
    :class:`InputError` when only one bound is present (invalid state).
    """
    time_from_raw = payload.get("weekend_time_from")
    time_to_raw = payload.get("weekend_time_to")
    if (time_from_raw is None) != (time_to_raw is None):
        raise InputError("weekend time range requires both start and end")
    if time_from_raw is None or time_to_raw is None:
        return None
    return time.fromisoformat(time_from_raw), time.fromisoformat(time_to_raw)


def _parse_preferred_instant(text: str, payload: Mapping[str, Any]) -> datetime | None:
    """Parse the optional preferred instant, or ``None`` for an explicit skip.

    The user types a wall clock, and the only wall clock in play is the cinema's, so
    the text is read as ``Europe/London`` local time and validated against the local
    date and daily window before being converted to UTC for storage. Reading it as UTC
    instead would silently shift every summer preference by an hour, and would validate
    against the wrong day for anything near midnight.

    Validation mirrors :meth:`WatchCriteria.__post_init__` exactly -- same conversion,
    same shared window predicate -- so accepting input here can never be overturned by
    that later check. A time that does not exist locally (the spring-forward gap) is
    rejected: silently sliding it an hour would be a different showing.
    """
    if _is_skip(text):
        return None
    try:
        naive = datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")  # noqa: DTZ007
    except ValueError as exc:
        raise InputError(_INSTANT_EXAMPLE) from exc
    local = naive.replace(tzinfo=LONDON)
    if local.astimezone(UTC).astimezone(LONDON).replace(tzinfo=None) != naive:
        raise InputError(_INSTANT_EXAMPLE)
    date_from = date.fromisoformat(payload["date_from"])
    date_to = date.fromisoformat(payload["date_to"])
    if not (date_from <= local.date() <= date_to):
        raise InputError(_INSTANT_EXAMPLE)
    time_from, time_to = window_for_local_date(
        local.date(),
        default_window=(
            time.fromisoformat(payload["time_from"]),
            time.fromisoformat(payload["time_to"]),
        ),
        weekend_window=_weekend_window(payload),
    )
    if not within_daily_window(time_from, time_to, local.time()):
        raise InputError(_INSTANT_EXAMPLE)
    return local.astimezone(UTC)


def _parse_optional_selectors(text: str, *, kind: str) -> list[str]:
    if _is_skip(text):
        return []
    return sorted(parse_seat_selectors(text, kind=kind))


def _parse_url(text: str) -> tuple[str, str]:
    """Validate and canonicalize the film page URL via the shared BFI URL parser."""
    article = parse_article_url(text.strip())
    return article.canonical_url, article.slug


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_URL_PROMPT = (
    "Send the BFI film page URL you want to watch, e.g. "
    "https://whatson.bfi.org.uk/imax/Online/article/dog-stars"
)
_DATE_RANGE_PROMPT = f"What date range should I watch? {_DATE_RANGE_EXAMPLE}."
_TIME_RANGE_PROMPT = f"What daily time window? {_TIME_RANGE_EXAMPLE}."
_WEEKDAY_TIME_RANGE_PROMPT = f"What Monday-Friday time window? {_TIME_RANGE_EXAMPLE}."
_WEEKEND_TIME_RANGE_PROMPT = f"What Saturday-Sunday time window? {_TIME_RANGE_EXAMPLE}."
_PREFERRED_ROWS_PROMPT = (
    f"Any preferred rows? {_ROW_EXAMPLE}, or send 'skip' for no preference."
)
_PREFERRED_SEATS_PROMPT = (
    f"Any preferred seats? {_SEAT_EXAMPLE}, or send 'skip' for no preference."
)
_EXCLUDED_ROWS_PROMPT = f"Any rows to exclude? {_ROW_EXAMPLE}, or send 'skip'."
_EXCLUDED_SEATS_PROMPT = f"Any seats to exclude? {_SEAT_EXAMPLE}, or send 'skip'."
_PREFERRED_INSTANT_PROMPT = f"{_INSTANT_EXAMPLE.capitalize()}."
_INTERVAL_PROMPT = f"{_INTERVAL_EXAMPLE.capitalize()}."

_QTY_PREFIX = "wizard:qty:"
_TIME_MODE_PREFIX = "wizard:time-mode:"
_SEAT_MODE_PREFIX = "wizard:seat-mode:"
_SEAT_PREFERENCE_PREFIX = "wizard:seat-preference:"
_MODE_PREFIX = "wizard:mode:"
_CONFIRM = "wizard:confirm"
_CANCEL = "wizard:cancel"


def _text_prompt(text: str) -> RenderedMessage:
    return RenderedMessage(text=text, parse_mode=ParseMode.HTML, reply_markup=None)


def _quantity_prompt() -> RenderedMessage:
    buttons = [
        InlineKeyboardButton(text=str(n), callback_data=f"{_QTY_PREFIX}{n}") for n in range(1, 9)
    ]
    rows = [buttons[0:4], buttons[4:8]]
    return RenderedMessage(
        text="How many seats do you need? Choose 1-8.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


def _time_mode_prompt() -> RenderedMessage:
    rows = [
        [
            InlineKeyboardButton(
                text="Same every day",
                callback_data=f"{_TIME_MODE_PREFIX}{TimeSetupMode.SAME_EVERY_DAY.value}",
            )
        ],
        [
            InlineKeyboardButton(
                text="Weekday + weekend",
                callback_data=f"{_TIME_MODE_PREFIX}{TimeSetupMode.WEEKDAY_WEEKEND.value}",
            )
        ],
    ]
    return RenderedMessage(
        text="Use one viewing-time window every day, or separate weekday and weekend windows?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


def _seat_mode_prompt() -> RenderedMessage:
    rows = [
        [
            InlineKeyboardButton(
                text="Simple",
                callback_data=f"{_SEAT_MODE_PREFIX}{SeatSetupMode.SIMPLE.value}",
            ),
            InlineKeyboardButton(
                text="Advanced",
                callback_data=f"{_SEAT_MODE_PREFIX}{SeatSetupMode.ADVANCED.value}",
            ),
        ]
    ]
    return RenderedMessage(
        text="How would you like to choose acceptable seats?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


def _simple_seat_preference_prompt() -> RenderedMessage:
    rows = [
        [
            InlineKeyboardButton(
                text="Only the best",
                callback_data=(
                    f"{_SEAT_PREFERENCE_PREFIX}"
                    f"{SeatPreferenceStrategy.ONLY_BEST.value}"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                text="Best and good",
                callback_data=(
                    f"{_SEAT_PREFERENCE_PREFIX}"
                    f"{SeatPreferenceStrategy.BEST_AND_GOOD.value}"
                ),
            )
        ],
    ]
    return RenderedMessage(
        text=(
            "Choose a simple seat preference:\n\n"
            "<b>Only the best</b>: middle seating bank between the aisles, "
            "row J or farther back.\n"
            "<b>Best and good</b>: the same middle bank, row C or farther back."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


def _mode_prompt() -> RenderedMessage:
    rows = [
        [
            InlineKeyboardButton(
                text="One-off", callback_data=f"{_MODE_PREFIX}{WatchMode.ONE_OFF.value}"
            ),
            InlineKeyboardButton(
                text="Recurring", callback_data=f"{_MODE_PREFIX}{WatchMode.RECURRING.value}"
            ),
        ]
    ]
    return RenderedMessage(
        text="Should this watch run once, or keep rechecking on an interval?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


def _seat_preference_strategy(
    payload: Mapping[str, Any],
) -> SeatPreferenceStrategy:
    """Resolve the draft's seat preference strategy, defaulting to Advanced.

    A legacy draft -- one never given a ``seat_preference_strategy`` because it was
    created before this branch existed -- reads as Advanced, matching the only
    behaviour it ever had.
    """
    raw = payload.get(
        _SEAT_PREFERENCE_KEY,
        SeatPreferenceStrategy.ADVANCED.value,
    )
    try:
        return SeatPreferenceStrategy(raw)
    except (TypeError, ValueError) as exc:
        raise InputError("seat preference strategy is invalid") from exc


def _build_criteria(payload: Mapping[str, Any]) -> WatchCriteria:
    """Rebuild and revalidate a complete :class:`WatchCriteria` from a draft's payload.

    Runs at every transition into ``REVIEW`` (to render the summary) and again at
    confirmation, so a stale or hand-edited payload can never reach
    :meth:`WatchService.create` without passing every invariant the domain type itself
    enforces.
    """
    interval_minutes = payload.get("interval_minutes")
    preferred_instant_raw = payload.get("preferred_utc_instant")
    weekend_w = _weekend_window(payload)
    return WatchCriteria(
        source_url=payload["source_url"],
        slug=payload["slug"],
        date_from=date.fromisoformat(payload["date_from"]),
        date_to=date.fromisoformat(payload["date_to"]),
        time_from=time.fromisoformat(payload["time_from"]),
        time_to=time.fromisoformat(payload["time_to"]),
        quantity=payload["quantity"],
        mode=WatchMode(payload["mode"]),
        interval=timedelta(minutes=interval_minutes) if interval_minutes is not None else None,
        preferred_rows=frozenset(payload.get("preferred_rows", [])),
        preferred_seats=frozenset(payload.get("preferred_seats", [])),
        excluded_rows=frozenset(payload.get("excluded_rows", [])),
        excluded_seats=frozenset(payload.get("excluded_seats", [])),
        preferred_utc_instant=(
            datetime.fromisoformat(preferred_instant_raw) if preferred_instant_raw else None
        ),
        seat_preference_strategy=_seat_preference_strategy(payload),
        weekend_time_from=weekend_w[0] if weekend_w is not None else None,
        weekend_time_to=weekend_w[1] if weekend_w is not None else None,
    )


def _review_prompt(payload: Mapping[str, Any]) -> RenderedMessage:
    criteria = _build_criteria(payload)
    lines = [
        "<b>Review your watch</b>",
        f"Film: {html.escape(criteria.slug)}",
        f"Dates: {criteria.date_from.isoformat()} to {criteria.date_to.isoformat()}",
    ]
    if criteria.weekend_time_from is None or criteria.weekend_time_to is None:
        lines.append(
            f"Times: {criteria.time_from.strftime('%H:%M')} "
            f"to {criteria.time_to.strftime('%H:%M')} daily"
        )
    else:
        lines.extend(
            [
                (
                    f"Weekdays: {criteria.time_from.strftime('%H:%M')} "
                    f"to {criteria.time_to.strftime('%H:%M')}"
                ),
                (
                    f"Weekends: {criteria.weekend_time_from.strftime('%H:%M')} "
                    f"to {criteria.weekend_time_to.strftime('%H:%M')}"
                ),
            ]
        )
    lines.append(f"Seats: {criteria.quantity}")
    if criteria.seat_preference_strategy is SeatPreferenceStrategy.ONLY_BEST:
        lines.append("Seat preference: Only the best")
    elif criteria.seat_preference_strategy is SeatPreferenceStrategy.BEST_AND_GOOD:
        lines.append("Seat preference: Best and good")
    else:
        if criteria.preferred_rows:
            lines.append(f"Preferred rows: {', '.join(sorted(criteria.preferred_rows))}")
        if criteria.preferred_seats:
            lines.append(f"Preferred seats: {', '.join(sorted(criteria.preferred_seats))}")
        if criteria.excluded_rows:
            lines.append(f"Excluded rows: {', '.join(sorted(criteria.excluded_rows))}")
        if criteria.excluded_seats:
            lines.append(f"Excluded seats: {', '.join(sorted(criteria.excluded_seats))}")
    if criteria.preferred_utc_instant is not None:
        when = criteria.preferred_utc_instant.astimezone(LONDON).strftime("%Y-%m-%d %H:%M")
        lines.append(f"Preferred time (London): {when}")
    lines.append(f"Mode: {'recurring' if criteria.mode is WatchMode.RECURRING else 'one-off'}")
    if criteria.interval is not None:
        lines.append(f"Recheck interval: {int(criteria.interval.total_seconds() // 60)} minutes")
    lines.append("")
    lines.append("Tap Confirm to create this watch, or Cancel to discard it.")
    rows = [
        [
            InlineKeyboardButton(text="Confirm", callback_data=_CONFIRM),
            InlineKeyboardButton(text="Cancel", callback_data=_CANCEL),
        ]
    ]
    return RenderedMessage(
        text="\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows)
    )


def _prompt_for(state: WizardState, payload: Mapping[str, Any]) -> RenderedMessage:
    if state is WizardState.AWAIT_URL:
        return _text_prompt(_URL_PROMPT)
    if state is WizardState.AWAIT_DATE_RANGE:
        return _text_prompt(_DATE_RANGE_PROMPT)
    if state is WizardState.AWAIT_TIME_MODE:
        return _time_mode_prompt()
    if state is WizardState.AWAIT_TIME_RANGE:
        return _text_prompt(_TIME_RANGE_PROMPT)
    if state is WizardState.AWAIT_WEEKDAY_TIME_RANGE:
        return _text_prompt(_WEEKDAY_TIME_RANGE_PROMPT)
    if state is WizardState.AWAIT_WEEKEND_TIME_RANGE:
        return _text_prompt(_WEEKEND_TIME_RANGE_PROMPT)
    if state is WizardState.AWAIT_QUANTITY:
        return _quantity_prompt()
    if state is WizardState.AWAIT_SEAT_MODE:
        return _seat_mode_prompt()
    if state is WizardState.AWAIT_SIMPLE_SEAT_PREFERENCE:
        return _simple_seat_preference_prompt()
    if state is WizardState.AWAIT_PREFERRED_ROWS:
        return _text_prompt(_PREFERRED_ROWS_PROMPT)
    if state is WizardState.AWAIT_PREFERRED_SEATS:
        return _text_prompt(_PREFERRED_SEATS_PROMPT)
    if state is WizardState.AWAIT_EXCLUDED_ROWS:
        return _text_prompt(_EXCLUDED_ROWS_PROMPT)
    if state is WizardState.AWAIT_EXCLUDED_SEATS:
        return _text_prompt(_EXCLUDED_SEATS_PROMPT)
    if state is WizardState.AWAIT_PREFERRED_INSTANT:
        return _text_prompt(_PREFERRED_INSTANT_PROMPT)
    if state is WizardState.AWAIT_MODE:
        return _mode_prompt()
    if state is WizardState.AWAIT_INTERVAL:
        return _text_prompt(_INTERVAL_PROMPT)
    return _review_prompt(payload)


def _retry_prompt(message: str) -> RenderedMessage:
    return RenderedMessage(text=html.escape(message), parse_mode=ParseMode.HTML, reply_markup=None)


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------

_TEXT_STATES = frozenset(
    {
        WizardState.AWAIT_URL,
        WizardState.AWAIT_DATE_RANGE,
        WizardState.AWAIT_TIME_RANGE,
        WizardState.AWAIT_WEEKDAY_TIME_RANGE,
        WizardState.AWAIT_WEEKEND_TIME_RANGE,
        WizardState.AWAIT_PREFERRED_ROWS,
        WizardState.AWAIT_PREFERRED_SEATS,
        WizardState.AWAIT_EXCLUDED_ROWS,
        WizardState.AWAIT_EXCLUDED_SEATS,
        WizardState.AWAIT_PREFERRED_INSTANT,
        WizardState.AWAIT_INTERVAL,
    }
)

_CALLBACK_STATES = frozenset(
    {
        WizardState.AWAIT_TIME_MODE,
        WizardState.AWAIT_QUANTITY,
        WizardState.AWAIT_SEAT_MODE,
        WizardState.AWAIT_SIMPLE_SEAT_PREFERENCE,
        WizardState.AWAIT_MODE,
    }
)


def _apply_text(
    state: WizardState, payload: dict[str, Any], text: str
) -> tuple[WizardState, dict[str, Any]]:
    if state is WizardState.AWAIT_URL:
        canonical_url, slug = _parse_url(text)
        payload["source_url"] = canonical_url
        payload["slug"] = slug
        return WizardState.AWAIT_DATE_RANGE, payload
    if state is WizardState.AWAIT_DATE_RANGE:
        date_from, date_to = parse_date_range(text)
        payload["date_from"] = date_from.isoformat()
        payload["date_to"] = date_to.isoformat()
        if payload.get(_TIME_FLOW_VERSION_KEY) == _TIME_FLOW_VERSION:
            return WizardState.AWAIT_TIME_MODE, payload
        return WizardState.AWAIT_TIME_RANGE, payload
    if state in (WizardState.AWAIT_TIME_RANGE, WizardState.AWAIT_WEEKDAY_TIME_RANGE):
        time_from, time_to = parse_time_range(text)
        payload["time_from"] = time_from.isoformat()
        payload["time_to"] = time_to.isoformat()
        if state is WizardState.AWAIT_WEEKDAY_TIME_RANGE:
            return WizardState.AWAIT_WEEKEND_TIME_RANGE, payload
        return WizardState.AWAIT_QUANTITY, payload
    if state is WizardState.AWAIT_WEEKEND_TIME_RANGE:
        time_from, time_to = parse_time_range(text)
        payload["weekend_time_from"] = time_from.isoformat()
        payload["weekend_time_to"] = time_to.isoformat()
        return WizardState.AWAIT_QUANTITY, payload
    if state is WizardState.AWAIT_PREFERRED_ROWS:
        payload["preferred_rows"] = _parse_optional_selectors(text, kind="row")
        return WizardState.AWAIT_PREFERRED_SEATS, payload
    if state is WizardState.AWAIT_PREFERRED_SEATS:
        payload["preferred_seats"] = _parse_optional_selectors(text, kind="seat")
        return WizardState.AWAIT_EXCLUDED_ROWS, payload
    if state is WizardState.AWAIT_EXCLUDED_ROWS:
        payload["excluded_rows"] = _parse_optional_selectors(text, kind="row")
        return WizardState.AWAIT_EXCLUDED_SEATS, payload
    if state is WizardState.AWAIT_EXCLUDED_SEATS:
        payload["excluded_seats"] = _parse_optional_selectors(text, kind="seat")
        return WizardState.AWAIT_PREFERRED_INSTANT, payload
    if state is WizardState.AWAIT_PREFERRED_INSTANT:
        instant = _parse_preferred_instant(text, payload)
        payload["preferred_utc_instant"] = instant.isoformat() if instant else None
        return WizardState.AWAIT_MODE, payload
    # Only AWAIT_INTERVAL remains among _TEXT_STATES.
    interval = parse_interval(text)
    payload["interval_minutes"] = int(interval.total_seconds() // 60)
    return WizardState.REVIEW, payload


def _apply_callback(
    state: WizardState, payload: dict[str, Any], data: str
) -> tuple[WizardState, dict[str, Any]]:
    if state is WizardState.AWAIT_TIME_MODE:
        if not data.startswith(_TIME_MODE_PREFIX):
            raise InputError("choose one daily window or separate weekday and weekend windows")
        try:
            time_mode = TimeSetupMode(data[len(_TIME_MODE_PREFIX) :])
        except ValueError as exc:
            raise InputError(
                "choose one daily window or separate weekday and weekend windows"
            ) from exc
        if time_mode is TimeSetupMode.SAME_EVERY_DAY:
            payload.pop("weekend_time_from", None)
            payload.pop("weekend_time_to", None)
            return WizardState.AWAIT_TIME_RANGE, payload
        return WizardState.AWAIT_WEEKDAY_TIME_RANGE, payload

    if state is WizardState.AWAIT_QUANTITY:
        if not data.startswith(_QTY_PREFIX):
            raise InputError("choose a quantity using the buttons above")
        try:
            quantity = int(data[len(_QTY_PREFIX) :])
        except ValueError as exc:
            raise InputError("choose a quantity using the buttons above") from exc
        if not (1 <= quantity <= 8):
            raise InputError("choose a quantity between 1 and 8")
        payload["quantity"] = quantity
        if payload.get(_SEAT_FLOW_VERSION_KEY) == _SEAT_FLOW_VERSION:
            return WizardState.AWAIT_SEAT_MODE, payload
        # A legacy draft -- no flow marker -- keeps its only historical behaviour:
        # straight into the manual (Advanced) seat prompts.
        payload[_SEAT_PREFERENCE_KEY] = SeatPreferenceStrategy.ADVANCED.value
        return WizardState.AWAIT_PREFERRED_ROWS, payload

    if state is WizardState.AWAIT_SEAT_MODE:
        if not data.startswith(_SEAT_MODE_PREFIX):
            raise InputError("choose Simple or Advanced using the buttons above")
        try:
            seat_mode = SeatSetupMode(data[len(_SEAT_MODE_PREFIX) :])
        except ValueError as exc:
            raise InputError(
                "choose Simple or Advanced using the buttons above"
            ) from exc
        if seat_mode is SeatSetupMode.ADVANCED:
            payload[_SEAT_PREFERENCE_KEY] = SeatPreferenceStrategy.ADVANCED.value
            return WizardState.AWAIT_PREFERRED_ROWS, payload
        return WizardState.AWAIT_SIMPLE_SEAT_PREFERENCE, payload

    if state is WizardState.AWAIT_SIMPLE_SEAT_PREFERENCE:
        if not data.startswith(_SEAT_PREFERENCE_PREFIX):
            raise InputError("choose one of the seat preferences above")
        try:
            strategy = SeatPreferenceStrategy(
                data[len(_SEAT_PREFERENCE_PREFIX) :]
            )
        except ValueError as exc:
            raise InputError("choose one of the seat preferences above") from exc
        if strategy is SeatPreferenceStrategy.ADVANCED:
            raise InputError("choose one of the seat preferences above")
        payload[_SEAT_PREFERENCE_KEY] = strategy.value
        for key in (
            "preferred_rows",
            "preferred_seats",
            "excluded_rows",
            "excluded_seats",
        ):
            payload.pop(key, None)
        return WizardState.AWAIT_PREFERRED_INSTANT, payload

    # Only AWAIT_MODE remains among _CALLBACK_STATES.
    if not data.startswith(_MODE_PREFIX):
        raise InputError("choose one-off or recurring using the buttons above")
    mode_value = data[len(_MODE_PREFIX) :]
    try:
        mode = WatchMode(mode_value)
    except ValueError as exc:
        raise InputError("choose one-off or recurring using the buttons above") from exc
    payload["mode"] = mode.value
    if mode is WatchMode.RECURRING:
        return WizardState.AWAIT_INTERVAL, payload
    payload["interval_minutes"] = None
    return WizardState.REVIEW, payload


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

_NO_USER = "update carries no user"
_IN_FLIGHT = (
    "I'm still setting this watch up from your last tap. Give me a moment -- tap "
    "Confirm again if nothing arrives."
)
_FAILED_BEFORE_CREATE = (
    "Something went wrong and nothing was saved. Tap Confirm again to retry."
)
_CANCELLED_MID_CONFIRM = (
    "Setup cancelled. That confirmation never finished, so a watch may already have "
    "been created -- send /watches to check."
)
_FAILED_AFTER_CREATE = (
    "Your watch is saved, but I couldn't finish setting it up. Tap Confirm again to "
    "finish -- this will not create a second watch."
)


def _require_user(update: Update) -> int:
    user = update.effective_user
    if user is None:
        raise InputError(_NO_USER)
    return user.id


async def start_new(update: Update, deps: WizardDeps) -> RenderedMessage:
    """Start (or restart) the wizard for this user, discarding any prior draft.

    ``DraftRepository.upsert`` fully replaces a user's existing row, so this is always
    a clean restart regardless of what state a previous, abandoned attempt was in.
    """
    user_id = _require_user(update)
    payload = {
        _SEAT_FLOW_VERSION_KEY: _SEAT_FLOW_VERSION,
        _TIME_FLOW_VERSION_KEY: _TIME_FLOW_VERSION,
    }
    async with deps.database.connection() as conn:
        await deps.drafts.upsert(
            conn,
            user_id,
            WizardState.AWAIT_URL.value,
            payload,
            deps.clock.now(),
        )
    return _prompt_for(WizardState.AWAIT_URL, payload)


async def cancel(update: Update, deps: WizardDeps) -> RenderedMessage:
    """Discard this user's in-progress draft, unless a confirmation is still running.

    A draft in ``CONFIRMING`` is a claimed saga, not an idle form: deleting it would
    strand a watch that may already exist and would let the same confirmation run twice.
    A live claim is therefore refused with the same in-progress wording the Confirm
    button gives. Once the lease has expired the draft is cancellable again, but the
    reply says so honestly -- the saga may have got far enough to create the watch, and
    only ``/watches`` can settle that.

    Deleting a missing draft stays a no-op.
    """
    user_id = _require_user(update)
    async with deps.database.connection() as conn:
        draft = await deps.drafts.get(conn, user_id)
    if draft is not None and draft.state == WizardState.CONFIRMING.value:
        if _claim_is_live(draft, deps.clock.now()):
            return RenderedMessage(text=_IN_FLIGHT, parse_mode=ParseMode.HTML, reply_markup=None)
        async with deps.database.connection() as conn:
            await deps.drafts.delete(conn, user_id)
        return RenderedMessage(
            text=_CANCELLED_MID_CONFIRM, parse_mode=ParseMode.HTML, reply_markup=None
        )
    async with deps.database.connection() as conn:
        await deps.drafts.delete(conn, user_id)
    return RenderedMessage(
        text="Watch setup cancelled.", parse_mode=ParseMode.HTML, reply_markup=None
    )


async def handle_wizard_text(update: Update, deps: WizardDeps) -> RenderedMessage | None:
    """Advance a text-driven wizard step for this update, if there is one to advance.

    Returns ``None`` when there is no draft for this user, or the draft's current state
    awaits a button press rather than free text -- either way, this update is not the
    wizard's to handle, and a caller is free to route it elsewhere. Invalid input raises
    from the parser, is turned into a reply, and never reaches the persisted draft, so
    an invalid message leaves state and payload exactly as they were.
    """
    user_id = _require_user(update)
    text = update.message.text if update.message is not None else None
    if text is None:
        return None
    async with deps.database.connection() as conn:
        draft = await deps.drafts.get(conn, user_id)
    if draft is None:
        return None
    state = WizardState(draft.state)
    if state not in _TEXT_STATES:
        return None
    try:
        next_state, payload = _apply_text(state, dict(draft.payload), text)
    except InputError as exc:
        return _retry_prompt(str(exc))
    async with deps.database.connection() as conn:
        await deps.drafts.upsert(conn, user_id, next_state.value, payload, deps.clock.now())
    return _prompt_for(next_state, payload)


async def handle_wizard_callback(update: Update, deps: WizardDeps) -> RenderedMessage | None:
    """Advance a button-driven wizard step, or confirm/cancel from the review screen.

    Returns ``None`` under the same "not this handler's update" conditions as
    :func:`handle_wizard_text`: no callback data, no draft, or a button press arriving
    while the draft sits in a state this handler does not drive (e.g. a stale quantity
    button pressed once the draft has already moved on to a typed prompt such as the
    date range).

    Stale schedule-choice (``time-mode``), seat-mode, and seat-preference presses are
    answered with a recoverable retry prompt telling the user to use the current prompt,
    because silently ignoring them looks like a dead button.
    """
    user_id = _require_user(update)
    query = update.callback_query
    data = query.data if query is not None else None
    if data is None:
        return None
    async with deps.database.connection() as conn:
        draft = await deps.drafts.get(conn, user_id)
    if draft is None:
        return None
    state = WizardState(draft.state)
    if data == _CANCEL:
        return await cancel(update, deps)
    if data == _CONFIRM:
        return await _confirm(user_id, deps)
    if state not in _CALLBACK_STATES:
        if data.startswith((_TIME_MODE_PREFIX, _SEAT_MODE_PREFIX, _SEAT_PREFERENCE_PREFIX)):
            return _retry_prompt(
                "that choice is no longer active; use the current prompt"
            )
        return None
    try:
        next_state, payload = _apply_callback(state, dict(draft.payload), data)
    except InputError as exc:
        return _retry_prompt(str(exc))
    async with deps.database.connection() as conn:
        await deps.drafts.upsert(conn, user_id, next_state.value, payload, deps.clock.now())
    return _prompt_for(next_state, payload)


# ---------------------------------------------------------------------------
# Confirmation saga
# ---------------------------------------------------------------------------


#: Failures the saga compensates for: the process is fine, this attempt is not, so the
#: claim is released and the user gets a truthful, retryable reply. Anything outside this
#: set is treated as a crash -- it propagates, the claim stands, and recovery resumes it.
_RECOVERABLE_FAILURES = (
    sqlite3.Error,
    InputError,
    PersistenceError,
    ConflictError,
    BfiNetworkError,
    BfiChallengeError,
    BfiContractError,
    CircuitOpenError,
    DeliveryError,
)


@dataclass(frozen=True, slots=True)
class RecoveredConfirmation:
    """One draft whose confirmation was resumed, and what its owner should be told."""

    user_id: int
    message: RenderedMessage


def _watch_id_for(setup_id: str) -> UUID:
    """Derive the watch identity a setup will always produce.

    Deterministic on purpose: a retry after a crash that created the watch but never
    got to record that fact must land on the same row, and only the durable setup id
    can tell it which row that is.
    """
    return uuid5(_WATCH_NAMESPACE, setup_id)


async def prepare_confirmation_watch_id(
    user_id: int, deps: WizardDeps
) -> UUID | None:
    """Ensure a confirmable draft has the stable identity its saga will create."""
    async with deps.database.connection() as conn, deps.database.transaction(conn):
        draft = await deps.drafts.get(conn, user_id)
        if draft is None:
            return None
        state = WizardState(draft.state)
        if state not in (WizardState.REVIEW, WizardState.CONFIRMING):
            return None
        payload = dict(draft.payload)
        setup_id = payload.get("setup_id")
        if setup_id is None:
            if state is not WizardState.REVIEW:
                return None
            setup_id = str(uuid4())
            payload["setup_id"] = setup_id
            await deps.drafts.upsert(conn, user_id, state.value, payload, deps.clock.now())
    return _watch_id_for(setup_id)


def _claim_is_live(draft: ConversationDraft, now: datetime) -> bool:
    return now - draft.updated_at < CLAIM_LEASE


async def _write_phase(
    deps: WizardDeps, user_id: int, payload: dict[str, Any], phase: ConfirmPhase
) -> None:
    """Record how far the saga got, keeping the draft claimed and the lease renewed."""
    payload["confirm_phase"] = phase.value
    async with deps.database.connection() as conn, deps.database.transaction(conn):
        await deps.drafts.upsert(
            conn, user_id, WizardState.CONFIRMING.value, payload, deps.clock.now()
        )


async def _claim(
    user_id: int, deps: WizardDeps, *, ignore_lease: bool = False
) -> tuple[dict[str, Any], WatchCriteria] | RenderedMessage | None:
    """Take exclusive ownership of a confirmable draft, or explain why we cannot.

    Returns the claimed payload and its revalidated criteria on success, a reply to send
    when the user should be told something, or ``None`` when this update is not ours.

    The criteria are rebuilt *before* the claim is written: a payload that can no longer
    produce a valid :class:`WatchCriteria` must leave the draft in ``REVIEW`` where the
    user can still fix it, rather than being stranded in a claim that can never succeed.
    """
    now = deps.clock.now()
    async with deps.database.connection() as conn, deps.database.transaction(conn):
        draft = await deps.drafts.get(conn, user_id)
        if draft is None:
            return None
        state = WizardState(draft.state)
        if state is WizardState.CONFIRMING:
            if not ignore_lease and _claim_is_live(draft, now):
                return _retry_prompt(_IN_FLIGHT)
        elif state is not WizardState.REVIEW:
            return None
        payload = dict(draft.payload)
        try:
            criteria = _build_criteria(payload)
        except InputError as exc:
            return _retry_prompt(str(exc))
        payload.setdefault("setup_id", str(uuid4()))
        payload.setdefault("confirm_phase", ConfirmPhase.CLAIMED.value)
        payload["confirm_attempts"] = int(payload.get("confirm_attempts", 0)) + 1
        await deps.drafts.upsert(conn, user_id, WizardState.CONFIRMING.value, payload, now)
    return payload, criteria


async def _run_saga(
    user_id: int, deps: WizardDeps, payload: dict[str, Any], criteria: WatchCriteria
) -> RenderedMessage:
    """Drive a claimed confirmation to completion, resuming from its recorded phase.

    Every step is either idempotent or guarded by the phase written once it finished,
    so running this against any durable intermediate state converges on exactly one
    watch, one creation check, and no draft.
    """
    phase = ConfirmPhase(payload["confirm_phase"])
    watch_id = _watch_id_for(payload["setup_id"])
    try:
        if phase is ConfirmPhase.CLAIMED:
            # Idempotent by watch_id: safe whether or not a previous attempt got here.
            await deps.watches.create(user_id, criteria, watch_id=watch_id)
            await _write_phase(deps, user_id, payload, ConfirmPhase.WATCH_CREATED)
            phase = ConfirmPhase.WATCH_CREATED
        if phase is ConfirmPhase.WATCH_CREATED:
            # Marked before the call, not after: a check that may have already run must
            # never run twice, and a check that never ran is covered by the scheduler,
            # which the watch's immediate next_check_at already guarantees.
            await _write_phase(deps, user_id, payload, ConfirmPhase.CHECK_STARTED)
            await deps.checks.check(watch_id, CheckTrigger.CREATION)
            await _write_phase(deps, user_id, payload, ConfirmPhase.CHECK_DONE)
    except _RECOVERABLE_FAILURES:
        return await _compensate(user_id, deps, payload)
    async with deps.database.connection() as conn, deps.database.transaction(conn):
        await deps.drafts.delete(conn, user_id)
    return RenderedMessage(
        text=(
            f"Watch created for <code>{html.escape(criteria.slug)}</code>. "
            "Checking availability now -- I'll message you with results."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=None,
    )


async def _release_to_review(
    user_id: int, deps: WizardDeps, payload: dict[str, Any]
) -> None:
    """Put a claimed draft back where its owner can act on it, keeping phase markers."""
    async with deps.database.connection() as conn, deps.database.transaction(conn):
        await deps.drafts.upsert(
            conn, user_id, WizardState.REVIEW.value, payload, deps.clock.now()
        )


async def _compensate(
    user_id: int, deps: WizardDeps, payload: dict[str, Any]
) -> RenderedMessage:
    """Release the claim after a routine failure, leaving a retryable draft.

    The phase markers survive, so the retry skips whatever already succeeded, and the
    message is chosen from that same phase: telling a user nothing was saved when their
    watch is already active would send them off to create a duplicate.
    """
    phase = ConfirmPhase(payload["confirm_phase"])
    await _release_to_review(user_id, deps, payload)
    if phase is ConfirmPhase.CLAIMED:
        return _retry_prompt(_FAILED_BEFORE_CREATE)
    return _retry_prompt(_FAILED_AFTER_CREATE)


async def recover_confirmations(deps: WizardDeps) -> tuple[RecoveredConfirmation, ...]:
    """Resume every confirmation left claimed by a process that died.

    Call once at startup, before serving updates: it deliberately ignores
    :data:`CLAIM_LEASE`, because nothing can still be in flight in a process that has
    only just begun. Without it a crash mid-confirmation would leave the draft claimed
    until the user happened to tap Confirm again, which is exactly the stranded state
    the saga exists to remove. Returns what each affected user should be told.
    """
    async with deps.database.connection() as conn:
        stale = await deps.drafts.list_by_state(conn, WizardState.CONFIRMING.value)
    recovered: list[RecoveredConfirmation] = []
    for draft in stale:
        claimed = await _claim(draft.user_id, deps, ignore_lease=True)
        if claimed is None:
            continue
        if isinstance(claimed, RenderedMessage):
            # The lease is ignored here, so the only way to be refused is a payload that
            # can no longer build criteria. Skipping it would re-strand the draft on
            # every future restart; hand it back to the user in REVIEW instead.
            await _release_to_review(draft.user_id, deps, dict(draft.payload))
            recovered.append(RecoveredConfirmation(user_id=draft.user_id, message=claimed))
            continue
        payload, criteria = claimed
        message = await _run_saga(draft.user_id, deps, payload, criteria)
        recovered.append(RecoveredConfirmation(user_id=draft.user_id, message=message))
    return tuple(recovered)


async def _confirm(user_id: int, deps: WizardDeps) -> RenderedMessage | None:
    """Create the watch for a reviewed draft, exactly once across duplicates and retries.

    See the module docstring for the saga's shape. This entry point handles both a first
    confirmation and a retry of one whose claim has outlived its lease; ``None`` means
    the update was not this handler's to act on (no draft, or a draft on another step).
    """
    claimed = await _claim(user_id, deps)
    if claimed is None or isinstance(claimed, RenderedMessage):
        return claimed
    payload, criteria = claimed
    return await _run_saga(user_id, deps, payload, criteria)
