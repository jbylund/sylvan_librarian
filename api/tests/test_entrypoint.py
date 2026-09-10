"""Tests for the master process's worker shutdown."""

from __future__ import annotations

import signal
import time
from unittest.mock import patch

from api import api_worker, entrypoint


class _FakeWorker:
    """Just enough of multiprocessing.Process for _stop_workers.

    `stubborn` workers ignore SIGTERM (a keep-alive connection bjoern is still waiting on, say) and
    only die on kill(); their join() really sleeps, so the deadline is exercised for real.
    """

    def __init__(self, pid: int | None, *, stubborn: bool = False, alive: bool = True) -> None:
        self.pid = pid
        self._alive = alive
        self._stubborn = stubborn
        self.terminated = False
        self.killed = False
        self.join_timeouts: list[float | None] = []

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self.terminated = True
        if not self._stubborn:
            self._alive = False

    def kill(self) -> None:
        self.killed = True
        self._alive = False

    def join(self, timeout: float | None = None) -> None:
        self.join_timeouts.append(timeout)
        if self._alive and timeout:
            time.sleep(min(timeout, 2.0))


class TestStopWorkers:
    def test_terminates_everyone_and_kills_only_the_stragglers(self) -> None:
        graceful = _FakeWorker(pid=101)
        stubborn = _FakeWorker(pid=102, stubborn=True)

        started = time.monotonic()
        entrypoint._stop_workers([graceful, stubborn], grace_seconds=0.3)
        elapsed = time.monotonic() - started

        assert graceful.terminated
        assert stubborn.terminated
        assert not graceful.killed, "a worker that exited on SIGTERM must not be SIGKILLed"
        assert stubborn.killed
        assert not graceful.is_alive()
        assert not stubborn.is_alive()
        # The grace period is a shared budget, not per worker.
        assert 0.3 <= elapsed < 1.0

    def test_join_deadline_is_shared_across_workers(self) -> None:
        first = _FakeWorker(pid=1, stubborn=True)
        second = _FakeWorker(pid=2, stubborn=True)
        entrypoint._stop_workers([first, second], grace_seconds=0.2)
        # First waited the whole budget; the second had (almost) nothing left and moved straight on.
        assert first.join_timeouts[0] is not None
        assert second.join_timeouts[0] is not None
        assert abs(first.join_timeouts[0] - 0.2) < 0.05
        assert second.join_timeouts[0] < 0.05

    def test_dead_and_unstarted_workers_are_left_alone(self) -> None:
        dead = _FakeWorker(pid=7, alive=False)
        unstarted = _FakeWorker(pid=None)
        entrypoint._stop_workers([dead, unstarted], grace_seconds=0.1)
        assert not dead.terminated
        assert not dead.killed
        assert not unstarted.terminated
        assert not unstarted.killed

    def test_default_grace_is_inside_composes_stop_grace_period(self) -> None:
        assert entrypoint.SHUTDOWN_GRACE_SECONDS + 1 < 10


def test_worker_forwards_sigterm_as_sigint_to_itself() -> None:
    """Bjoern shuts down gracefully on SIGINT only; SIGTERM would otherwise kill the worker outright."""
    with patch.object(api_worker.os, "kill") as mock_kill, patch.object(api_worker.os, "getpid", return_value=4242):
        api_worker.forward_sigterm_to_sigint(signal.SIGTERM, None)
    mock_kill.assert_called_once_with(4242, signal.SIGINT)
