"""Domain results type tests."""

from datetime import UTC, datetime

from cinema_friend.domain.results import RankVector


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
