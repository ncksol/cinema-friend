"""Tests for cinema_friend.telegram.rendering.

Rendering is pure: every test constructs domain values directly and asserts on the
returned :class:`RenderedMessage`. Nothing here touches a repository or a live bot.
"""

from __future__ import annotations

import re
import time as time_module
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
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
    RenderedMessage,
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
        start_utc=start_utc,
        sales_status_code="S",
        availability_status_code="E",
        availability_num=42,
        seat_map_url=seat_map_url,
        options=("1", "2"),
    )


def _option(
    *,
    performance: Performance | None = None,
    seat_label: str = "L17-L18",
    seat_ids: tuple[str, ...] | None = None,
    raw_view_score: float = 95.0,
    price_pence: int | None = 1500,
    seat_categories: tuple[str, ...] = ("Premium",),
    title: str | None = "Dog Stars",
) -> RankedOption:
    perf = performance or _performance()
    ids = seat_ids if seat_ids is not None else tuple(f"guid-{p}" for p in seat_label.split("-"))
    return RankedOption(
        performance=perf,
        seat_label=seat_label,
        seat_ids=ids,
        rank_vector=RankVector(
            preferred_seat_overlap=1,
            preferred_row_match=1,
            view_score_band=int(raw_view_score // 5),
            preferred_time_distance_minutes=0,
            raw_view_score=raw_view_score,
            performance_start=perf.start_utc,
            seat_key="|".join(ids),
        ),
        price_pence=price_pence,
        seat_categories=seat_categories,
        title=title,
    )


def _snapshot_page(
    *,
    options: tuple[RankedOption, ...] | None = None,
    page: int = 1,
    total_pages: int = 1,
    total_options: int | None = None,
    total_performances: int = 1,
    checked_at: datetime = datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
    watch_title: str | None = "Dog Stars",
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
        watch_title=watch_title,
    )


def _watch(
    *,
    watch_id: UUID | None = None,
    title: str | None = "Dog Stars",
    status: WatchStatus = WatchStatus.ACTIVE,
    mode: WatchMode = WatchMode.RECURRING,
    quantity: int = 2,
    interval: timedelta | None = None,
    next_check_at: datetime | None = datetime(2026, 8, 26, 8, 30, tzinfo=UTC),
    last_check_at: datetime | None = None,
) -> Watch:
    criteria = WatchCriteria(
        source_url="https://whatson.bfi.org.uk/imax/Online/article/dog-stars",
        slug="dog-stars",
        date_from=date(2026, 8, 26),
        date_to=date(2026, 8, 30),
        time_from=time(18, 0),
        time_to=time(23, 0),
        quantity=quantity,
        mode=mode,
        interval=(
            (interval or timedelta(minutes=30)) if mode is WatchMode.RECURRING else None
        ),
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Watch(
        watch_id=watch_id or uuid4(),
        user_id=11,
        criteria=criteria,
        status=status,
        created_at=now,
        updated_at=now,
        next_check_at=next_check_at,
        title=title,
        last_check_at=last_check_at,
    )


_ALLOWED_TAGS = frozenset({"b", "i", "u", "s", "code", "pre", "a"})
_TAG_RE = re.compile(r"<(/?)([a-zA-Z]+)(?:\s[^<>]*)?>")
_DANGLING_ENTITY_RE = re.compile(r"&(?!(?:[a-zA-Z]+|#\d+);)")


def _assert_valid_telegram_html(text: str) -> None:
    """Fail unless *text* is well-formed, fully-escaped Telegram HTML within the limit."""
    assert len(text) <= MAX_MESSAGE_CHARS
    assert _DANGLING_ENTITY_RE.search(text) is None, "truncated or unescaped HTML entity"
    assert text.count("<") == text.count(">"), "truncated tag"
    stack: list[str] = []
    for closing, name in _TAG_RE.findall(text):
        assert name in _ALLOWED_TAGS, f"unsupported Telegram tag {name!r}"
        if closing:
            assert stack and stack.pop() == name, f"unbalanced </{name}>"
        else:
            stack.append(name)
    assert not stack, f"unclosed tags {stack}"
    # Every "<" that survives escaping must belong to one of the tags above.
    assert text.count("<") == len(_TAG_RE.findall(text)), "stray angle bracket"


def _numbered_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if re.match(r"^\d+\. ", line)]


def _url_buttons(rendered: RenderedMessage) -> list[str]:
    if rendered.reply_markup is None:
        return []
    return [
        button.url
        for row in rendered.reply_markup.inline_keyboard
        for button in row
        if button.url
    ]


@pytest.fixture
def host_timezone(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run the test with a deliberately non-London host timezone."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time_module.tzset()
    yield
    monkeypatch.undo()
    time_module.tzset()


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


# ---------------------------------------------------------------------------
# Film title, raw score, and seat categories
# ---------------------------------------------------------------------------


def test_render_result_page_shows_the_film_title_from_the_options() -> None:
    page = _snapshot_page(options=(_option(title="Dog Stars"),), watch_title="Stale title")

    rendered = render_result_page(page)

    assert "Dog Stars" in rendered.text


def test_render_result_page_falls_back_to_the_watch_title_for_an_empty_snapshot() -> None:
    page = _snapshot_page(
        options=(), total_options=0, total_performances=0, watch_title="Dog Stars"
    )

    rendered = render_result_page(page)

    assert "Dog Stars" in rendered.text


def test_render_result_page_falls_back_to_the_watch_title_for_untitled_options() -> None:
    page = _snapshot_page(options=(_option(title=None),), watch_title="Dog Stars")

    rendered = render_result_page(page)

    assert "Dog Stars" in rendered.text


def test_render_result_page_shows_the_raw_view_score_beside_its_rationale() -> None:
    page = _snapshot_page(options=(_option(raw_view_score=87.5),))

    rendered = render_result_page(page)

    assert "87.5" in rendered.text
    assert "Excellent view" in rendered.text


def test_render_result_page_lists_distinct_seat_categories() -> None:
    page = _snapshot_page(
        options=(_option(seat_categories=("Premium", "Standard", "Premium")),)
    )

    rendered = render_result_page(page)

    assert "Premium" in rendered.text
    assert "Standard" in rendered.text
    assert rendered.text.count("Premium") == 1


def test_render_result_page_omits_the_category_field_when_no_category_is_known() -> None:
    with_categories = render_result_page(
        _snapshot_page(options=(_option(seat_categories=("Premium",)),))
    )
    without = render_result_page(_snapshot_page(options=(_option(seat_categories=()),)))

    assert "Premium" in with_categories.text
    assert "Premium" not in without.text
    assert len(_numbered_lines(without.text)) == 1


def test_render_result_page_shows_human_readable_seat_labels() -> None:
    page = _snapshot_page(
        options=(
            _option(
                seat_label="L17-L18",
                seat_ids=(
                    "1FA0A9C8-1111-4000-8000-000000000017",
                    "1FA0A9C8-2222-4000-8000-000000000018",
                ),
            ),
        )
    )

    rendered = render_result_page(page)

    assert "L17-L18" in rendered.text
    assert "1FA0A9C8" not in rendered.text


# ---------------------------------------------------------------------------
# Empty snapshot
# ---------------------------------------------------------------------------


def test_render_result_page_states_no_match_for_an_empty_snapshot() -> None:
    page = _snapshot_page(options=(), total_options=0, total_performances=0)

    rendered = render_result_page(page)

    assert "No matching seats" in rendered.text
    assert "Showing 0 of 0" not in rendered.text
    assert rendered.reply_markup is None


# ---------------------------------------------------------------------------
# Timezone
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("host_timezone")
def test_render_result_page_renders_times_in_london_whatever_the_host_timezone() -> None:
    page = _snapshot_page(
        options=(
            _option(performance=_performance(start_utc=datetime(2026, 8, 26, 17, 15, tzinfo=UTC))),
        ),
        checked_at=datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
    )

    rendered = render_result_page(page)

    assert "18:15" in rendered.text  # 17:15 UTC is 18:15 British Summer Time
    assert "13:00" in rendered.text  # checked-at, likewise
    assert "10:15" not in rendered.text  # never the host's Los Angeles clock
    assert "London" in rendered.text


@pytest.mark.usefixtures("host_timezone")
def test_render_result_page_renders_winter_times_in_london_standard_time() -> None:
    page = _snapshot_page(
        options=(
            _option(performance=_performance(start_utc=datetime(2026, 1, 20, 19, 0, tzinfo=UTC))),
        ),
        checked_at=datetime(2026, 1, 20, 9, 0, tzinfo=UTC),
    )

    rendered = render_result_page(page)

    assert "19:00" in rendered.text  # GMT in January, so identical to UTC
    assert "11:00" not in rendered.text


@pytest.mark.usefixtures("host_timezone")
def test_render_watch_list_renders_the_next_check_in_london() -> None:
    watch = _watch(next_check_at=datetime(2026, 8, 26, 8, 30, tzinfo=UTC))

    rendered = render_watch_list((watch,))

    assert "09:30" in rendered.text
    assert "01:30" not in rendered.text


# ---------------------------------------------------------------------------
# Bounded assembly: whole lines only, keyboard matching what was rendered
# ---------------------------------------------------------------------------


def test_render_result_page_produces_valid_telegram_html() -> None:
    page = _snapshot_page(
        options=(
            _option(
                seat_label="L17 & L18 <best>",
                seat_categories=("Premium & Co",),
                title="A & B",
            ),
        )
    )

    _assert_valid_telegram_html(render_result_page(page).text)


def test_render_result_page_stays_valid_html_for_pathologically_long_content() -> None:
    page = _snapshot_page(
        options=tuple(
            _option(
                performance=_performance(f"perf-{index}"),
                seat_label="&" * 2000,
                seat_categories=("<" * 2000,),
                title="&" * 2000,
            )
            for index in range(MAX_OPTIONS_PER_PAGE)
        ),
        total_options=MAX_OPTIONS_PER_PAGE,
    )

    _assert_valid_telegram_html(render_result_page(page).text)


def test_render_result_page_drops_whole_options_rather_than_slicing_markup() -> None:
    options = tuple(
        _option(performance=_performance(f"perf-{index}"), seat_label=f"L{index}-L{index + 1}")
        for index in range(MAX_OPTIONS_PER_PAGE)
    )
    page = _snapshot_page(options=options, total_options=MAX_OPTIONS_PER_PAGE)

    rendered = render_result_page(page, max_chars=460)

    assert len(rendered.text) <= 460
    _assert_valid_telegram_html(rendered.text)
    lines = _numbered_lines(rendered.text)
    assert 0 < len(lines) < MAX_OPTIONS_PER_PAGE
    assert len(_url_buttons(rendered)) == len(lines)
    assert f"Showing {len(lines)} of {MAX_OPTIONS_PER_PAGE}" in rendered.text


def test_render_result_page_keyboard_rows_match_the_rendered_options() -> None:
    options = tuple(
        _option(performance=_performance(f"perf-{index}")) for index in range(15)
    )
    page = _snapshot_page(options=options, total_options=15)

    rendered = render_result_page(page)

    assert len(_url_buttons(rendered)) == len(_numbered_lines(rendered.text))


def test_render_result_page_keeps_pagination_when_no_option_fits() -> None:
    options = tuple(
        _option(performance=_performance(f"perf-{index}")) for index in range(MAX_OPTIONS_PER_PAGE)
    )
    page = _snapshot_page(options=options, total_options=30, page=2, total_pages=3)

    rendered = render_result_page(page, max_chars=200)

    assert len(rendered.text) <= 200
    _assert_valid_telegram_html(rendered.text)
    assert _numbered_lines(rendered.text) == []
    assert _url_buttons(rendered) == []


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


def test_render_watch_list_shows_the_status_of_each_watch() -> None:
    rendered = render_watch_list((_watch(status=WatchStatus.PAUSED),))

    assert "paused" in rendered.text


def test_render_watch_list_summarises_the_date_and_time_window() -> None:
    rendered = render_watch_list((_watch(),))

    assert "26 Aug 2026" in rendered.text
    assert "30 Aug 2026" in rendered.text
    assert "18:00" in rendered.text
    assert "23:00" in rendered.text


def test_render_watch_list_shows_the_requested_quantity() -> None:
    rendered = render_watch_list((_watch(quantity=3),))

    assert "3 seats" in rendered.text


def test_render_watch_list_shows_the_recurring_interval() -> None:
    rendered = render_watch_list(
        (_watch(mode=WatchMode.RECURRING, interval=timedelta(minutes=45)),)
    )

    assert "every 45m" in rendered.text


def test_render_watch_list_shows_a_compound_recurring_interval() -> None:
    rendered = render_watch_list(
        (_watch(mode=WatchMode.RECURRING, interval=timedelta(hours=2, minutes=30)),)
    )

    assert "every 2h 30m" in rendered.text


def test_render_watch_list_marks_a_one_off_watch_as_one_off() -> None:
    rendered = render_watch_list((_watch(mode=WatchMode.ONE_OFF),))

    assert "one-off" in rendered.text
    assert "every" not in rendered.text


def test_render_watch_list_shows_the_next_check_time() -> None:
    rendered = render_watch_list(
        (_watch(next_check_at=datetime(2026, 8, 26, 8, 30, tzinfo=UTC)),)
    )

    assert "Next check" in rendered.text
    assert "26 Aug 2026, 09:30" in rendered.text


def test_render_watch_list_says_when_no_next_check_is_scheduled() -> None:
    rendered = render_watch_list((_watch(status=WatchStatus.PAUSED, next_check_at=None),))

    assert "Next check: not scheduled" in rendered.text


def test_render_watch_list_shows_the_last_check_time_when_one_exists() -> None:
    rendered = render_watch_list(
        (_watch(last_check_at=datetime(2026, 8, 25, 7, 15, tzinfo=UTC)),)
    )

    assert "Last checked" in rendered.text
    assert "25 Aug 2026, 08:15" in rendered.text


def test_render_watch_list_omits_the_last_check_line_before_the_first_check() -> None:
    rendered = render_watch_list((_watch(last_check_at=None),))

    assert "Last checked" not in rendered.text


def test_render_watch_list_produces_valid_telegram_html() -> None:
    rendered = render_watch_list((_watch(title="Tom & Jerry <2026>"), _watch(title=None)))

    _assert_valid_telegram_html(rendered.text)


def test_render_watch_list_stays_valid_html_for_pathologically_long_titles() -> None:
    rendered = render_watch_list(tuple(_watch(title="&" * 3000) for _ in range(20)))

    _assert_valid_telegram_html(rendered.text)


def test_render_watch_list_drops_whole_watches_rather_than_slicing_markup() -> None:
    watches = tuple(_watch(title=f"Film {index}") for index in range(10))

    rendered = render_watch_list(watches, max_chars=400)

    assert len(rendered.text) <= 400
    _assert_valid_telegram_html(rendered.text)
    shown = _numbered_lines(rendered.text)
    assert 0 < len(shown) < len(watches)
    assert rendered.reply_markup is not None
    assert len(rendered.reply_markup.inline_keyboard) == len(shown)


def test_rendered_message_reply_markup_is_an_inline_keyboard_markup() -> None:
    rendered = render_result_page(_snapshot_page())

    assert isinstance(rendered.reply_markup, InlineKeyboardMarkup)
    for row in rendered.reply_markup.inline_keyboard:
        for button in row:
            assert isinstance(button, InlineKeyboardButton)
