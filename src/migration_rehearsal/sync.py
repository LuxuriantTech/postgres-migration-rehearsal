"""Named synchronization for deterministic local concurrency tests."""

from __future__ import annotations

from concurrent.futures import Future
from threading import Barrier, Event, Lock

TIMEOUT_SECONDS = 5.0
BACKFILL_TIMEOUT_SECONDS = 3.0


class ClientWindow:
    """Coordinates two workers and their observing coordinator without scheduler timing."""

    def __init__(self) -> None:
        self._after_admission = Barrier(2)
        self._after_dml = Barrier(3)
        self._commit_release = Event()
        self._actors: dict[str, tuple[int, int, int]] = {}
        self._actors_lock = Lock()
        self._aborted = Event()

    def record_actor(self, name: str, identity: tuple[int, int, int]) -> None:
        with self._actors_lock:
            self._actors[name] = identity

    def actors(self) -> dict[str, tuple[int, int, int]]:
        with self._actors_lock:
            return dict(self._actors)

    def worker_after_admission(self) -> None:
        self._after_admission.wait(timeout=TIMEOUT_SECONDS)

    def worker_after_dml(self) -> None:
        self._after_dml.wait(timeout=TIMEOUT_SECONDS)

    def coordinator_after_dml(self) -> None:
        self._after_dml.wait(timeout=TIMEOUT_SECONDS)

    def worker_wait_for_commit_release(self) -> None:
        if not self._commit_release.wait(timeout=TIMEOUT_SECONDS) or self._aborted.is_set():
            raise RuntimeError("concurrent client window aborted")

    def release_commits(self) -> None:
        self._commit_release.set()

    def abort(self) -> None:
        self._aborted.set()
        self._after_admission.abort()
        self._after_dml.abort()
        self._commit_release.set()


class BackfillContentionWindow:
    """Coordinates the AC08 V1 writer without scheduler-dependent sleeps."""

    def __init__(self) -> None:
        self._pid_ready = Event()
        self._dml_done = Event()
        self._commit_release = Event()
        self._aborted = Event()

    def worker_pid_ready(self) -> None:
        self._pid_ready.set()

    def coordinator_wait_for_worker_pid(self, worker: Future[None]) -> None:
        if not self._pid_ready.wait(timeout=BACKFILL_TIMEOUT_SECONDS) or self._aborted.is_set():
            if worker.done():
                worker.result(timeout=0)
            raise RuntimeError("backfill V1 PID was not ready")

    def worker_dml_done(self) -> None:
        self._dml_done.set()

    def coordinator_wait_for_worker_dml(self, worker: Future[None]) -> None:
        if not self._dml_done.wait(timeout=BACKFILL_TIMEOUT_SECONDS) or self._aborted.is_set():
            if worker.done():
                worker.result(timeout=0)
            raise RuntimeError("backfill V1 DML did not complete")

    def worker_wait_for_commit_release(self) -> None:
        released = self._commit_release.wait(timeout=BACKFILL_TIMEOUT_SECONDS)
        if not released or self._aborted.is_set():
            raise RuntimeError("backfill V1 commit was not released")

    def release_commit(self) -> None:
        self._commit_release.set()

    def abort(self) -> None:
        self._aborted.set()
        self._pid_ready.set()
        self._dml_done.set()
        self._commit_release.set()
