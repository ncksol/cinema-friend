"""Watch-related enums."""

from __future__ import annotations

from enum import Enum


class WatchMode(Enum):
    ONE_OFF = "one_off"
    RECURRING = "recurring"


class WatchStatus(Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    BACKOFF = "backoff"
    COMPLETED = "completed"
    EXPIRED = "expired"
    FAILED = "failed"


class CheckTrigger(Enum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"
    RECOVERY = "recovery"


class CheckOutcome(Enum):
    # A check run is recorded when it starts, before its outcome is known, so that a
    # process that dies mid-check still leaves evidence the attempt happened.
    RUNNING = "running"
    SUCCESS = "success"
    NO_CHANGE = "no_change"
    NEW_OPTIONS = "new_options"
    NETWORK_ERROR = "network_error"
    CHALLENGE = "challenge"
    CONTRACT_ERROR = "contract_error"
    CIRCUIT_OPEN = "circuit_open"
    PERSISTENCE_ERROR = "persistence_error"
