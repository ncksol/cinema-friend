"""Tests for cinema_friend.telegram.rendering.

Rendering is pure: every test constructs domain values directly and asserts on the
returned :class:`RenderedMessage`. Nothing here touches a repository or a live bot.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from cinema_friend.domain.bfi import Performance
from cinema_friend.domain.results import RankedOption, RankVector, SnapshotPage
from cinema_friend.domain.state import WatchMode, WatchStatus
from cinema_friend.domain.watch import Watch, WatchCriteria
from cinema_friend.telegram.callbacks import ResultPageAction, decode_callback
from cinema_friend.telegram.rendering import (
    MAX_MESSAGE_CHARS,
    MAX_OPTIONS_PER_PAGE,
    render_result_page,
    render_watch_list,
)

_SNAPSHOT_ID = UUID("11111111-1111-4111-8111-111111111111")
_WATCH_ID = UUID("22222222-2222-4222-8222-222222222222")


def _performance(
    performance_id: str = "perf-1",
    *,
    start_utc: datetime = datetime(2026, 8, 26, 17, 15, tzinfo=UTC),
    seat_map_url: str | None = "https://whatson.bfi.org.uk/imax/Online/mapSelect.asp?x=1",
) -> Performance:
    return Performance(
        performance_id=performance_id,
        event_id="event-1",
        start_utc=start_utc,
        sales_status_code="S",
        availability_code="E",
        availability_num=42,
        reserved_seating=True,
        seat_map_url=seat_map_url,
    )


def _option(
    *,
    performance: Performance | None = None,
    seat_label: str = "L17-L18",
    raw_view_score: float = 95.0,
    price_pence: int | None = 1500,
) -> RankedOption:
    perf = performance or _performance()
    return RankedOption(
        performance=perf,
        seat_label=seat_label,
        rank_vector=RankVector(
            preferred_seat_overlap=1,
            preferred_row_match=1,
            view_score_band=int(raw_view_score // 5),
            preferred_time_distance_minutes=0,
            raw_view_score=raw_view_score,
            performance_start=perf.start_utc,
            seat_label=seat_label,
        ),
        price_pence=price_pence,
    )


def _snapshot_page(
    *,
    options: tuple[RankedOption, ...] | None = None,
    page: int = 1,
    total_pages: int = 1,
    total_options: int | None = None,
    total_performances: int = 1,
    checked_at: datetime = datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
) -> SnapshotPage:
    opts = options if options is not None else (_option(),)
    return SnapshotPage(
        snapshot_id=_SNAPSHOT_ID,
        checked_at=checked_at,
        options=opts,
        page=page,
        total_pages=total_pages,
        total_options=total_options if total_options is not None else len(opts),
        total_performances=total_performances,
    )


def _watch(
    *,
    watch_id: UUID | None = None,
    title: str | None = "Dog Stars",
    status: WatchStatus = WatchStatus.ACTIVE,
    mode: WatchMode = WatchMode.RECURRING,
) -> Watch:
    criteria = WatchCriteria(
        source_url="https://whatson.bfi.org.uk/imax/Online/article/dog-stars",
        slug="dog-stars",
        date_from=date(2026, 8, 26),
        date_to=date(2026, 8, 30),
        time_from=time(18, 0),
        time_to=time(23, 0),
        quantity=2,
        mode=mode,
        interval=timedelta(minutes=30) if mode is WatchMode.RECURRING else None,
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Watch(
        watch_id=watch_id or uuid4(),
        user_id=11,
        criteria=criteria,
        status=status,
        created_at=now,
        updated_at=now,
        title=title,
    )


# ---------------------------------------------------------------------------
# render_result_page
# ---------------------------------------------------------------------------


def test_render_result_page_caps_options_at_ten_even_if_the_page_carries_more() -> None:
    options = tuple(_option(seat_label=f"L{i}") for i in range(15))
    page = _snapshot_page(options=options, total_options=15)

    rendered = render_result_page(page)

    assert rendered.reply_markup is not None
    seat_map_button_rows = [row for row in rendered.reply_markup.inline_keyboard if row[0].url]
    assert len(seat_map_button_rows) == MAX_OPTIONS_PER_PAGE


def test_render_result_page_includes_time_seats_rationale_and_checked_time() -> None:
    page = _snapshot_page(
        options=(_option(seat_label="L17-L18", raw_view_score=95.0),),
    )

    rendered = render_result_page(page)

    assert "L17-L18" in rendered.text
    assert "26 Aug 2026" in rendered.text
    assert "Excellent view" in rendered.text
    assert "26 Aug 2026" in rendered.text  # checked-at date also appears


def test_render_result_page_describes_view_quality_by_band() -> None:
    excellent = render_result_page(_snapshot_page(options=(_option(raw_view_score=90.0),)))
    good = render_result_page(_snapshot_page(options=(_option(raw_view_score=65.0),)))
    fair = render_result_page(_snapshot_page(options=(_option(raw_view_score=45.0),)))
    limited = render_result_page(_snapshot_page(options=(_option(raw_view_score=10.0),)))

    assert "Excellent view" in excellent.text
    assert "Good view" in good.text
    assert "Fair view" in fair.text
    assert "Limited view" in limited.text


def test_render_result_page_includes_total_option_and_performance_counts() -> None:
    page = _snapshot_page(
        options=(_option(),),
        total_options=37,
        total_performances=4,
        page=2,
        total_pages=4,
    )

    rendered = render_result_page(page)

    assert "37" in rendered.text
    assert "4" in rendered.text
    assert "2" in rendered.text  # current page


def test_render_result_page_buttons_link_directly_to_the_bfi_seat_map() -> None:
    performance = _performance(
        performance_id="perf-42",
        seat_map_url="https://whatson.bfi.org.uk/imax/Online/mapSelect.asp?BOparam=perf-42",
    )
    page = _snapshot_page(options=(_option(performance=performance),))

    rendered = render_result_page(page)

    assert rendered.reply_markup is not None
    urls = [button.url for row in rendered.reply_markup.inline_keyboard for button in row]
    assert "https://whatson.bfi.org.uk/imax/Online/mapSelect.asp?BOparam=perf-42" in urls


def test_render_result_page_omits_a_seat_map_button_when_the_url_is_absent() -> None:
    performance = _performance(seat_map_url=None)
    page = _snapshot_page(options=(_option(performance=performance),))

    rendered = render_result_page(page)

    # No seat-map button and no pagination (single page) leaves no keyboard at all.
    assert rendered.reply_markup is None


def test_render_result_page_includes_a_next_button_when_more_pages_remain() -> None:
    page = _snapshot_page(page=1, total_pages=3)

    rendered = render_result_page(page)

    assert rendered.reply_markup is not None
    callbacks = [
        button.callback_data
        for row in rendered.reply_markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert len(callbacks) == 1
    assert decode_callback(callbacks[0]) == ResultPageAction(snapshot_id=_SNAPSHOT_ID, page=2)


def test_render_result_page_includes_a_previous_button_when_not_on_the_first_page() -> None:
    page = _snapshot_page(page=2, total_pages=3)

    rendered = render_result_page(page)

    assert rendered.reply_markup is not None
    callbacks = [
        decode_callback(button.callback_data)
        for row in rendered.reply_markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert ResultPageAction(snapshot_id=_SNAPSHOT_ID, page=1) in callbacks
    assert ResultPageAction(snapshot_id=_SNAPSHOT_ID, page=3) in callbacks


def test_render_result_page_omits_previous_on_the_first_page() -> None:
    page = _snapshot_page(page=1, total_pages=3)

    rendered = render_result_page(page)

    assert rendered.reply_markup is not None
    callbacks = [
        decode_callback(button.callback_data)
        for row in rendered.reply_markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert ResultPageAction(snapshot_id=_SNAPSHOT_ID, page=1) not in callbacks


def test_render_result_page_omits_next_on_the_last_page() -> None:
    page = _snapshot_page(page=3, total_pages=3)

    rendered = render_result_page(page)

    assert rendered.reply_markup is not None
    callbacks = [
        decode_callback(button.callback_data)
        for row in rendered.reply_markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert ResultPageAction(snapshot_id=_SNAPSHOT_ID, page=4) not in callbacks


def test_render_result_page_omits_pagination_buttons_on_a_single_page() -> None:
    page = _snapshot_page(page=1, total_pages=1)

    rendered = render_result_page(page)

    assert rendered.reply_markup is not None
    callbacks = [
        button.callback_data
        for row in rendered.reply_markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert callbacks == []


def test_render_result_page_escapes_html_special_characters() -> None:
    performance = _performance()
    option = _option(performance=performance, seat_label="<script>alert(1)</script>")
    page = _snapshot_page(options=(option,))

    rendered = render_result_page(page)

    assert "<script>" not in rendered.text
    assert "&lt;script&gt;" in rendered.text


def test_render_result_page_uses_html_parse_mode() -> None:
    rendered = render_result_page(_snapshot_page())

    assert rendered.parse_mode == ParseMode.HTML


def test_render_result_page_stays_under_the_telegram_character_limit() -> None:
    long_seat_label = "-".join(str(uuid4()) for _ in range(8))
    options = tuple(
        _option(seat_label=long_seat_label, performance=_performance(f"perf-{i}"))
        for i in range(MAX_OPTIONS_PER_PAGE)
    )
    page = _snapshot_page(options=options, total_options=len(options))

    rendered = render_result_page(page)

    assert len(rendered.text) <= MAX_MESSAGE_CHARS


def test_render_result_page_handles_an_empty_snapshot() -> None:
    page = _snapshot_page(options=(), total_options=0, total_performances=0)

    rendered = render_result_page(page)

    assert "0" in rendered.text


# ---------------------------------------------------------------------------
# render_watch_list
# ---------------------------------------------------------------------------


def test_render_watch_list_includes_each_watchs_title_and_status() -> None:
    watches = (
        _watch(title="Dog Stars", status=WatchStatus.ACTIVE),
        _watch(title="Cat Moons", status=WatchStatus.PAUSED),
    )

    rendered = render_watch_list(watches)

    assert "Dog Stars" in rendered.text
    assert "Cat Moons" in rendered.text


def test_render_watch_list_shows_a_pause_button_for_an_active_recurring_watch() -> None:
    watch = _watch(watch_id=_WATCH_ID, status=WatchStatus.ACTIVE, mode=WatchMode.RECURRING)

    rendered = render_watch_list((watch,))

    assert rendered.reply_markup is not None
    actions = [
        decode_callback(button.callback_data).action  # type: ignore[union-attr]
        for row in rendered.reply_markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert "pause" in actions
    assert "resume" not in actions


def test_render_watch_list_shows_a_resume_button_for_a_paused_recurring_watch() -> None:
    watch = _watch(watch_id=_WATCH_ID, status=WatchStatus.PAUSED, mode=WatchMode.RECURRING)

    rendered = render_watch_list((watch,))

    assert rendered.reply_markup is not None
    actions = [
        decode_callback(button.callback_data).action  # type: ignore[union-attr]
        for row in rendered.reply_markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert "resume" in actions
    assert "pause" not in actions


def test_render_watch_list_always_shows_a_delete_button() -> None:
    watch = _watch(watch_id=_WATCH_ID, status=WatchStatus.COMPLETED, mode=WatchMode.ONE_OFF)

    rendered = render_watch_list((watch,))

    assert rendered.reply_markup is not None
    actions = [
        decode_callback(button.callback_data).action  # type: ignore[union-attr]
        for row in rendered.reply_markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert "delete" in actions


def test_render_watch_list_omits_pause_and_resume_for_a_one_off_watch() -> None:
    watch = _watch(watch_id=_WATCH_ID, status=WatchStatus.ACTIVE, mode=WatchMode.ONE_OFF)

    rendered = render_watch_list((watch,))

    assert rendered.reply_markup is not None
    actions = [
        decode_callback(button.callback_data).action  # type: ignore[union-attr]
        for row in rendered.reply_markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert "pause" not in actions
    assert "resume" not in actions


def test_render_watch_list_handles_an_empty_list() -> None:
    rendered = render_watch_list(())

    assert rendered.text
    assert rendered.reply_markup is None


def test_render_watch_list_escapes_html_special_characters_in_titles() -> None:
    watch = _watch(title="<b>evil</b>")

    rendered = render_watch_list((watch,))

    assert "<b>evil</b>" not in rendered.text
    assert "&lt;b&gt;evil&lt;/b&gt;" in rendered.text


def test_render_watch_list_uses_html_parse_mode() -> None:
    rendered = render_watch_list((_watch(),))

    assert rendered.parse_mode == ParseMode.HTML


def test_rendered_message_reply_markup_is_an_inline_keyboard_markup() -> None:
    rendered = render_result_page(_snapshot_page())

    assert isinstance(rendered.reply_markup, InlineKeyboardMarkup)
    for row in rendered.reply_markup.inline_keyboard:
        for button in row:
            assert isinstance(button, InlineKeyboardButton)
