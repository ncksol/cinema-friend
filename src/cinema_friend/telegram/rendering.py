"""Pure Telegram rendering: result-snapshot pages and watch lists as displayable messages.

Nothing here calls Telegram or a repository. Domain values (:class:`SnapshotPage`,
:class:`Watch`) go in, a :class:`RenderedMessage` comes out; Task 14 is the only place
that hands one of these to a live bot.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from cinema_friend.domain.results import RankedOption, SnapshotPage
from cinema_friend.domain.state import WatchMode, WatchStatus
from cinema_friend.domain.watch import Watch
from cinema_friend.telegram.callbacks import encode_callback

MAX_MESSAGE_CHARS = 4096
MAX_OPTIONS_PER_PAGE = 10

_MAX_SEAT_LABEL_DISPLAY = 40
_TRUNCATION_MARK = "…"

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


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(_TRUNCATION_MARK)] + _TRUNCATION_MARK


def _view_rationale(raw_view_score: float) -> str:
    if raw_view_score >= 80:
        return "Excellent view"
    if raw_view_score >= 60:
        return "Good view"
    if raw_view_score >= 40:
        return "Fair view"
    return "Limited view"


def _option_line(index: int, option: RankedOption) -> str:
    when = _escape(option.performance.start.strftime("%a %d %b %Y, %H:%M"))
    rationale = _view_rationale(option.rank_vector.raw_view_score)
    seats = _escape(_truncate(option.seat_label, _MAX_SEAT_LABEL_DISPLAY))
    line = f"{index}. <b>{when}</b> — {seats} — {rationale}"
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


def render_result_page(snapshot_page: SnapshotPage) -> RenderedMessage:
    """Render one page of a result snapshot as a numbered, linked option list.

    Options beyond :data:`MAX_OPTIONS_PER_PAGE` are dropped defensively -- the
    repository already caps a page at ten, but rendering does not trust that and
    re-enforces its own limit so a future caller passing a larger page can never grow
    the message past the 4096-character ceiling through this path alone. The final
    text is also hard-truncated to that ceiling as a last-resort guarantee.
    """
    options = snapshot_page.options[:MAX_OPTIONS_PER_PAGE]
    checked_at = _escape(snapshot_page.checked_at.astimezone().strftime("%a %d %b %Y, %H:%M"))

    lines = [
        "<b>Ticket options</b>",
        f"Checked: {checked_at}",
        "",
        *[_option_line(i, option) for i, option in enumerate(options, start=1)],
        "",
        (
            f"Showing {len(options)} of {snapshot_page.total_options} option(s) across "
            f"{snapshot_page.total_performances} performance(s) — "
            f"page {snapshot_page.page}/{snapshot_page.total_pages}."
        ),
    ]
    text = _truncate("\n".join(lines), MAX_MESSAGE_CHARS)

    rows: list[list[InlineKeyboardButton]] = [
        button for i, option in enumerate(options, start=1) if (button := _seat_map_button(i, option))
    ]

    nav_row: list[InlineKeyboardButton] = []
    if snapshot_page.page > 1:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅ Previous",
                callback_data=encode_callback(
                    "page", snapshot_page.snapshot_id, snapshot_page.page - 1
                ),
            )
        )
    if snapshot_page.page < snapshot_page.total_pages:
        nav_row.append(
            InlineKeyboardButton(
                text="Next ➡",
                callback_data=encode_callback(
                    "page", snapshot_page.snapshot_id, snapshot_page.page + 1
                ),
            )
        )
    if nav_row:
        rows.append(nav_row)

    reply_markup = InlineKeyboardMarkup(rows) if rows else None
    return RenderedMessage(text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)


def _watch_line(index: int, watch: Watch) -> str:
    title = _escape(watch.title) if watch.title else "(untitled)"
    status = _STATUS_LABEL[watch.status]
    return f"{index}. <b>{title}</b> — {status}"


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


def render_watch_list(watches: Sequence[Watch]) -> RenderedMessage:
    """Render an owner's watches as a numbered list with per-watch action buttons.

    Pause/resume buttons only appear for a ``RECURRING`` watch whose status makes the
    action legal (``ACTIVE``/``PAUSED``), matching :class:`WatchService`'s own
    transition guards. A delete button is always offered.
    """
    if not watches:
        return RenderedMessage(
            text="You have no watches yet.", parse_mode=ParseMode.HTML, reply_markup=None
        )

    text = _truncate(
        "\n".join(_watch_line(i, watch) for i, watch in enumerate(watches, start=1)),
        MAX_MESSAGE_CHARS,
    )
    rows = [_watch_buttons(watch) for watch in watches]
    return RenderedMessage(
        text=text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows)
    )
