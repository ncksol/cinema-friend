"""Tests for cinema_friend.telegram.callbacks.

Callback data survives a full round trip through Telegram's servers: a client can
replay it, retype it, or forge it outright, so every malformed shape must be rejected
rather than partially parsed, and every encoded payload must fit Telegram's 64-byte
``callback_data`` limit.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from cinema_friend.domain.errors import InputError
from cinema_friend.telegram.callbacks import (
    MAX_CALLBACK_BYTES,
    ResultPageAction,
    WatchAction,
    decode_callback,
    encode_callback,
)

_SNAPSHOT_ID = UUID("11111111-1111-4111-8111-111111111111")
_WATCH_ID = UUID("22222222-2222-4222-8222-222222222222")


# ---------------------------------------------------------------------------
# Result-page callbacks
# ---------------------------------------------------------------------------


def test_encode_result_page_round_trips_through_decode() -> None:
    data = encode_callback("page", _SNAPSHOT_ID, page=3)

    assert decode_callback(data) == ResultPageAction(snapshot_id=_SNAPSHOT_ID, page=3)


def test_encode_result_page_uses_the_documented_shape() -> None:
    assert encode_callback("page", _SNAPSHOT_ID, page=2) == f"v1:r:{_SNAPSHOT_ID}:2"


def test_encode_result_page_rejects_a_missing_page() -> None:
    with pytest.raises(InputError):
        encode_callback("page", _SNAPSHOT_ID)


@pytest.mark.parametrize("page", [0, -1])
def test_encode_result_page_rejects_a_non_positive_page(page: int) -> None:
    with pytest.raises(InputError):
        encode_callback("page", _SNAPSHOT_ID, page=page)


# ---------------------------------------------------------------------------
# Watch-action callbacks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["pause", "resume", "delete"])
def test_encode_watch_action_round_trips_through_decode(action: str) -> None:
    data = encode_callback(action, _WATCH_ID)

    assert decode_callback(data) == WatchAction(action=action, watch_id=_WATCH_ID)


def test_encode_watch_action_uses_the_documented_shape() -> None:
    assert encode_callback("pause", _WATCH_ID) == f"v1:w:pause:{_WATCH_ID}"


def test_encode_watch_action_rejects_a_page() -> None:
    with pytest.raises(InputError):
        encode_callback("pause", _WATCH_ID, page=1)


def test_encode_rejects_an_unknown_action() -> None:
    with pytest.raises(InputError):
        encode_callback("launch-missiles", _WATCH_ID)


# ---------------------------------------------------------------------------
# Encoded length
# ---------------------------------------------------------------------------


def test_encoded_result_page_callback_fits_the_telegram_limit() -> None:
    data = encode_callback("page", _SNAPSHOT_ID, page=999)

    assert len(data.encode("utf-8")) <= MAX_CALLBACK_BYTES


@pytest.mark.parametrize("action", ["pause", "resume", "delete"])
def test_encoded_watch_action_callback_fits_the_telegram_limit(action: str) -> None:
    data = encode_callback(action, _WATCH_ID)

    assert len(data.encode("utf-8")) <= MAX_CALLBACK_BYTES


# ---------------------------------------------------------------------------
# Malformed callback rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        "",
        "garbage",
        "v2:r:11111111-1111-4111-8111-111111111111:1",  # unknown version
        "v1:x:11111111-1111-4111-8111-111111111111:1",  # unknown type
        "v1:r:not-a-uuid:1",  # malformed uuid
        "v1:r:11111111-1111-4111-8111-111111111111:0",  # non-positive page
        "v1:r:11111111-1111-4111-8111-111111111111:-1",  # non-positive page
        "v1:r:11111111-1111-4111-8111-111111111111:abc",  # non-integer page
        "v1:w:launch-missiles:22222222-2222-4222-8222-222222222222",  # unknown action
        "v1:w:pause:not-a-uuid",  # malformed uuid
        "v1:r:11111111-1111-4111-8111-111111111111",  # too few fields
        "v1:r:11111111-1111-4111-8111-111111111111:1:extra",  # too many fields
    ],
)
def test_decode_rejects_malformed_callback_data(data: str) -> None:
    with pytest.raises(InputError):
        decode_callback(data)
