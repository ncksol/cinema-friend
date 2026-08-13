"""Pure Telegram rendering: result-snapshot pages and watch lists as displayable messages.

Nothing here calls Telegram or a repository. Domain values (:class:`SnapshotPage`,
:class:`Watch`) go in, a :class:`RenderedMessage` comes out; Task 14 is the only place
that hands one of these to a live bot.

Two invariants shape the whole module. First, every message is assembled from whole,
already-escaped lines and is only ever shortened by dropping a complete line, so no
message can end mid-tag or mid-entity and be rejected by Telegram's HTML parser.
Second, every time is rendered in ``Europe/London`` -- the cinema's clock -- so the
message reads the same whatever timezone the host process happens to run in.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from cinema_friend.domain.results import RankedOption, SnapshotPage
from cinema_friend.domain.state import WatchMode, WatchStatus
from cinema_friend.domain.time_window import LONDON
from cinema_friend.domain.watch import Watch
from cinema_friend.telegram.callbacks import encode_callback

MAX_MESSAGE_CHARS = 4096
MAX_OPTIONS_PER_PAGE = 10

_TIME_FORMAT = "%a %d %b %Y, %H:%M"
_DATE_FORMAT = "%d %b %Y"
_CLOCK_FORMAT = "%H:%M"

_MAX_TITLE_CHARS = 80
_MAX_SEAT_LABEL_CHARS = 60
_MAX_CATEGORY_CHARS = 40
_TRUNCATION_MARK = "…"

_UNTITLED = "(untitled)"
_NO_MATCH = "No matching seats were found for this check."
_INITIAL_RECURRING_EMPTY = (
    "I haven't found anything right now, but I'll keep watching."
)
_TOO_LONG = "Your watches are too long to display here."

_STATUS_LABEL: dict[WatchStatus, str] = {
    WatchStatus.ACTIVE: "active",
    WatchStatus.PAUSED: "paused",
    WatchStatus.BACKOFF: "waiting on host",
    WatchStatus.COMPLETED: "completed",
    WatchStatus.EXPIRED: "expired",
    WatchStatus.FAILED: "failed",
}


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    """A message ready to send: HTML text, its parse mode, and its keyboard, if any."""

    text: str
    parse_mode: str
    reply_markup: InlineKeyboardMarkup | None


def _clip(text: str, limit: int) -> str:
    """Shorten *text* before it is escaped, never after.

    Clipping escaped markup could cut ``&amp;`` in half and leave Telegram parsing a
    dangling entity, so every bound in this module is applied to the raw value and the
    escape then runs over an already-short string.
    """
    if len(text) <= limit:
        return text
    return text[: limit - len(_TRUNCATION_MARK)] + _TRUNCATION_MARK


def _escape(text: str, limit: int) -> str:
    return html.escape(_clip(text, limit), quote=False)


def _london(moment: datetime) -> str:
    return moment.astimezone(LONDON).strftime(_TIME_FORMAT)


def _fit_lines(fixed: Sequence[str], items: Sequence[str], max_chars: int) -> int:
    """Return how many of *items* fit alongside *fixed* within *max_chars*.

    Items are whole rendered lines and are taken in order, so the answer is also the
    number of leading items the caller may build buttons for -- keyboard and text can
    never disagree about what was shown.
    """
    budget = sum(len(line) + 1 for line in fixed)
    fitted = 0
    for item in items:
        budget += len(item) + 1
        if budget > max_chars:
            break
        fitted += 1
    return fitted


def _view_rationale(raw_view_score: float) -> str:
    if raw_view_score >= 80:
        return "Excellent view"
    if raw_view_score >= 60:
        return "Good view"
    if raw_view_score >= 40:
        return "Fair view"
    return "Limited view"


def _option_line(index: int, option: RankedOption) -> str:
    when = _escape(option.performance.start.strftime(_TIME_FORMAT), _MAX_TITLE_CHARS)
    seats = _escape(option.seat_label, _MAX_SEAT_LABEL_CHARS)
    score = option.rank_vector.raw_view_score
    line = f"{index}. <b>{when}</b> — {seats} — {_view_rationale(score)} ({score:.1f}/100)"
    if option.seat_categories:
        # Ranking already de-duplicates, but the domain type does not enforce it and a
        # block spanning two zones must not read "Premium, Premium".
        categories = ", ".join(dict.fromkeys(option.seat_categories))
        line += f" — {_escape(categories, _MAX_CATEGORY_CHARS)}"
    if option.price_pence is not None:
        line += f" — £{option.price_pence / 100:.2f}"
    return line


def _seat_map_button(index: int, option: RankedOption) -> list[InlineKeyboardButton]:
    """One button linking directly to the option's BFI seat map, if it has one.

    ``Performance.seat_map_url`` is already the canonical URL built by the gateway from
    the performance ID; rendering reuses it rather than reconstructing one. A
    ``RankedOption`` reaching this page implies a seat map was fetched for it, so the
    field is populated in practice, but the domain type still allows ``None`` and a
    Telegram button cannot be created without a URL, so a missing one is skipped rather
    than surfaced as a broken button.
    """
    url = option.performance.seat_map_url
    if url is None:
        return []
    return [InlineKeyboardButton(text=f"Seat map {index}", url=url)]


def _page_title(snapshot_page: SnapshotPage) -> str:
    """The film name to head the page with.

    An option carries the title the listing used at check time, which is what the
    snapshot actually recorded; the watch's own title is the fallback, and the only
    title available at all when the snapshot found nothing.
    """
    for option in snapshot_page.options:
        if option.title:
            return option.title
    return snapshot_page.watch_title or "Ticket options"


def _pagination_row(snapshot_page: SnapshotPage) -> list[InlineKeyboardButton]:
    row: list[InlineKeyboardButton] = []
    if snapshot_page.page > 1:
        row.append(
            InlineKeyboardButton(
                text="⬅ Previous",
                callback_data=encode_callback(
                    "page", snapshot_page.snapshot_id, snapshot_page.page - 1
                ),
            )
        )
    if snapshot_page.page < snapshot_page.total_pages:
        row.append(
            InlineKeyboardButton(
                text="Next ➡",
                callback_data=encode_callback(
                    "page", snapshot_page.snapshot_id, snapshot_page.page + 1
                ),
            )
        )
    return row


def render_result_page(
    snapshot_page: SnapshotPage,
    *,
    max_chars: int = MAX_MESSAGE_CHARS,
    empty_message: str = _NO_MATCH,
) -> RenderedMessage:
    """Render one page of a result snapshot as a numbered, linked option list.

    Options beyond :data:`MAX_OPTIONS_PER_PAGE` are dropped defensively -- the
    repository already caps a page at ten, but rendering does not trust that and
    re-enforces its own limit. Whatever survives that cap is then fitted to
    *max_chars* one whole line at a time, and only the options that were actually
    written get a seat-map button, so the keyboard always describes the text beside it.

    A snapshot with no options is a real answer, not an empty page, so it is rendered
    as a plain "nothing matched" message under the watch's own title rather than as a
    zero-length list with pagination. Callers may supply context-specific copy for the
    empty snapshot without changing the rest of the page layout.
    """
    title = _escape(_page_title(snapshot_page), _MAX_TITLE_CHARS)
    checked_at = _escape(_london(snapshot_page.checked_at), _MAX_TITLE_CHARS)
    header = [f"<b>{title}</b>", f"Checked: {checked_at} (London)"]

    if not snapshot_page.options:
        return RenderedMessage(
            text="\n".join([*header, "", empty_message]),
            parse_mode=ParseMode.HTML,
            reply_markup=None,
        )

    options = snapshot_page.options[:MAX_OPTIONS_PER_PAGE]
    option_lines = [_option_line(i, option) for i, option in enumerate(options, start=1)]

    def footer(shown: int) -> str:
        return (
            f"Showing {shown} of {snapshot_page.total_options} option(s) across "
            f"{snapshot_page.total_performances} performance(s) — "
            f"page {snapshot_page.page}/{snapshot_page.total_pages}."
        )

    # The footer states how many options were shown, so it can only be sized once the
    # count is known; the widest possible count reserves enough room for the real one.
    fixed = [*header, "", "", footer(len(option_lines))]
    shown = _fit_lines(fixed, option_lines, max_chars)

    text = "\n".join([*header, "", *option_lines[:shown], "", footer(shown)])
    rows: list[list[InlineKeyboardButton]] = [
        button
        for i, option in enumerate(options[:shown], start=1)
        if (button := _seat_map_button(i, option))
    ]
    if nav_row := _pagination_row(snapshot_page):
        rows.append(nav_row)

    reply_markup = InlineKeyboardMarkup(rows) if rows else None
    return RenderedMessage(text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)


def render_initial_recurring_empty_page(
    snapshot_page: SnapshotPage, *, max_chars: int = MAX_MESSAGE_CHARS
) -> RenderedMessage:
    return render_result_page(
        snapshot_page,
        max_chars=max_chars,
        empty_message=_INITIAL_RECURRING_EMPTY,
    )


def _interval_summary(interval: timedelta) -> str:
    minutes = max(1, int(interval.total_seconds() // 60))
    hours, remainder = divmod(minutes, 60)
    if hours and remainder:
        return f"{hours}h {remainder}m"
    if hours:
        return f"{hours}h"
    return f"{remainder}m"


def _criteria_summary(watch: Watch) -> str:
    """The watch's search window in one line: dates, daily times, quantity, cadence."""
    criteria = watch.criteria
    date_from = criteria.date_from.strftime(_DATE_FORMAT)
    date_to = criteria.date_to.strftime(_DATE_FORMAT)
    dates = date_from if date_from == date_to else f"{date_from} to {date_to}"
    time_from = criteria.time_from.strftime(_CLOCK_FORMAT)
    time_to = criteria.time_to.strftime(_CLOCK_FORMAT)
    cadence = (
        f"every {_interval_summary(criteria.interval)}"
        if criteria.mode is WatchMode.RECURRING and criteria.interval is not None
        else "one-off"
    )
    return f"{dates}, {time_from}-{time_to}, {criteria.quantity} seats, {cadence}"


