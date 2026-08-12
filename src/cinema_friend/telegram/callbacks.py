"""Compact, versioned Telegram callback-data encoding.

Telegram caps ``callback_data`` at 64 bytes, so payloads carry only a version tag, a
type discriminator, and the minimum identifying fields -- never a title, a page size,
or anything else the handler could instead look up again from persisted state on
decode.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from cinema_friend.domain.errors import InputError

_VERSION = "v1"
_RESULT_PAGE_ACTION = "page"
#: Every action a watch keyboard may carry. ``delete`` only *offers* a deletion;
#: ``delete_confirm`` and ``keep`` are the two answers to that offer, so an accidental
#: tap on a stale keyboard can never destroy a watch on its own.
WATCH_ACTIONS = frozenset({"pause", "resume", "delete", "delete_confirm", "keep"})
MAX_CALLBACK_BYTES = 64

_MALFORMED = "malformed callback data"


@dataclass(frozen=True, slots=True)
class ResultPageAction:
    """Navigate to one page of a result snapshot."""

    snapshot_id: UUID
    page: int


@dataclass(frozen=True, slots=True)
class WatchAction:
    """Act on one owned watch."""

    action: str
    watch_id: UUID


CallbackAction = ResultPageAction | WatchAction


def encode_callback(action: str, resource_id: UUID, page: int | None = None) -> str:
    """Encode one callback as ``v1:r:<uuid>:<page>`` or ``v1:w:<action>:<uuid>``.

    ``action="page"`` together with a *page* produces a result-page callback; any
    other *action* must be one of :data:`WATCH_ACTIONS` and must not carry a *page*.
    Raises :class:`InputError` for an unknown action, a missing/non-positive page on a
    page callback, a page supplied for a watch action, or an encoded payload that
    would exceed Telegram's 64-byte ``callback_data`` limit.
    """
    if action == _RESULT_PAGE_ACTION:
        if page is None or page < 1:
            raise InputError("page must be a positive integer")
        data = f"{_VERSION}:r:{resource_id}:{page}"
    elif action in WATCH_ACTIONS:
        if page is not None:
            raise InputError("watch actions must not carry a page")
        data = f"{_VERSION}:w:{action}:{resource_id}"
    else:
        raise InputError(f"unknown callback action: {action!r}")

    if len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise InputError("callback data exceeds Telegram's 64-byte limit")
    return data


def decode_callback(data: str) -> CallbackAction:
    """Decode callback data produced by :func:`encode_callback`.

    Raises :class:`InputError` for anything that does not match the expected shape --
    wrong version, unknown type or action, a malformed UUID, a non-positive page, or
    an unexpected number of fields -- since callback data can be replayed or forged by
    anyone with a running Telegram client.
    """
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != _VERSION:
        raise InputError(_MALFORMED)
    _, kind, third, fourth = parts

    if kind == "r":
        try:
            snapshot_id = UUID(third)
            page = int(fourth)
        except ValueError as exc:
            raise InputError(_MALFORMED) from exc
        if page < 1:
            raise InputError(_MALFORMED)
        return ResultPageAction(snapshot_id=snapshot_id, page=page)

    if kind == "w":
        if third not in WATCH_ACTIONS:
            raise InputError(_MALFORMED)
        try:
            watch_id = UUID(fourth)
        except ValueError as exc:
            raise InputError(_MALFORMED) from exc
        return WatchAction(action=third, watch_id=watch_id)

    raise InputError(_MALFORMED)
