"""One running process per database, enforced by an advisory lock the kernel holds.

Two copies of this service pointed at the same database is not a storage problem --
SQLite would serialise the writes and both would survive -- it is a behavioural one.
Two long-polls against one bot token fight over every update, so commands land in
whichever process won the race; two schedulers double the request rate against a host
that is already rate-limiting us; and a user with one watch gets every notification
twice. None of that is visible in a log until someone reads it carefully, which is the
worst kind of fault to leave available. So the second process refuses to start.

The lock is ``<DATABASE_PATH>.lock``, held for the lifetime of the process, taken
before the schema is opened and released only at teardown.

Why ``flock`` and not a PID file: a PID in a file proves only that a PID was written
once. The number is reused, the writer may have been ``kill -9``'d without ever
removing the file, and a machine that lost power leaves a file naming a process that
will never exist again -- so a PID check either refuses a legitimate restart or, if it
is made forgiving, stops refusing anything. :func:`fcntl.flock` is held by the kernel
against the *open file description*, so it is released when that description closes,
however the process ended: normal exit, unhandled exception, ``SIGKILL``, or panic.
There is nothing to clean up and nothing stale to reason about.

Why not :func:`fcntl.lockf`: POSIX record locks are owned per-process, so a second
``lockf`` in the same process succeeds -- and, worse, closing *any* descriptor for that
file drops the lock. ``flock`` on macOS and Linux is per-description, which is what
makes "one instance" mean one instance.

The lock file is deliberately never unlinked. Removing it while another process holds
a descriptor open on the same inode would let a third process create a fresh file at
the same path and lock that instead -- two "exclusive" holders, one path. An empty
file left behind costs nothing; the next start locks the same inode.
"""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
from types import TracebackType
from typing import Final, Self

from cinema_friend.domain.errors import AlreadyRunningError

LOCK_SUFFIX: Final = ".lock"
LOCK_MODE: Final = 0o600
"""Owner-only, like the env file. The lock names a path and a PID, not a secret, but a
world-writable lock is a lock any local process can take out from under the service."""

_CONTENDED_ERRNOS: Final = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK})


def lock_path_for(database_path: Path | str) -> Path:
    """The single-instance lock path for *database_path*: the design's ``<PATH>.lock``."""
    path = Path(database_path)
    return path.with_name(path.name + LOCK_SUFFIX)


class SingleInstanceLock:
    """An exclusive, non-blocking advisory lock on one database's lock file.

    Not reentrant and not shared between threads: one process holds one of these, for
    as long as it is running. ``acquire`` either takes the lock or raises; it never
    waits, because a second instance is a deployment mistake to report, not a queue to
    join.
    """

    def __init__(self, database_path: Path | str) -> None:
        self.path = lock_path_for(database_path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        """Take the lock, or raise :class:`AlreadyRunningError` if someone else has it."""
        if self._fd is not None:
            raise RuntimeError(f"single-instance lock is already held: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # O_CLOEXEC so a child we exec (launchctl, a shell, anything) does not inherit
        # the descriptor and keep the lock alive after this process is gone.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, LOCK_MODE)
        try:
            # O_CREAT applies the mode only when it creates the file, so a lock file
            # left behind at a looser mode keeps it. Set it explicitly on the open
            # descriptor: no window, and no way to retarget it via the path.
            os.fchmod(fd, LOCK_MODE)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            if error.errno in _CONTENDED_ERRNOS:
                raise AlreadyRunningError(
                    "another cinema-friend process is already running against this "
                    f"database{self._holder()}; lock: {self.path}"
                ) from error
            raise
        self._fd = fd
        self._record_owner(fd)

    def release(self) -> None:
        """Drop the lock if this object holds it. Safe to call any number of times.

        Only ever closes the descriptor this object opened, so a process that was
        refused the lock cannot release the holder's by tidying up after itself.
        """
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    def _record_owner(self, fd: int) -> None:
        """Write this process's PID into the locked file, for diagnosis only.

        Nothing reads this to decide whether the lock is free -- the kernel decides
        that. It exists so the error message a second process prints can name the
        process to go and look at.
        """
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
        except OSError:  # pragma: no cover - the lock is held; diagnosis is optional
            pass

    def _holder(self) -> str:
        try:
            pid = int(self.path.read_text().strip())
        except (OSError, ValueError):
            return ""
        return f" (pid {pid})"
