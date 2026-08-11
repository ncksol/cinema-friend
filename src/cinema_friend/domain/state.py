"""Watch-related enums."""

from __future__ import annotations

from enum import Enum


class WatchMode(Enum):
    ONE_OFF = "one_off"
    RECURRING = "recurring"


class WatchStatus(Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class CheckTrigger(Enum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"
    RECOVERY = "recovery"


class CheckOutcome(Enum):
    SUCCESS = "success"
    NO_CHANGE = "no_change"
    NEW_OPTIONS = "new_options"
    NETWORK_ERROR = "network_error"
    CHALLENGE = "challenge"
    CONTRACT_ERROR = "contract_error"
    CIRCUIT_OPEN = "circuit_open"
    PERSISTENCE_ERROR = "persistence_error"
