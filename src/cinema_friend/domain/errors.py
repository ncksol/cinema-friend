"""Domain errors."""

from __future__ import annotations


class InputError(ValueError):
    """Raised when caller-supplied input fails validation."""


class BfiNetworkError(OSError):
    """Raised on network-level failures when contacting BFI."""


class BfiChallengeError(Exception):
    """Raised when BFI responds with a CAPTCHA or bot-detection challenge."""


class BfiContractError(Exception):
    """Raised when BFI response does not match the expected schema."""


class CircuitOpenError(Exception):
    """Raised when the circuit breaker for a host is open."""


class PersistenceError(Exception):
    """Raised on database read/write failures."""


class DeliveryError(Exception):
    """Raised when a Telegram notification cannot be delivered."""
