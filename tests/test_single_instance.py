"""Tests for cinema_friend.single_instance: one process per database, enforced.

Two copies of this service pointed at one database is not a storage problem -- SQLite
would cope -- it is a *behavioural* one: two pollers on one bot token fight over
Telegram's long-poll, two schedulers double the request rate against a host that is
already rate-limiting us, and the user gets every notification twice. The service has
to refuse the second copy, and refuse it before it has done anything visible.

The mechanism matters as much as the outcome. A lock file holding a PID proves only
that a PID was once written; the number is reused, the writer may have been killed, and
nothing removes the file when a process dies. An advisory `flock` is held by the kernel
against the open file description and released when that description closes, however
the process ends -- including `kill -9`, which no cleanup handler survives. So the
tests below assert both halves: that a live holder blocks a second acquirer, and that a
*stale* file with a plausible PID in it does not.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from cinema_friend.domain.errors import AlreadyRunningError
from cinema_friend.single_instance import SingleInstanceLock, lock_path_for


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "cinema-friend.db"


# ---------------------------------------------------------------------------
# Where the lock lives, and what it looks like
# ---------------------------------------------------------------------------


def test_the_lock_sits_beside_the_database(database_path: Path) -> None:
    assert lock_path_for(database_path) == database_path.parent / "cinema-friend.db.lock"
    assert SingleInstanceLock(database_path).path == lock_path_for(database_path)


def test_acquiring_creates_the_lock_file_and_its_directory_at_mode_0600(
    database_path: Path,
) -> None:
    lock = SingleInstanceLock(database_path)

    lock.acquire()
    try:
        assert lock.path.exists()
        assert stat.S_IMODE(lock.path.stat().st_mode) == 0o600
    finally:
        lock.release()


def test_an_existing_lock_file_is_tightened_to_0600(database_path: Path) -> None:
    """A file left world-readable by an earlier version is not left that way."""
    path = lock_path_for(database_path)
    path.parent.mkdir(parents=True)
    path.write_text("")
    path.chmod(0o644)

    lock = SingleInstanceLock(database_path)
    lock.acquire()
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Exclusion
# ---------------------------------------------------------------------------


def test_a_second_lock_on_the_same_database_is_refused(database_path: Path) -> None:
    held = SingleInstanceLock(database_path)
    held.acquire()
    second = SingleInstanceLock(database_path)
    try:
        with pytest.raises(AlreadyRunningError) as caught:
            second.acquire()

        message = str(caught.value)
        assert str(lock_path_for(database_path)) in message
        assert not second.held
        assert held.held
    finally:
        held.release()


def test_a_refused_acquisition_does_not_release_the_holder(database_path: Path) -> None:
    """A losing acquirer must not touch the winner's lock, not even by closing it."""
    held = SingleInstanceLock(database_path)
    held.acquire()
    loser = SingleInstanceLock(database_path)
    try:
        with pytest.raises(AlreadyRunningError):
            loser.acquire()
        loser.release()

        with pytest.raises(AlreadyRunningError):
            SingleInstanceLock(database_path).acquire()
    finally:
        held.release()


def test_a_different_database_may_be_locked_at_the_same_time(tmp_path: Path) -> None:
    first = SingleInstanceLock(tmp_path / "one.db")
    second = SingleInstanceLock(tmp_path / "two.db")

    first.acquire()
    try:
        second.acquire()
        second.release()
    finally:
        first.release()


def test_the_lock_is_acquirable_again_after_release(database_path: Path) -> None:
    lock = SingleInstanceLock(database_path)
    lock.acquire()
    lock.release()

    again = SingleInstanceLock(database_path)
    again.acquire()
    try:
        assert again.held
    finally:
        again.release()


def test_a_stale_lock_file_naming_a_live_pid_does_not_block_anything(
    database_path: Path,
) -> None:
    """Proof the lock is the kernel's, not the file's contents.

    The file is left behind naming *this* very much alive process. A PID-existence
    check would refuse; an advisory lock, which nobody holds, does not.
    """
    path = lock_path_for(database_path)
    path.parent.mkdir(parents=True)
    path.write_text(f"{os.getpid()}\n")

    lock = SingleInstanceLock(database_path)
    lock.acquire()
    try:
        assert lock.held
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


def test_release_is_safe_when_nothing_was_acquired(database_path: Path) -> None:
    SingleInstanceLock(database_path).release()


def test_release_is_repeatable(database_path: Path) -> None:
    lock = SingleInstanceLock(database_path)
    lock.acquire()

    lock.release()
    lock.release()

    assert not lock.held


def test_the_context_manager_releases_even_when_the_body_raises(database_path: Path) -> None:
    lock = SingleInstanceLock(database_path)

    with pytest.raises(RuntimeError, match="startup"), lock:
        raise RuntimeError("startup failed half way")

    assert not lock.held
    SingleInstanceLock(database_path).acquire()


def test_acquiring_twice_from_the_same_object_is_a_programming_error(
    database_path: Path,
) -> None:
    lock = SingleInstanceLock(database_path)
    lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="already"):
            lock.acquire()
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Across processes
# ---------------------------------------------------------------------------


HOLDER = textwrap.dedent(
    """
    import sys
    from cinema_friend.single_instance import SingleInstanceLock

    lock = SingleInstanceLock(sys.argv[1])
    lock.acquire()
    print("held", flush=True)
    sys.stdin.readline()
    """
)


def test_a_lock_held_by_another_process_is_refused_and_freed_when_it_exits(
    database_path: Path,
) -> None:
    """The property that actually matters: a *second process* cannot start.

    In-process exclusion is necessary but not sufficient -- `fcntl.lockf` would pass
    every test above and silently grant the lock to a second process, because POSIX
    record locks are owned per-process rather than per-open-file-description.
    """
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(database_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdin is not None
        assert holder.stdout.readline().strip() == "held"

        with pytest.raises(AlreadyRunningError):
            SingleInstanceLock(database_path).acquire()

        holder.stdin.write("\n")
        holder.stdin.flush()
        assert holder.wait(timeout=10) == 0
    finally:
        if holder.poll() is None:  # pragma: no cover - only on an assertion failure
            holder.kill()
            holder.wait(timeout=10)

    freed = SingleInstanceLock(database_path)
    freed.acquire()
    freed.release()


def test_the_lock_is_freed_when_a_holder_is_killed_outright(database_path: Path) -> None:
    """No cleanup code runs on SIGKILL, so the kernel has to be the one releasing it."""
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(database_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "held"

    holder.kill()
    holder.wait(timeout=10)

    survivor = SingleInstanceLock(database_path)
    survivor.acquire()
    survivor.release()
