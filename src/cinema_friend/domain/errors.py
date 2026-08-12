"""Domain errors."""

from __future__ import annotations


class InputError(ValueError):
    """Raised when caller-supplied input fails validation."""


class AuthorizationError(InputError):
    """Raised when a caller is not permitted to interact with the service at all.

    Narrower than :class:`InputError` on purpose. Both are caller-fault rejections, but
    only this one means "you may not be here": it is the only error that may be answered
    with the generic denial and with no detail whatsoever. Everything else an authorized
    caller can provoke -- a stale button, a deleted watch, a malformed page -- is
    ordinary bad input and must still receive usable guidance, so a router that cannot
    tell the two apart either leaks state to strangers or leaves real users in silence.
    """


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


class ConflictError(Exception):
    """Raised when persisted state changed under a unit of work that had already read it.

    Distinct from :class:`PersistenceError`: the database is healthy and did exactly what
    it was told. Someone else legitimately changed the row first, and the losing caller
    must abandon its work rather than write over the winner.
    """


class DeliveryError(Exception):
    """Raised when a Telegram notification cannot be delivered."""


class AlreadyRunningError(RuntimeError):
    """Raised when another process already holds this database's single-instance lock.

    A deployment fault rather than a runtime one: nothing about it improves by waiting,
    so it is reported at startup, before the process has polled Telegram or asked BFI
    for anything.
    """
