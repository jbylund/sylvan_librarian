"""Main entrypoint for the api container."""

from __future__ import annotations

import argparse
import logging
import multiprocessing
import os
import signal
import time
from typing import TYPE_CHECKING, Protocol

from api.api_worker import ApiWorker
from api.utils.deployment_reporting import report_deployment

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import FrameType

logger = logging.getLogger("api")

ALL_INTERFACES = "0.0.0.0"  # noqa: S104
DEFAULT_PORT = 8080
DEFAULT_WORKERS = max(2, int((os.cpu_count() or 1) * 0.6))

# How long the master waits for terminated workers to drain in-flight requests before killing the
# rest. Below docker compose's default 10 s stop_grace_period, so the whole shutdown -- this wait
# plus the kill and join -- finishes before compose gives up and SIGKILLs the container.
SHUTDOWN_GRACE_SECONDS = 8.0


def get_args() -> dict:
    """Argument parsing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--workers", type=int, dest="num_workers", default=DEFAULT_WORKERS)
    return vars(parser.parse_args())


class _StoppableWorker(Protocol):
    """The slice of multiprocessing.Process that _stop_workers drives (so a test can use a stand-in)."""

    pid: int | None

    def is_alive(self) -> bool: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def join(self, timeout: float | None = None) -> None: ...


def _stop_workers(workers: Sequence[_StoppableWorker], grace_seconds: float = SHUTDOWN_GRACE_SECONDS) -> None:
    """Stop every live worker: terminate them all, wait up to grace_seconds in total, kill the rest.

    The previous shutdown was `Process.kill()` -- SIGKILL -- for every worker the moment the master
    got its signal, so every deploy dropped whatever requests were in flight. terminate() sends
    SIGTERM, which the worker forwards to bjoern's SIGINT handling (see ApiWorker.run): bjoern stops
    accepting, lets its in-flight requests finish, and returns. The deadline is shared rather than
    per worker so a stubborn one cannot stretch the shutdown past the container's stop grace period;
    a worker still alive at the deadline (a keep-alive connection bjoern is waiting on, say) is
    killed as before.
    """
    live = [iworker for iworker in workers if iworker.pid is not None and iworker.is_alive()]
    for iworker in live:
        logger.info("Terminating worker %d", iworker.pid)
        iworker.terminate()

    deadline = time.monotonic() + grace_seconds
    for iworker in live:
        iworker.join(timeout=max(0.0, deadline - time.monotonic()))

    stragglers = [iworker for iworker in live if iworker.is_alive()]
    for iworker in stragglers:
        logger.warning("Worker %d did not exit within %.1fs, killing it", iworker.pid, grace_seconds)
        iworker.kill()
    for iworker in stragglers:
        iworker.join(timeout=1)


def _all_workers_alive(workers: list[ApiWorker], exit_flag: multiprocessing.Event) -> bool:
    if exit_flag.is_set():
        return False
    for iworker in workers:
        if not iworker.is_alive():
            logger.error(
                "Worker %s (pid=%s) died with exitcode %s",
                iworker.name,
                iworker.pid,
                iworker.exitcode,
            )
            return False
    return True


def run_server(
    *,
    port: int = DEFAULT_PORT,
    num_workers: int = DEFAULT_WORKERS,
) -> None:
    """Run the server."""
    logging.basicConfig(level=logging.INFO)
    workers: list[ApiWorker] = []
    logger.info("Starting %d workers on port %d...", num_workers, port)
    os.getpid()

    exit_flag = multiprocessing.Event()

    def graceful_shutdown(signum: int, frame: FrameType | None) -> None:
        # Only sets the flag: the main loop below sees it and runs the one shutdown sequence, so a
        # signal and a dead worker take the same path and the join/kill logic exists once.
        del frame
        logger.info("Received signal %d in pid %d, setting exit flag", signum, os.getpid())
        exit_flag.set()

    # Create shared objects for all workers
    import_guard = multiprocessing.RLock()
    last_import_time = multiprocessing.Value("d", 0.0, lock=True)
    schema_setup_event = multiprocessing.Event()
    cache_generation = multiprocessing.Value("i", 0, lock=True)
    engine_reload_guard = multiprocessing.Lock()

    # start workers
    for _ in range(num_workers):
        iworker = ApiWorker(
            cache_generation=cache_generation,
            engine_reload_guard=engine_reload_guard,
            exit_flag=exit_flag,
            host=ALL_INTERFACES,
            import_guard=import_guard,
            last_import_time=last_import_time,
            port=port,
            schema_setup_event=schema_setup_event,
        )
        workers.append(iworker)

    for iworker in workers:
        iworker.start()

    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)

    try:
        while _all_workers_alive(workers, exit_flag):
            # block for up to 1 second on exit flag being set
            response = exit_flag.wait(1 / 20)
            if response:
                logger.info("Exit flag set, terminating workers")
                break
        else:
            logger.info("Some workers died")
    except KeyboardInterrupt:
        graceful_shutdown(signal.SIGINT, None)

    # A worker died, or a signal arrived: either way the master exits, and takes the remaining
    # workers down with it (the container's restart policy brings the whole set back).
    exit_flag.set()
    _stop_workers(workers)

    logger.info("Main server process exiting")


def main() -> None:
    """Main entrypoint for the api container."""
    logging.basicConfig(level=logging.INFO)

    # Report deployment to Honeybadger if configured
    report_deployment()

    args = get_args()
    run_server(
        port=args["port"],
        num_workers=args["num_workers"],
    )


if __name__ == "__main__":
    main()
