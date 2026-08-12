"""Domain results type tests."""

from datetime import UTC, datetime
from uuid import UUID

from cinema_friend.domain.bfi import Performance, PriceZone, Seat, SeatStatus
from cinema_friend.domain.results import RankedOption, RankVector, ResultSnapshot

_WATCH_ID = UUID("00000000-0000-4000-8000-000000000042")
_SNAPSHOT_ID = UUID("00000000-0000-4000-8000-000000000007")


def test_rank_vector_uses_ascending_sort_key_for_better_option():
    better = RankVector(
        preferred_seat_overlap=2,
        preferred_row_match=1,
        view_score_band=19,
        preferred_time_distance_minutes=20,
        raw_view_score=98.0,
        performance_start=datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
        seat_key="seat-a|seat-b",
    )
    worse = RankVector(
        preferred_seat_overlap=0,
        preferred_row_match=0,
        view_score_band=18,
        preferred_time_distance_minutes=0,
        raw_view_score=94.0,
        performance_start=datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
        seat_key="seat-c|seat-d",
    )
    assert better.sort_key() < worse.sort_key()


def test_rank_vector_final_tiebreak_is_the_stable_seat_key():
    common = {
        "preferred_seat_overlap": 1,
        "preferred_row_match": 1,
        "view_score_band": 19,
        "preferred_time_distance_minutes": 0,
        "raw_view_score": 95.0,
        "performance_start": datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
    }
    first = RankVector(seat_key="aaaa|bbbb", **common)
    second = RankVector(seat_key="cccc|dddd", **common)
    assert first.sort_key() < second.sort_key()


_PERF = Performance(
    performance_id="p1",
    start_utc=datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
    sales_status_code="OPEN",
    availability_status_code="E",
    availability_num=50,
    seat_map_url="https://example.com/map",
    options=("1", "2"),
)

_SEAT = Seat(
    seat_id="L17",
    raw_status_code="A",
    status=SeatStatus.AVAILABLE,
    zone=PriceZone(zone_id="z1", label="Premium", price=None),
    note="",
    section="BFI IMAX",
    row="L",
    column=17,
    x=120.0,
    y=80.5,
)


def test_seat_carries_svg_coordinates():
    assert _SEAT.x == 120.0
    assert _SEAT.y == 80.5


def _ranked_option(
    *,
    seat_label: str = "L17",
    seat_ids: tuple[str, ...] = ("1FA0A9C8-0000-4000-8000-000000000017",),
    performance: Performance = _PERF,
) -> RankedOption:
    return RankedOption(
        performance=performance,
        seat_label=seat_label,
        seat_ids=seat_ids,
        rank_vector=RankVector(
            preferred_seat_overlap=1,
            preferred_row_match=1,
            view_score_band=19,
            preferred_time_distance_minutes=0,
            raw_view_score=95.0,
            performance_start=datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
            seat_key="|".join(seat_ids),
        ),
        price_pence=1500,
    )


def test_result_snapshot_is_defined():
    snap = ResultSnapshot(
        snapshot_id=_SNAPSHOT_ID,
        watch_id=_WATCH_ID,
        checked_at=datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
        options=(_ranked_option(),),
    )
    assert snap.snapshot_id == _SNAPSHOT_ID
    assert snap.watch_id == _WATCH_ID
    assert len(snap.options) == 1


def test_option_key_is_the_performance_id_and_ordered_stable_seat_ids():
    option = _ranked_option(seat_label="L17-L18", seat_ids=("seat-a", "seat-b"))
    assert option.key == "p1:seat-a|seat-b"


def test_option_key_depends_on_seat_id_order():
    forwards = _ranked_option(seat_ids=("seat-a", "seat-b"))
    backwards = _ranked_option(seat_ids=("seat-b", "seat-a"))
    assert forwards.key != backwards.key


def test_option_keys_do_not_collide_when_a_row_number_repeats_across_sections():
    # Two different physical seat pairs that a human sees as the same "L17-L18"
    # because the venue numbers rows independently per section. The display label
    # is deliberately identical; only the stable seat IDs distinguish them.
    stalls = _ranked_option(seat_label="L17-L18", seat_ids=("stalls-l17", "stalls-l18"))
    balcony = _ranked_option(seat_label="L17-L18", seat_ids=("balcony-l17", "balcony-l18"))
    assert stalls.seat_label == balcony.seat_label
    assert stalls.key != balcony.key


def test_option_key_separator_cannot_be_produced_by_a_guid_seat_id():
    option = _ranked_option(
        seat_ids=(
            "1FA0A9C8-1111-4000-8000-000000000017",
            "1FA0A9C8-2222-4000-8000-000000000018",
        )
    )
    # A hyphen join would be ambiguous against the hyphens inside a BFI seat GUID.
    assert option.key.count("|") == 1