def _watch_block(index: int, watch: Watch) -> str:
    """One watch as a title/status line plus its criteria and scheduling state.

    Returned as a single string with embedded newlines so the budget check treats a
    watch as indivisible: a watch is either shown whole or not at all.
    """
    title = _escape(watch.title, _MAX_TITLE_CHARS) if watch.title else _UNTITLED
    lines = [
        f"{index}. <b>{title}</b> — {_STATUS_LABEL[watch.status]}",
        f"    {_escape(_criteria_summary(watch), _MAX_TITLE_CHARS * 2)}",
    ]
    next_check = _london(watch.next_check_at) if watch.next_check_at else "not scheduled"
    lines.append(f"    Next check: {_escape(next_check, _MAX_TITLE_CHARS)} (London)")
    if watch.last_check_at is not None:
        lines.append(
            f"    Last checked: {_escape(_london(watch.last_check_at), _MAX_TITLE_CHARS)} (London)"
        )
    return "\n".join(lines)


def _watch_buttons(watch: Watch) -> list[InlineKeyboardButton]:
    buttons: list[InlineKeyboardButton] = []
    if watch.criteria.mode is WatchMode.RECURRING:
        if watch.status is WatchStatus.ACTIVE:
            buttons.append(
                InlineKeyboardButton(
                    text="Pause", callback_data=encode_callback("pause", watch.watch_id)
                )
            )
        elif watch.status is WatchStatus.PAUSED:
            buttons.append(
                InlineKeyboardButton(
                    text="Resume", callback_data=encode_callback("resume", watch.watch_id)
                )
            )
    buttons.append(
        InlineKeyboardButton(
            text="Delete", callback_data=encode_callback("delete", watch.watch_id)
        )
    )
    return buttons


def render_watch_list(
    watches: Sequence[Watch], *, max_chars: int = MAX_MESSAGE_CHARS
) -> RenderedMessage:
    """Render an owner's watches with their criteria, schedule state, and actions.

    Each watch is an indivisible block, so a list too long for one message loses whole
    watches from the end rather than half of one. Buttons are built only for the
    watches that fit, keeping one keyboard row per watch actually shown.

    Pause/resume buttons only appear for a ``RECURRING`` watch whose status makes the
    action legal (``ACTIVE``/``PAUSED``), matching :class:`WatchService`'s own
    transition guards. A delete button is always offered.
    """
    if not watches:
        return RenderedMessage(
            text="You have no watches yet.", parse_mode=ParseMode.HTML, reply_markup=None
        )

    blocks = [_watch_block(i, watch) for i, watch in enumerate(watches, start=1)]
    shown = _fit_lines((), blocks, max_chars)
    if shown == 0:
        return RenderedMessage(text=_TOO_LONG, parse_mode=ParseMode.HTML, reply_markup=None)

    rows = [_watch_buttons(watch) for watch in watches[:shown]]
    return RenderedMessage(
        text="\n".join(blocks[:shown]),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )
