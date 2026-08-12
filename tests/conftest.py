"""Test fixtures shared across the suite."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest


def _is_framework_task(task: asyncio.Task[object]) -> bool:
    """True for tasks pytest-asyncio creates to finalize its own async fixtures."""
    frame = getattr(task.get_coro(), "cr_frame", None)
    module = frame.f_globals.get("__name__", "") if frame is not None else ""
    return module.startswith(("pytest_asyncio", "_pytest"))


@pytest.fixture
async def no_leaked_tasks() -> AsyncIterator[None]:
    """Fail a test that leaves work running.

    Opt in with ``pytestmark = pytest.mark.usefixtures("no_leaked_tasks")`` in modules
    that start background loops. A task still running at the end of a test is the same
    defect as a daemon that will not shut down, and it is far easier to find here than
    as an occasional warning during a real restart.
    """
    before = asyncio.all_tasks()
    yield
    await asyncio.sleep(0)
    leaked = {
        task
        for task in asyncio.all_tasks()
        if task not in before and not task.done() and not _is_framework_task(task)
    }
    assert not leaked, f"tasks still running: {[task.get_name() for task in leaked]}"
