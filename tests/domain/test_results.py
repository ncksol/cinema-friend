"""Domain results type tests."""

from datetime import UTC, datetime

from cinema_friend.domain.bfi import Performance, PriceZone, Seat, SeatStatus
from cinema_friend.domain.results import RankedOption, RankVector, ResultSnapshot


def test_rank_vector_uses_ascending_sort_key_for_better_option():
    better = RankVector(
        preferred_seat_overlap=2,
        preferred_row_match=1,
        view_score_band=19,
        preferred_time_distance_minutes=20,
        raw_view_score=98.0,
        performance_start=datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
        seat_label="L17-L18",
    )
    worse = RankVector(
        preferred_seat_overlap=0,
        preferred_row_match=0,
        view_score_band=18,
        preferred_time_distance_minutes=0,
        raw_view_score=94.0,
        performance_start=datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
        seat_label="K1-K2",
    )
    assert better.sort_key() < worse.sort_key()


_PERF = Performance(
    performance_id="p1",
    event_id="e1",
    start_utc=datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
    sales_status_code="OPEN",
    availability_code="A",
    availability_num=50,
    reserved_seating=True,
    seat_map_url="https://example.com/map",
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


def test_result_snapshot_is_defined():
    snap = ResultSnapshot(
        snapshot_id=1,
        watch_id=42,
        checked_at=datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
        options=(
            RankedOption(
                performance=_PERF,
                seat_label="L17",
                rank_vector=RankVector(
                    preferred_seat_overlap=1,
                    preferred_row_match=1,
                    view_score_band=19,
                    preferred_time_distance_minutes=0,
                    raw_view_score=95.0,
                    performance_start=datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
                    seat_label="L17",
                ),
                price_pence=1500,
            ),
        ),
    )
    assert snap.snapshot_id == 1
    assert snap.watch_id == 42
    assert len(snap.options) == 1
